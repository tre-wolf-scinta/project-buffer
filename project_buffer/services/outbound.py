"""Sending approved messages to the co-parent.

Bias: never send the same message twice. The outbound row is committed as
``sending`` before the provider is called, the draft is flipped ``ready`` →
``approved`` atomically, and an ambiguous failure is resolved by looking the
message up at the provider instead of sending again.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from project_buffer.domain.enums import (
    DeliveryStatus,
    Direction,
    DraftStatus,
    JobKind,
    ProcessingStatus,
    SenderStatus,
)
from project_buffer.infrastructure.db.models import Draft, Message
from project_buffer.infrastructure.sms.base import SmsRejectedError, SmsUncertainError
from project_buffer.services import audit, delivery, jobs
from project_buffer.services.container import Services
from project_buffer.services.drafts import body_digest, validate_body

logger = logging.getLogger(__name__)


class SendOutcome(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    ALREADY_HANDLED = "already_handled"
    STALE_REVIEW = "stale_review"
    INVALID = "invalid"


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    message_id: uuid.UUID | None = None
    error: str | None = None


def approve_and_send(
    session: Session,
    services: Services,
    *,
    draft_id: uuid.UUID,
    reviewed_digest: str,
    now: datetime,
) -> SendResult:
    """Send a draft the owner has just approved. Commits as it goes."""
    settings = services.settings
    draft = session.get(Draft, draft_id)
    if draft is None:
        return SendResult(SendOutcome.INVALID, error="Draft not found.")
    if draft.status != DraftStatus.READY:
        return SendResult(SendOutcome.ALREADY_HANDLED, draft.sent_message_id)

    body = draft.body
    problem = validate_body(body)
    if problem:
        return SendResult(SendOutcome.INVALID, error=problem)
    if body_digest(body) != reviewed_digest:
        return SendResult(SendOutcome.STALE_REVIEW)

    # Atomic claim: of two concurrent approvals (a double-click), exactly one updates a row.
    claimed = session.execute(
        update(Draft)
        .where(Draft.id == draft_id, Draft.status == DraftStatus.READY)
        .values(status=DraftStatus.APPROVED, approved_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        session.rollback()
        return SendResult(SendOutcome.ALREADY_HANDLED)

    message_id = uuid.uuid4()
    message = Message(
        id=message_id,
        conversation_id=draft.conversation_id,
        direction=Direction.OUTBOUND,
        provider=services.sms.provider,
        from_number=settings.twilio_phone_number,
        to_number=settings.coparent_phone_number,
        body_ciphertext=b"",
        body_mac=services.crypto.mac(body.encode()),
        occurred_at=now,
        sender_status=SenderStatus.OWNER,
        processing_status=ProcessingStatus.NOT_APPLICABLE,
        delivery_status=DeliveryStatus.SENDING,
        in_reply_to_id=draft.in_reply_to_id,
        read_at=now,
        handled_at=now,
    )
    message.body_ciphertext = services.crypto.encrypt(body.encode(), aad=message.body_aad())
    session.add(message)
    session.execute(
        update(Draft)
        .where(Draft.id == draft_id)
        .values(sent_message_id=message_id)
        .execution_options(synchronize_session=False)
    )
    audit.record(
        session,
        actor="owner",
        action="outbound_approved",
        subject_type="message",
        subject_id=message_id,
        draft_id=str(draft_id),
        edited_after_ai=draft.ai_draft_text is not None and draft.ai_draft_text != body,
    )
    # Durable record of intent before any external side effect.
    session.commit()
    session.refresh(draft)

    try:
        sent = services.sms.send(
            to=settings.coparent_phone_number,
            body=body,
            status_callback_url=f"{settings.application_base_url}/webhooks/twilio/status",
        )
    except SmsRejectedError as exc:
        message.delivery_status = DeliveryStatus.FAILED
        message.delivery_error_code = exc.code
        draft.status = DraftStatus.FAILED
        audit.record(
            session,
            actor="system",
            action="outbound_rejected",
            subject_type="message",
            subject_id=message_id,
            error_code=exc.code,
        )
        session.commit()
        return SendResult(SendOutcome.FAILED, message_id, describe_error(exc.code))
    except SmsUncertainError:
        message.delivery_status = DeliveryStatus.UNCERTAIN
        jobs.enqueue(
            session,
            JobKind.RECONCILE_OUTBOUND,
            now=now,
            max_attempts=5,
            message_id=message_id,
            dedupe_key=f"reconcile:{message_id}",
            run_after=now + timedelta(seconds=20),
        )
        audit.record(
            session,
            actor="system",
            action="outbound_uncertain",
            subject_type="message",
            subject_id=message_id,
        )
        session.commit()
        return SendResult(SendOutcome.UNCERTAIN, message_id)

    _record_accepted(session, message, draft, sent.provider_message_id, sent.status, now)
    if draft.in_reply_to is not None and draft.in_reply_to.handled_at is None:
        draft.in_reply_to.handled_at = now
        draft.in_reply_to.requires_response = False
    session.commit()
    logger.info("outbound message accepted id=%s", message_id)
    return SendResult(SendOutcome.SENT, message_id)


def _record_accepted(
    session: Session, message: Message, draft: Draft | None, sid: str, status: str, now: datetime
) -> None:
    message.provider_message_id = sid
    message.sent_at = now
    message.delivery_status = delivery.map_status(status) or DeliveryStatus.QUEUED
    if draft is not None:
        draft.status = DraftStatus.SENT
    audit.record(
        session,
        actor="system",
        action="outbound_accepted",
        subject_type="message",
        subject_id=message.id,
        provider_message_id=sid,
    )
    session.flush()
    # Status callbacks can beat this commit; apply any that arrived early.
    delivery.adopt_orphan_events(session, message)


def reconcile_outbound(
    session: Session, services: Services, message_id: uuid.UUID, now: datetime, *, final: bool
) -> None:
    """Resolve an ``uncertain`` send by asking the provider whether it exists."""
    message = session.get(Message, message_id)
    if message is None or message.delivery_status != DeliveryStatus.UNCERTAIN:
        return
    body = services.crypto.decrypt(message.body_ciphertext, aad=message.body_aad()).decode()
    found = services.sms.find_sent(
        to=message.to_number, body=body, since=message.occurred_at - timedelta(minutes=2)
    )
    draft = session.scalar(select(Draft).where(Draft.sent_message_id == message_id))
    if found is not None:
        _record_accepted(session, message, draft, found.provider_message_id, found.status, now)
        return
    if not final:
        raise SmsUncertainError("not yet visible at provider")
    message.delivery_status = DeliveryStatus.FAILED
    if draft is not None:
        draft.status = DraftStatus.FAILED
    audit.record(
        session,
        actor="worker",
        action="outbound_not_sent",
        subject_type="message",
        subject_id=message_id,
    )
    delivery.enqueue_delivery_failure_notice(session, services, message, now)


def describe_error(code: str | None) -> str:
    known = {
        "21610": "The recipient has opted out of messages from this number by texting STOP. "
        "They must text START to this number before messages can be delivered.",
        "21211": "The recipient's phone number is not valid.",
        "21614": "The recipient's number cannot receive text messages.",
        "30003": "The recipient's phone is unreachable.",
        "30005": "The recipient's number is unknown or no longer in service.",
        "30006": "The recipient's number is a landline or cannot receive texts.",
        "30007": "The carrier filtered the message.",
        "30034": "The sending number is not registered for US messaging (A2P 10DLC).",
    }
    if code and code in known:
        return known[code]
    return (
        f"The text messaging provider reported error {code}."
        if code
        else ("The text messaging provider rejected the message.")
    )
