"""Development gateway: sends nothing, accepts no webhooks."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime

from project_buffer.domain.phone import mask_phone
from project_buffer.infrastructure.sms.base import FetchedMedia, MediaFetchError, SentSms

logger = logging.getLogger(__name__)


class ConsoleGateway:
    provider = "console"

    def validate_signature(self, url: str, params: Mapping[str, str], signature: str) -> bool:
        return False

    def send(self, *, to: str, body: str, status_callback_url: str | None = None) -> SentSms:
        logger.info("console sms: would send %d characters to %s", len(body), mask_phone(to))
        return SentSms(provider_message_id=f"DEV{uuid.uuid4().hex}", status="sent")

    def find_sent(self, *, to: str, body: str, since: datetime) -> SentSms | None:
        return None

    def fetch_media(self, url: str, max_bytes: int) -> FetchedMedia:
        raise MediaFetchError("media download is unavailable without Twilio", retryable=False)
