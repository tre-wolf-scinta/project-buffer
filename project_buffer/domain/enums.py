"""Domain enumerations. Values are stored as strings in the database."""

from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class SenderStatus(StrEnum):
    AUTHORIZED = "authorized"
    UNRECOGNIZED = "unrecognized"
    OWNER = "owner"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    NOT_APPLICABLE = "not_applicable"


class DeliveryStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    SENDING = "sending"
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    UNDELIVERED = "undelivered"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class Urgency(StrEnum):
    EMERGENCY = "emergency"
    URGENT = "urgent"
    NORMAL = "normal"
    INFORMATIONAL = "informational"

    @property
    def rank(self) -> int:
        return {"informational": 0, "normal": 1, "urgent": 2, "emergency": 3}[self.value]

    @property
    def is_elevated(self) -> bool:
        return self in (Urgency.EMERGENCY, Urgency.URGENT)


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class JobKind(StrEnum):
    ANALYZE_MESSAGE = "analyze_message"
    NOTIFY_OWNER = "notify_owner"
    FETCH_MEDIA = "fetch_media"
    RECONCILE_OUTBOUND = "reconcile_outbound"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DraftStatus(StrEnum):
    READY = "ready"
    APPROVED = "approved"
    SENT = "sent"
    FAILED = "failed"
    DISCARDED = "discarded"


class NotificationKind(StrEnum):
    SUMMARY = "summary"
    REMINDER_1 = "reminder_1"
    REMINDER_2 = "reminder_2"
    REMINDER_3 = "reminder_3"
    PROCESSING_DELAYED = "processing_delayed"
    PROCESSING_FAILED = "processing_failed"
    UNRECOGNIZED_SENDER = "unrecognized_sender"
    DELIVERY_FAILED = "delivery_failed"
    MISSED_CALL = "missed_call"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class AttachmentStatus(StrEnum):
    PENDING = "pending"
    STORED = "stored"
    FAILED = "failed"


class ContactRole(StrEnum):
    COPARENT = "coparent"
    OWNER = "owner"


class AuthAttemptKind(StrEnum):
    PASSWORD = "password"  # noqa: S105
    TOTP = "totp"
