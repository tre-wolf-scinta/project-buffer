"""Delivery status tracking from provider callbacks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from project_buffer.domain.enums import DeliveryStatus, Direction, JobKind, NotificationKind
from project_buffer.infrastructure.db.models import DeliveryEvent, Message, Notification
from project_buffer.services import audit, jobs
from project_buffer.services.container import Services

_STATUS_MAP = {
    "accepted": DeliveryStatus.QUEUED,
    "scheduled": DeliveryStatus.QUEUED,
    "queued": DeliveryStatus.QUEUED,
    "sending": DeliveryStatus.QUEUED,
    "sent": DeliveryStatus.SENT,
    "delivered": DeliveryStatus.DELIVERED,
    "read": DeliveryStatus.DELIVERED,
    "undelivered": DeliveryStatus.UNDELIVERED,
    "failed": DeliveryStatus.FAILED,
}
# Callbacks can arrive out of order; a status only ever moves to a higher rank.
_RANK = {
    DeliveryStatus.SENDING: 0,
    DeliveryStatus.UNCERTAIN: 0,
    DeliveryStatus.QUEUED: 1,
    DeliveryStatus.SENT: 2,
    DeliveryStatus.UNDELIVERED: 3,
    DeliveryStatus.FAILED: 3,
    DeliveryStatus.DELIVERED: 4,
}
_FAILURES = (DeliveryStatus.UNDELIVERED, DeliveryStatus.FAILED)


def map_status(provider_status: str) -> DeliveryStatus | None:
    return _STATUS_MAP.get(provider_status.lower())


def _apply(message: Message, status: DeliveryStatus, error_code: str | None) -> bool:
    """Advance the message's status. Returns True if it newly became a failure."""
    current = message.delivery_status
    if _RANK.get(status, 0) <= _RANK.get(current, 0):
        return False
    message.delivery_status = status
    if status in _FAILURES:
        message.delivery_error_code = error_code
        return current not in _FAILURES
    return False


def enqueue_delivery_failure_notice(
    session: Session, services: Services, message: Message, now: datetime
) -> None:
    jobs.enqueue(
        session,
        JobKind.NOTIFY_OWNER,
        now=now,
        max_attempts=services.settings.job_max_attempts,
        message_id=message.id,
        payload={"kind": NotificationKind.DELIVERY_FAILED.value},
        dedupe_key=f"notify:delivery_failed:{message.id}",
    )


def handle_status_callback(
    session: Session, services: Services, params: Mapping[str, str], now: datetime
) -> bool:
    """Record a provider status callback. Returns False if it carried no usable ID."""
    sid = params.get("MessageSid") or params.get("SmsSid")
    provider_status = params.get("MessageStatus") or params.get("SmsStatus") or ""
    if not sid or not provider_status:
        return False
    error_code = params.get("ErrorCode") or None

    message = session.scalar(
        select(Message).where(
            Message.provider == services.sms.provider, Message.provider_message_id == sid
        )
    )
    notification = None
    if message is None:
        notification = session.scalar(
            select(Notification).where(Notification.provider_message_id == sid)
        )
    session.add(
        DeliveryEvent(
            message_id=message.id if message else None,
            notification_id=notification.id if notification else None,
            provider_message_id=sid,
            status=provider_status[:32],
            error_code=error_code[:16] if error_code else None,
            payload=dict(params),
            received_at=now,
        )
    )
    if notification is not None:
        notification.delivery_status = provider_status[:32]
    status = map_status(provider_status)
    if (
        message is not None
        and status is not None
        and message.direction == Direction.OUTBOUND
        and _apply(message, status, error_code)
    ):
        enqueue_delivery_failure_notice(session, services, message, now)
        audit.record(
            session,
            actor="twilio",
            action="outbound_delivery_failed",
            subject_type="message",
            subject_id=message.id,
            error_code=error_code,
        )
    return True


def adopt_orphan_events(session: Session, message: Message) -> None:
    """Attach callbacks that arrived before the provider ID was stored."""
    if not message.provider_message_id:
        return
    orphans = session.scalars(
        select(DeliveryEvent)
        .where(
            DeliveryEvent.provider_message_id == message.provider_message_id,
            DeliveryEvent.message_id.is_(None),
            DeliveryEvent.notification_id.is_(None),
        )
        .order_by(DeliveryEvent.received_at)
    ).all()
    for event in orphans:
        event.message_id = message.id
        status = map_status(event.status)
        if status is not None:
            _apply(message, status, event.error_code)
