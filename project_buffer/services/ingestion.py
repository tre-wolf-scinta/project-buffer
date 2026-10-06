"""Inbound message ingestion.

Runs inside the webhook request and does only fast, local work: persist the
encrypted original and queue the follow-up jobs in one transaction.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from project_buffer.domain.enums import (
    Direction,
    JobKind,
    NotificationKind,
    ProcessingStatus,
    SenderStatus,
)
from project_buffer.domain.phone import mask_phone, try_normalize_e164
from project_buffer.infrastructure.db.models import Attachment, Message
from project_buffer.services import audit, jobs
from project_buffer.services.container import Services
from project_buffer.services.conversations import ensure_conversation

logger = logging.getLogger(__name__)

# Provider fields that carry message text and so stay out of readable metadata.
_TEXT_FIELDS = {"Body"}
_MAX_MEDIA = 20


class IngestOutcome(StrEnum):
    STORED = "stored"
    QUARANTINED = "quarantined"
    DUPLICATE = "duplicate"
    OWNER_IGNORED = "owner_ignored"
    INVALID = "invalid"


@dataclass(frozen=True)
class IngestResult:
    outcome: IngestOutcome
    message_id: uuid.UUID | None = None


def ingest_inbound_sms(
    session: Session, services: Services, params: Mapping[str, str], now: datetime
) -> IngestResult:
    """Persist one inbound message. The caller commits."""
    settings = services.settings
    sid = params.get("MessageSid") or params.get("SmsSid")
    raw_from = params.get("From", "")
    if not sid or not raw_from:
        return IngestResult(IngestOutcome.INVALID)

    from_number = try_normalize_e164(raw_from) or raw_from[:32]
    to_number = try_normalize_e164(params.get("To")) or params.get("To", "")[:32]

    if from_number == settings.owner_phone_number:
        # A reply to a notification. Not a co-parent message, so nothing is stored.
        audit.record(session, actor="twilio", action="owner_sms_ignored", provider_message_id=sid)
        return IngestResult(IngestOutcome.OWNER_IGNORED)

    existing = session.scalar(
        select(Message.id).where(
            Message.provider == services.sms.provider, Message.provider_message_id == sid
        )
    )
    if existing is not None:
        return IngestResult(IngestOutcome.DUPLICATE, existing)

    authorized = from_number == settings.coparent_phone_number
    conversation = ensure_conversation(session, settings)
    body = params.get("Body", "")
    message_id = uuid.uuid4()
    message = Message(
        id=message_id,
        conversation_id=conversation.id,
        direction=Direction.INBOUND,
        provider=services.sms.provider,
        provider_message_id=sid,
        from_number=from_number,
        to_number=to_number,
        body_ciphertext=b"",
        body_mac=services.crypto.mac(body.encode()),
        provider_metadata={k: v for k, v in params.items() if k not in _TEXT_FIELDS},
        occurred_at=now,
        sender_status=SenderStatus.AUTHORIZED if authorized else SenderStatus.UNRECOGNIZED,
        processing_status=ProcessingStatus.PENDING if authorized else ProcessingStatus.QUARANTINED,
    )
    message.body_ciphertext = services.crypto.encrypt(body.encode(), aad=message.body_aad())
    message.raw_payload_ciphertext = services.crypto.encrypt(
        json.dumps(dict(params), sort_keys=True).encode(), aad=message.payload_aad()
    )

    attachments = _attachments_from(params, message_id)
    message.num_media = len(attachments)

    try:
        with session.begin_nested():
            session.add(message)
            session.add_all(attachments)
            session.flush()
    except IntegrityError:
        # Twilio retried while the first delivery was still being written.
        return IngestResult(IngestOutcome.DUPLICATE)

    max_attempts = settings.job_max_attempts
    if authorized:
        jobs.enqueue(
            session,
            JobKind.ANALYZE_MESSAGE,
            now=now,
            max_attempts=max_attempts,
            message_id=message_id,
            dedupe_key=f"analyze:{message_id}:initial",
        )
        enqueue_media_jobs(session, attachments, now=now, max_attempts=max_attempts)
    elif settings.notify_unrecognized_senders:
        # At most one neutral notice per hour, however many texts arrive.
        jobs.enqueue(
            session,
            JobKind.NOTIFY_OWNER,
            now=now,
            max_attempts=max_attempts,
            message_id=message_id,
            payload={"kind": NotificationKind.UNRECOGNIZED_SENDER.value},
            dedupe_key=f"notify:unrecognized:{now:%Y%m%d%H}",
        )

    audit.record(
        session,
        actor="twilio",
        action="message_received" if authorized else "message_quarantined",
        subject_type="message",
        subject_id=message_id,
        provider_message_id=sid,
        num_media=len(attachments),
    )
    logger.info(
        "inbound message stored id=%s sender=%s authorized=%s media=%d",
        message_id,
        mask_phone(from_number),
        authorized,
        len(attachments),
    )
    return IngestResult(
        IngestOutcome.STORED if authorized else IngestOutcome.QUARANTINED, message_id
    )


def _attachments_from(params: Mapping[str, str], message_id: uuid.UUID) -> list[Attachment]:
    try:
        count = min(int(params.get("NumMedia", "0") or 0), _MAX_MEDIA)
    except ValueError:
        count = 0
    attachments = []
    for position in range(count):
        url = params.get(f"MediaUrl{position}")
        if not url:
            continue
        attachments.append(
            Attachment(
                message_id=message_id,
                position=position,
                provider_media_url=url,
                content_type=params.get(f"MediaContentType{position}", "application/octet-stream")[
                    :120
                ],
            )
        )
    return attachments


def enqueue_media_jobs(
    session: Session, attachments: list[Attachment], *, now: datetime, max_attempts: int
) -> None:
    for attachment in attachments:
        jobs.enqueue(
            session,
            JobKind.FETCH_MEDIA,
            now=now,
            max_attempts=max_attempts,
            message_id=attachment.message_id,
            payload={"attachment_id": str(attachment.id)},
            dedupe_key=f"media:{attachment.id}",
        )
