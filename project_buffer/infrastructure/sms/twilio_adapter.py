"""Twilio Programmable Messaging adapter."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from urllib.parse import urlparse

import httpx
import requests
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from project_buffer.infrastructure.sms.base import (
    FetchedMedia,
    MediaFetchError,
    SentSms,
    SmsRejectedError,
    SmsUncertainError,
)


class TwilioGateway:
    provider = "twilio"

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from = from_number
        self._validator = RequestValidator(auth_token)
        self._client = Client(account_sid, auth_token, http_client=TwilioHttpClient(timeout=20))

    def validate_signature(self, url: str, params: Mapping[str, str], signature: str) -> bool:
        if not signature:
            return False
        return bool(self._validator.validate(url, dict(params), signature))

    def send(self, *, to: str, body: str, status_callback_url: str | None = None) -> SentSms:
        kwargs: dict[str, str] = {"to": to, "from_": self._from, "body": body}
        if status_callback_url:
            kwargs["status_callback"] = status_callback_url
        try:
            message = self._client.messages.create(**kwargs)
        except TwilioRestException as exc:
            if exc.status >= 500:
                raise SmsUncertainError(f"twilio status={exc.status}") from None
            raise SmsRejectedError(
                code=str(exc.code) if exc.code else None, http_status=exc.status
            ) from None
        except requests.RequestException as exc:
            raise SmsUncertainError(type(exc).__name__) from None
        return SentSms(provider_message_id=str(message.sid), status=str(message.status))

    def find_sent(self, *, to: str, body: str, since: datetime) -> SentSms | None:
        try:
            candidates = self._client.messages.list(
                to=to, from_=self._from, date_sent_after=since, limit=50
            )
        except (TwilioRestException, requests.RequestException) as exc:
            raise SmsUncertainError(type(exc).__name__) from None
        for candidate in candidates:
            if candidate.body == body:
                return SentSms(provider_message_id=str(candidate.sid), status=str(candidate.status))
        return None

    def fetch_media(self, url: str, max_bytes: int) -> FetchedMedia:
        host = urlparse(url).hostname or ""
        if urlparse(url).scheme != "https" or not (
            host == "twilio.com" or host.endswith(".twilio.com")
        ):
            raise MediaFetchError("unexpected media host", retryable=False)
        try:
            # httpx drops the Authorization header on cross-origin redirects.
            with (
                httpx.Client(timeout=30, follow_redirects=True) as client,
                client.stream("GET", url, auth=(self._account_sid, self._auth_token)) as response,
            ):
                if response.status_code == 404:
                    raise MediaFetchError("media not found", retryable=False)
                if response.status_code >= 400:
                    raise MediaFetchError(f"status={response.status_code}")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise MediaFetchError("media too large", retryable=False)
                    chunks.append(chunk)
                content_type = response.headers.get("content-type", "application/octet-stream")
        except httpx.HTTPError as exc:
            raise MediaFetchError(type(exc).__name__) from None
        return FetchedMedia(content=b"".join(chunks), content_type=content_type.split(";")[0])
