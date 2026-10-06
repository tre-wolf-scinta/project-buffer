"""Database schema.

Two records are kept strictly apart: ``Message`` holds the encrypted original
exactly as received or sent; ``MessageAnalysis`` holds AI output about it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, Session, attributes, mapped_column, relationship

from project_buffer.clock import utcnow
from project_buffer.domain.enums import (
    AttachmentStatus,
    AuthAttemptKind,
    ContactRole,
    DeliveryStatus,
    Direction,
    DraftStatus,
    JobKind,
    JobStatus,
    NotificationKind,
    NotificationStatus,
    ProcessingStatus,
    SenderStatus,
    Urgency,
)
from project_buffer.infrastructure.db.base import Base, JsonType, StrEnumType, UtcDateTime


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    # Last accepted TOTP time-step, so a code cannot be replayed.
    totp_last_counter: Mapped[int | None] = mapped_column(Integer)

    @property
    def mfa_enabled(self) -> bool:
        return self.totp_enabled_at is not None


class RecoveryCode(Base):
    __tablename__ = "recovery_codes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    mfa_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    user: Mapped[User] = relationship()


class AuthAttempt(Base):
    __tablename__ = "auth_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    kind: Mapped[AuthAttemptKind] = mapped_column(StrEnumType(AuthAttemptKind))
    ip: Mapped[str] = mapped_column(String(64))
    succeeded: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)


class Contact(TimestampMixin, Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    role: Mapped[ContactRole] = mapped_column(StrEnumType(ContactRole), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    phone_number: Mapped[str] = mapped_column(String(20))


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    contact: Mapped[Contact] = relationship()


class Message(TimestampMixin, Base):
    """One SMS/MMS exactly as received or sent. Original columns are immutable."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("provider", "provider_message_id"),
        Index("ix_messages_conversation_occurred", "conversation_id", "occurred_at"),
        Index("ix_messages_triage", "direction", "handled_at", "urgency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[Direction] = mapped_column(StrEnumType(Direction))
    provider: Mapped[str] = mapped_column(String(32), default="twilio")
    # Null only for an outbound message the provider has not yet accepted.
    provider_message_id: Mapped[str | None] = mapped_column(String(64))
    from_number: Mapped[str] = mapped_column(String(32))
    to_number: Mapped[str] = mapped_column(String(32))
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    body_mac: Mapped[str] = mapped_column(String(64))
    # Complete provider payload (including the body), encrypted. Inbound only.
    raw_payload_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Provider fields that contain no message text, kept readable for audits.
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    num_media: Mapped[int] = mapped_column(Integer, default=0)
    # received_at for inbound, approval time for outbound.
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime)
    sender_status: Mapped[SenderStatus] = mapped_column(StrEnumType(SenderStatus))

    processing_status: Mapped[ProcessingStatus] = mapped_column(StrEnumType(ProcessingStatus))
    processed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        StrEnumType(DeliveryStatus), default=DeliveryStatus.NOT_APPLICABLE
    )
    delivery_error_code: Mapped[str | None] = mapped_column(String(16))
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    urgency: Mapped[Urgency] = mapped_column(StrEnumType(Urgency), default=Urgency.NORMAL)
    urgency_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_response: Mapped[bool] = mapped_column(Boolean, default=False)
    read_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    handled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    in_reply_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))

    analyses: Mapped[list[MessageAnalysis]] = relationship(
        back_populates="message", order_by="MessageAnalysis.created_at"
    )
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="message", order_by="Attachment.position"
    )
    delivery_events: Mapped[list[DeliveryEvent]] = relationship(
        back_populates="message", order_by="DeliveryEvent.received_at"
    )

    @property
    def current_analysis(self) -> MessageAnalysis | None:
        return next((a for a in reversed(self.analyses) if a.is_current), None)

    @property
    def is_inbound(self) -> bool:
        return self.direction == Direction.INBOUND

    def body_aad(self) -> bytes:
        return f"message-body:{self.id}".encode()

    def payload_aad(self) -> bytes:
        return f"message-payload:{self.id}".encode()


