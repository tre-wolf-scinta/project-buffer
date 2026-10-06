"""SMS gateway interface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class SentSms:
    provider_message_id: str
    status: str


@dataclass(frozen=True)
class FetchedMedia:
    content: bytes
    content_type: str


class SmsRejectedError(Exception):
    """The provider definitely did not accept the message."""

    def __init__(self, code: str | None = None, http_status: int | None = None) -> None:
        super().__init__(f"sms rejected code={code} status={http_status}")
        self.code = code
        self.http_status = http_status


class SmsUncertainError(Exception):
    """The request may or may not have reached the provider. Do not blindly resend."""


class MediaFetchError(Exception):
    def __init__(self, reason: str, *, retryable: bool = True) -> None:
        super().__init__(reason)
        self.retryable = retryable


class SmsGateway(Protocol):
    provider: str

    def validate_signature(self, url: str, params: Mapping[str, str], signature: str) -> bool: ...

    def send(self, *, to: str, body: str, status_callback_url: str | None = None) -> SentSms: ...

    def find_sent(self, *, to: str, body: str, since: datetime) -> SentSms | None:
        """Look up a previously attempted send, to resolve an uncertain outcome."""
        ...

    def fetch_media(self, url: str, max_bytes: int) -> FetchedMedia: ...
