"""Downloads MMS media from the provider into private storage."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from project_buffer.domain.enums import AttachmentStatus
from project_buffer.infrastructure.db.models import Attachment
from project_buffer.infrastructure.sms.base import MediaFetchError
from project_buffer.services.container import Services

logger = logging.getLogger(__name__)


def fetch_attachment(
    session: Session, services: Services, attachment_id: uuid.UUID, now: datetime, *, final: bool
) -> None:
    attachment = session.get(Attachment, attachment_id)
    if attachment is None or attachment.status == AttachmentStatus.STORED:
        return
    try:
        media = services.sms.fetch_media(
            attachment.provider_media_url, services.settings.max_media_bytes
        )
    except MediaFetchError as exc:
        if exc.retryable and not final:
            raise
        attachment.status = AttachmentStatus.FAILED
        logger.error("attachment fetch failed id=%s reason=%s", attachment.id, exc)
        return
    key = f"attachment/{attachment.id}"
    services.media.put(session, key, media.content)
    attachment.storage_key = key
    attachment.size_bytes = len(media.content)
    attachment.content_mac = services.crypto.mac(media.content)
    attachment.content_type = media.content_type or attachment.content_type
    attachment.status = AttachmentStatus.STORED
    attachment.fetched_at = now


def read_attachment(session: Session, services: Services, attachment: Attachment) -> bytes:
    if attachment.status != AttachmentStatus.STORED or not attachment.storage_key:
        raise KeyError(str(attachment.id))
    return services.media.get(session, attachment.storage_key)