IMMUTABLE_MESSAGE_COLUMNS = (
    "conversation_id",
    "direction",
    "provider",
    "from_number",
    "to_number",
    "body_ciphertext",
    "body_mac",
    "raw_payload_ciphertext",
    "provider_metadata",
    "num_media",
    "occurred_at",
)


class ImmutableRecordError(Exception):
    """An attempt was made to alter or delete an original record."""


@event.listens_for(Session, "before_flush")
def _protect_originals(session: Session, flush_context: Any, instances: Any) -> None:
    for obj in session.deleted:
        if isinstance(obj, Message | MessageAnalysis | Attachment | DeliveryEvent | AuditEvent):
            raise ImmutableRecordError(f"{type(obj).__name__} rows cannot be deleted")
    for obj in session.dirty:
        if not isinstance(obj, Message):
            continue
        for column in IMMUTABLE_MESSAGE_COLUMNS:
            if attributes.get_history(obj, column).has_changes():
                raise ImmutableRecordError(f"messages.{column} is immutable")
        history = attributes.get_history(obj, "provider_message_id")
        if history.has_changes() and any(v is not None for v in history.deleted):
            raise ImmutableRecordError("messages.provider_message_id can only be set once")


class MessageAnalysis(Base):
    """AI-generated sanitized representation. Append-only; never the original."""

    __tablename__ = "message_analyses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    llm_provider: Mapped[str] = mapped_column(String(32))
    llm_model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(16))
    short_summary: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(String(120))
    # The full validated MessageAnalysisResult.
    structured: Mapped[dict[str, Any]] = mapped_column(JsonType)
    # Flattened sanitized text for default search.
    search_text: Mapped[str] = mapped_column(Text)
    safety_keywords_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    message: Mapped[Message] = relationship(back_populates="analyses")


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("message_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    provider_media_url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(120))
    status: Mapped[AttachmentStatus] = mapped_column(
        StrEnumType(AttachmentStatus), default=AttachmentStatus.PENDING
    )
    storage_key: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    content_mac: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    message: Mapped[Message] = relationship(back_populates="attachments")


class MediaBlob(Base):
    """Encrypted attachment bytes for the database-backed media store."""

    __tablename__ = "media_blobs"

    storage_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Draft(TimestampMixin, Base):
    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    in_reply_to_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"), index=True)
    instruction: Mapped[str] = mapped_column(Text, default="")
    # What the model proposed, kept for the record even after edits.
    ai_draft_text: Mapped[str | None] = mapped_column(Text)
    ai_notes: Mapped[list[str]] = mapped_column(JsonType, default=list)
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[DraftStatus] = mapped_column(
        StrEnumType(DraftStatus), default=DraftStatus.READY, index=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    sent_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))

    in_reply_to: Mapped[Message | None] = relationship(foreign_keys=[in_reply_to_id])
    sent_message: Mapped[Message | None] = relationship(foreign_keys=[sent_message_id])


class DeliveryEvent(Base):
    __tablename__ = "delivery_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"), index=True)
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("notifications.id"), index=True
    )
    provider_message_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    message: Mapped[Message | None] = relationship(back_populates="delivery_events")


class Notification(Base):
    """A sanitized SMS sent to the owner. Unique per message and kind."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("dedupe_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"), index=True)
    kind: Mapped[NotificationKind] = mapped_column(StrEnumType(NotificationKind))
    dedupe_key: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(
        StrEnumType(NotificationStatus), default=NotificationStatus.PENDING
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(64), index=True)
    delivery_status: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class ProcessingJob(TimestampMixin, Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        Index("ix_processing_jobs_claim", "status", "run_after"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    kind: Mapped[JobKind] = mapped_column(StrEnumType(JobKind))
    status: Mapped[JobStatus] = mapped_column(StrEnumType(JobStatus), default=JobStatus.QUEUED)
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(120))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer)
    run_after: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    # Exception class and status code only. Never message content.
    last_error: Mapped[str | None] = mapped_column(String(200))
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str | None] = mapped_column(String(32))
    subject_id: Mapped[str | None] = mapped_column(String(64), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime)
