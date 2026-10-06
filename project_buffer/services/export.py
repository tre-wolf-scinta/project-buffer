"""Record export for a date range, as a ZIP of JSON and CSV files.

Originals and AI summaries are written to separate files so a summary can
never be mistaken for the message it describes.
"""

from __future__ import annotations

import csv
import io
import json
import mimetypes
import zipfile
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from project_buffer.domain.enums import AttachmentStatus
from project_buffer.infrastructure.db.models import AuditEvent, Message
from project_buffer.services import audit
from project_buffer.services.container import Services
from project_buffer.services.media import read_attachment

README = """\
Project Buffer export

messages.csv              One row per message: metadata only, no message text.
originals.json            Exact original text of every message, as received or sent,
                          with the complete provider payload for inbound messages.
                          Present only if originals were included.
sanitized_summaries.json  AI-GENERATED summaries of inbound messages. These are NOT
                          the messages. They are what the owner normally reads.
delivery_events.json      Delivery status callbacks from the messaging provider.
audit_events.json         Application audit trail for the period.
attachments/              Attachment files, named <message id>_<position>.

All timestamps are UTC, ISO 8601. body_hmac_sha256 is a keyed digest of the
original text recorded at ingestion; it lets the application detect alteration.
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def build_export(
    session: Session,
    services: Services,
    *,
    start: datetime,
    end: datetime,
    include_originals: bool,
    now: datetime,
) -> bytes:
    messages = session.scalars(
        select(Message)
        .where(Message.occurred_at >= start, Message.occurred_at < end)
        .options(
            selectinload(Message.analyses),
            selectinload(Message.attachments),
            selectinload(Message.delivery_events),
        )
        .order_by(Message.occurred_at)
    ).all()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", README)

        rows = io.StringIO()
        writer = csv.writer(rows)
        writer.writerow(
            [
                "message_id",
                "direction",
                "occurred_at_utc",
                "from",
                "to",
                "provider",
                "provider_message_id",
                "sender_status",
                "num_attachments",
                "delivery_status",
                "sent_at_utc",
                "processed_at_utc",
                "body_hmac_sha256",
            ]
        )
        originals: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []

        for message in messages:
            writer.writerow(
                [
                    message.id,
                    message.direction.value,
                    _iso(message.occurred_at),
                    message.from_number,
                    message.to_number,
                    message.provider,
                    message.provider_message_id or "",
                    message.sender_status.value,
                    message.num_media,
                    message.delivery_status.value,
                    _iso(message.sent_at) or "",
                    _iso(message.processed_at) or "",
                    message.body_mac,
                ]
            )
            for analysis in message.analyses:
                summaries.append(
                    {
                        "message_id": str(message.id),
                        "ai_generated": True,
                        "is_current": analysis.is_current,
                        "created_at_utc": _iso(analysis.created_at),
                        "llm_provider": analysis.llm_provider,
                        "llm_model": analysis.llm_model,
                        "prompt_version": analysis.prompt_version,
                        "analysis": analysis.structured,
                    }
                )
            for event in message.delivery_events:
                events.append(
                    {
                        "message_id": str(message.id),
                        "provider_message_id": event.provider_message_id,
                        "status": event.status,
                        "error_code": event.error_code,
                        "received_at_utc": _iso(event.received_at),
                    }
                )
            if not include_originals:
                continue
            crypto = services.crypto
            body = crypto.decrypt(message.body_ciphertext, aad=message.body_aad()).decode()
            payload = None
            if message.raw_payload_ciphertext:
                payload = json.loads(
                    crypto.decrypt(message.raw_payload_ciphertext, aad=message.payload_aad())
                )
            files = []
            for attachment in message.attachments:
                entry: dict[str, Any] = {
                    "position": attachment.position,
                    "content_type": attachment.content_type,
                    "size_bytes": attachment.size_bytes,
                    "status": attachment.status.value,
                    "file": None,
                }
                if attachment.status == AttachmentStatus.STORED:
                    extension = mimetypes.guess_extension(attachment.content_type) or ".bin"
                    name = f"attachments/{message.id}_{attachment.position}{extension}"
                    archive.writestr(name, read_attachment(session, services, attachment))
                    entry["file"] = name
                files.append(entry)
            originals.append(
                {
                    "message_id": str(message.id),
                    "direction": message.direction.value,
                    "occurred_at_utc": _iso(message.occurred_at),
                    "from": message.from_number,
                    "to": message.to_number,
                    "provider_message_id": message.provider_message_id,
                    "original_text": body,
                    "integrity_verified": crypto.mac(body.encode()) == message.body_mac,
                    "provider_payload": payload,
                    "attachments": files,
                }
            )

        audit_rows = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.occurred_at >= start, AuditEvent.occurred_at < end)
            .order_by(AuditEvent.occurred_at)
        ).all()

        archive.writestr("messages.csv", rows.getvalue())
        if include_originals:
            archive.writestr("originals.json", json.dumps(originals, indent=2))
        archive.writestr("sanitized_summaries.json", json.dumps(summaries, indent=2))
        archive.writestr("delivery_events.json", json.dumps(events, indent=2))
        archive.writestr(
            "audit_events.json",
            json.dumps(
                [
                    {
                        "occurred_at_utc": _iso(row.occurred_at),
                        "actor": row.actor,
                        "action": row.action,
                        "subject_type": row.subject_type,
                        "subject_id": row.subject_id,
                        "detail": row.detail,
                    }
                    for row in audit_rows
                ],
                indent=2,
            ),
        )
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "generated_at_utc": _iso(now),
                    "range_start_utc": _iso(start),
                    "range_end_utc": _iso(end),
                    "message_count": len(messages),
                    "originals_included": include_originals,
                },
                indent=2,
            ),
        )

    audit.record(
        session,
        actor="owner",
        action="export_created",
        message_count=len(messages),
        originals_included=include_originals,
        range_start=_iso(start),
        range_end=_iso(end),
    )
    return buffer.getvalue()
