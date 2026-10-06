"""Sanitized SMS notifications to the owner.

Bias: a duplicate notice is harmless, a missing one is not. So sends are
at-least-once, with a short claim window to avoid obvious duplicates.
Notification text is built only from sanitized analysis fields and fixed
wording. The original message text is never read here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from project_buffer.config import Settings
from project_buffer.domain.enums import (
    Direction,
    JobKind,
    NotificationKind,
    NotificationStatus,
    ProcessingStatus,
    SenderStatus,
    Urgency,
)
from project_buffer.domain.phone import mask_phone
from project_buffer.infrastructure.db.models import Message, Notification
from project_buffer.infrastructure.sms.base import SmsRejectedError, SmsUncertainError
from project_buffer.services import audit, jobs
from project_buffer.services.container import Services

logger = logging.getLogger(__name__)

MAX_SMS_CHARS = 300
_CLAIM_WINDOW = timedelta(minutes=2)
_REMINDERS = (
    NotificationKind.REMINDER_1,
    NotificationKind.REMINDER_2,
    NotificationKind.REMINDER_3,
)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def _with_link(text: str, link: str) -> str:
    return f"{_truncate(text, MAX_SMS_CHARS - len(link) - 1)} {link}"


def build_text(settings: Settings, kind: NotificationKind, message: Message | None) -> str | None:
    """Compose the SMS for a notification, or None if it no longer applies."""
    name = settings.coparent_display_name
    base = settings.application_base_url

    if kind == NotificationKind.UNRECOGNIZED_SENDER:
        sender = mask_phone(message.from_number) if message else "an unknown number"
        return _with_link(
            f"A text arrived from an unrecognized {sender}. It is saved but has not been "
            "processed or shown.",
            f"{base}/inbox?filter=quarantined",
        )
    if message is None:
        return None
    link = settings.url_for_message(message.id)

    if kind == NotificationKind.PROCESSING_DELAYED:
        if message.processing_status != ProcessingStatus.PENDING:
            return None
        return _with_link(
            f"A message from {name} was received. Automatic filtering is delayed and is "
            "still being retried.",
            link,
        )
    if kind == NotificationKind.PROCESSING_FAILED:
        if message.processing_status != ProcessingStatus.FAILED:
            return None
        return _with_link(
            f"A message from {name} was received, but automatic filtering failed. The "
            "original is saved and has not been shown. Open to retry:",
            link,
        )
    if kind == NotificationKind.DELIVERY_FAILED:
        return _with_link(f"Your message to {name} could not be delivered. Open for details:", link)

    analysis = message.current_analysis
    if analysis is None:
        return None
    prefix = {Urgency.EMERGENCY: "EMERGENCY", Urgency.URGENT: "URGENT"}.get(message.urgency)

    if kind in _REMINDERS:
        if message.read_at is not None or message.handled_at is not None or prefix is None:
            return None
        return _with_link(f"Reminder: unread {prefix} message from {name}: {analysis.topic}.", link)

    if settings.notify_mode == "link_only":
        label = f"{prefix} message" if prefix else "New message"
        return _with_link(f"{label} from {name}.", link)

    head = f"{prefix} from {name}" if prefix else name
    reply = " Reply needed." if message.requires_response else ""
    return _with_link(f"{head}: {analysis.topic}. {analysis.short_summary}{reply}", link)


def _claim(
    session: Session,
    *,
    dedupe_key: str,
    kind: NotificationKind,
    message_id: uuid.UUID | None,
    body: str,
    now: datetime,
) -> Notification | None:
    """Reserve the right to send. Returns None if it is sent or freshly claimed elsewhere."""
    existing = session.scalar(select(Notification).where(Notification.dedupe_key == dedupe_key))
    if existing is None:
        notification = Notification(
            message_id=message_id, kind=kind, dedupe_key=dedupe_key, body=body, created_at=now
        )
        try:
            with session.begin_nested():
                session.add(notification)
                session.flush()
        except IntegrityError:
            return None
        session.commit()
        return notification
    if existing.status != NotificationStatus.PENDING:
        return None
    if now - existing.created_at < _CLAIM_WINDOW:
        return None
    # A previous attempt died mid-send. Take it over.
    existing.created_at = now
    existing.body = body
    session.commit()
    return existing


def send_notification(
    session: Session,
    services: Services,
    *,
    kind: NotificationKind,
    message_id: uuid.UUID | None,
    dedupe_key: str,
    now: datetime,
) -> None:
    """Send one owner notification. Raises SmsUncertainError so the job retries."""
    settings = services.settings
    message = session.get(Message, message_id) if message_id else None
    text = build_text(settings, kind, message)
    if text is None or settings.notify_mode == "off":
        return

    notification = _claim(
        session, dedupe_key=dedupe_key, kind=kind, message_id=message_id, body=text, now=now
    )
    if notification is None:
        return

    try:
        sent = services.sms.send(
            to=settings.owner_phone_number,
            body=text,
            status_callback_url=f"{settings.application_base_url}/webhooks/twilio/status",
        )
    except SmsUncertainError:
        # Release the claim so the job's retry is not mistaken for a duplicate.
        notification.created_at = now - _CLAIM_WINDOW
        session.commit()
        raise
    except SmsRejectedError as exc:
        notification.status = NotificationStatus.FAILED
        audit.record(
            session,
            actor="worker",
            action="owner_notification_rejected",
            subject_type="notification",
            subject_id=notification.id,
            error_code=exc.code,
        )
        session.commit()
        logger.error("owner notification rejected kind=%s code=%s", kind.value, exc.code)
        return

    notification.status = NotificationStatus.SENT
    notification.provider_message_id = sent.provider_message_id
    notification.delivery_status = sent.status
    notification.sent_at = now
    audit.record(
        session,
        actor="worker",
        action="owner_notified",
        subject_type="message",
        subject_id=message_id,
        kind=kind.value,
    )
    _schedule_reminder(session, settings, kind, message, now)
    session.commit()


def _schedule_reminder(
    session: Session,
    settings: Settings,
    kind: NotificationKind,
    message: Message | None,
    now: datetime,
) -> None:
    if message is None or message.urgency != Urgency.EMERGENCY:
        return
    if kind == NotificationKind.SUMMARY:
        index = 0
    elif kind in _REMINDERS:
        index = _REMINDERS.index(kind) + 1
    else:
        return
    if index >= min(settings.notify_emergency_max_repeats, len(_REMINDERS)):
        return
    next_kind = _REMINDERS[index]
    jobs.enqueue(
        session,
        JobKind.NOTIFY_OWNER,
        now=now,
        max_attempts=settings.job_max_attempts,
        message_id=message.id,
        payload={"kind": next_kind.value},
        dedupe_key=f"notify:{next_kind.value}:{message.id}",
        run_after=now + timedelta(minutes=settings.notify_emergency_repeat_minutes),
    )


def run_notification_job(
    session: Session,
    services: Services,
    message_id: uuid.UUID | None,
    payload: dict[str, str],
    dedupe_key: str | None,
    now: datetime,
) -> None:
    kind = NotificationKind(payload["kind"])
    send_notification(
        session,
        services,
        kind=kind,
        message_id=message_id,
        dedupe_key=dedupe_key or f"notify:{kind.value}:{message_id}",
        now=now,
    )


def notify_delayed_messages(session: Session, services: Services, now: datetime) -> int:
    """Send a neutral notice for messages stuck unprocessed.

    Called from the web process as well as the worker, so a dead worker cannot
    hide the fact that a message arrived.
    """
    settings = services.settings
    cutoff = now - timedelta(minutes=settings.processing_delay_notice_minutes)
    stuck = session.scalars(
        select(Message.id).where(
            Message.direction == Direction.INBOUND,
            Message.sender_status == SenderStatus.AUTHORIZED,
            Message.processing_status == ProcessingStatus.PENDING,
            Message.occurred_at < cutoff,
        )
    ).all()
    for message_id in stuck:
        send_notification(
            session,
            services,
            kind=NotificationKind.PROCESSING_DELAYED,
            message_id=message_id,
            dedupe_key=f"notify:processing_delayed:{message_id}",
            now=now,
        )
    return len(stuck)
