"""Shared fixtures. No test talks to Twilio or an LLM vendor.

Tests run on SQLite by default. Set TEST_DATABASE_URL to a disposable Postgres
database to run the same suite there, including Postgres-only behaviour.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pyotp
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator

from project_buffer.config import Settings
from project_buffer.domain.analysis import DraftReplyResult, MessageAnalysisResult
from project_buffer.infrastructure.crypto import AesGcmEncryptionService, generate_key
from project_buffer.infrastructure.db.base import Base
from project_buffer.infrastructure.db.models import User
from project_buffer.infrastructure.db.session import create_db_engine, create_session_factory
from project_buffer.infrastructure.llm.base import AnalysisRequest, DraftRequest
from project_buffer.infrastructure.media import DatabaseMediaStorage
from project_buffer.infrastructure.sms.base import FetchedMedia, SentSms
from project_buffer.services import auth
from project_buffer.services.container import Services
from project_buffer.web.app import create_app

BASE_URL = "https://buffer.test"
TWILIO_NUMBER = "+12025550100"
COPARENT = "+12025550101"
OWNER = "+12025550102"
STRANGER = "+12025550199"
TEST_AUTH_TOKEN = "test-twilio-auth-token"
OWNER_USERNAME = "owner"
OWNER_PASSWORD = "correct horse battery staple"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"

POSTGRES_URL = os.environ.get("TEST_DATABASE_URL")


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": "sqlite:///:memory:",
        "application_base_url": BASE_URL,
        "secret_key": "s" * 48,
        "raw_message_encryption_key": generate_key(),
        "sms_provider": "console",
        "twilio_phone_number": TWILIO_NUMBER,
        "coparent_phone_number": COPARENT,
        "owner_phone_number": OWNER,
        "coparent_display_name": "Jordan",
        "owner_display_name": "Alex",
        "children_names": "Riley, Sam",
        "owner_timezone": "America/New_York",
        "llm_provider": "fake",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeSms:
    """In-memory SMS gateway that validates signatures exactly as Twilio does."""

    provider = "twilio"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.failures: list[Exception] = []
        self.findable: SentSms | None = None
        self.media: dict[str, FetchedMedia] = {}
        self._validator = RequestValidator(TEST_AUTH_TOKEN)
        self._counter = 0

    def validate_signature(self, url: str, params: Mapping[str, str], signature: str) -> bool:
        return bool(signature) and bool(self._validator.validate(url, dict(params), signature))

    def send(self, *, to: str, body: str, status_callback_url: str | None = None) -> SentSms:
        if self.failures:
            raise self.failures.pop(0)
        self._counter += 1
        sid = f"SMout{self._counter:029d}"
        self.sent.append({"to": to, "body": body, "sid": sid, "callback": status_callback_url})
        return SentSms(provider_message_id=sid, status="queued")

    def find_sent(self, *, to: str, body: str, since: datetime) -> SentSms | None:
        return self.findable

    def fetch_media(self, url: str, max_bytes: int) -> FetchedMedia:
        return self.media[url]

    def sent_to(self, number: str) -> list[dict[str, Any]]:
        return [message for message in self.sent if message["to"] == number]


class ScriptedLLM:
    """Returns queued results or raises queued exceptions, recording every request."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self) -> None:
        self.analyses: list[MessageAnalysisResult | Exception] = []
        self.drafts: list[DraftReplyResult | Exception] = []
        self.analysis_requests: list[AnalysisRequest] = []
        self.draft_requests: list[DraftRequest] = []

    def analyze_message(self, request: AnalysisRequest) -> MessageAnalysisResult:
        self.analysis_requests.append(request)
        if not self.analyses:
            raise AssertionError("unexpected analyze_message call")
        item = self.analyses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def draft_reply(self, request: DraftRequest) -> DraftReplyResult:
        self.draft_requests.append(request)
        if not self.drafts:
            raise AssertionError("unexpected draft_reply call")
        item = self.drafts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def analysis(**overrides: Any) -> MessageAnalysisResult:
    values = {"short_summary": "Jordan asks about the weekend schedule.", "topic": "Schedule"}
    values.update(overrides)
    return MessageAnalysisResult.minimal(**values)


@pytest.fixture(scope="session")
def _engine_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    if POSTGRES_URL:
        return POSTGRES_URL
    return f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"


@pytest.fixture(scope="session")
def _schema(_engine_url: str) -> Iterator[Any]:
    """Create the schema once: via migrations on Postgres, via metadata on SQLite."""
    url = make_settings(database_url=_engine_url).database_url
    engine = create_db_engine(url)
    if engine.dialect.name == "postgresql":
        from alembic import command
        from alembic.config import Config

        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        os.environ.update(
            DATABASE_URL=url, SECRET_KEY="s" * 48, RAW_MESSAGE_ENCRYPTION_KEY=generate_key()
        )
        from project_buffer.config import get_settings

        get_settings.cache_clear()
        command.upgrade(Config("alembic.ini"), "head")
    else:
        Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def services(_schema: Any, _engine_url: str) -> Iterator[Services]:
    engine = _schema
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
            connection.execute(text(f"TRUNCATE {names} CASCADE"))
        else:
            connection.execute(text("PRAGMA foreign_keys=OFF"))
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())
    settings = make_settings(database_url=_engine_url)
    crypto = AesGcmEncryptionService(settings.raw_message_encryption_key.get_secret_value())
    yield Services(
        settings=settings,
        session_factory=create_session_factory(engine),
        crypto=crypto,
        llm=ScriptedLLM(),
        sms=FakeSms(),
        media=DatabaseMediaStorage(crypto),
    )


@pytest.fixture
def sms(services: Services) -> FakeSms:
    assert isinstance(services.sms, FakeSms)
    return services.sms


@pytest.fixture
def llm(services: Services) -> ScriptedLLM:
    assert isinstance(services.llm, ScriptedLLM)
    return services.llm


@pytest.fixture
def db(services: Services) -> Iterator[Session]:
    with services.session_factory() as session:
        yield session


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    with TestClient(create_app(services), base_url=BASE_URL, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def owner(services: Services) -> User:
    """An owner account with two-step sign-in already enabled."""
    now = datetime.now(UTC)
    with services.session_factory() as session:
        user = auth.create_owner(session, OWNER_USERNAME, OWNER_PASSWORD)
        user.totp_secret_encrypted = services.crypto.encrypt(
            TOTP_SECRET.encode(), aad=f"totp:{user.id}".encode()
        )
        user.totp_enabled_at = now
        session.commit()
        return user


def soup_of(response: Any) -> BeautifulSoup:
    return BeautifulSoup(response.text, "html.parser")


def csrf_from(response: Any) -> str:
    field = soup_of(response).find("input", {"name": "csrf_token"})
    assert field is not None, "page has no CSRF field"
    return str(field["value"])


def sign_in(client: TestClient, *, complete_mfa: bool = True) -> str:
    """Sign in through the real forms. Returns the session CSRF token."""
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": OWNER_USERNAME,
            "password": OWNER_PASSWORD,
            "csrf_token": csrf_from(page),
        },
    )
    assert response.status_code == 303, response.text
    page = client.get("/login/verify")
    token = csrf_from(page)
    if complete_mfa:
        response = client.post(
            "/login/verify", data={"code": pyotp.TOTP(TOTP_SECRET).now(), "csrf_token": token}
        )
        assert response.status_code == 303, response.text
    return token


@pytest.fixture
def auth_client(client: TestClient, owner: User) -> TestClient:
    """A client signed in as the owner. The CSRF token is on ``client.csrf``."""
    client.csrf = sign_in(client)  # type: ignore[attr-defined]
    return client


def twilio_signature(path: str, params: Mapping[str, str]) -> str:
    return str(RequestValidator(TEST_AUTH_TOKEN).compute_signature(BASE_URL + path, dict(params)))


_sid_counter = 0


def inbound_params(
    body: str, *, sender: str = COPARENT, sid: str | None = None, **extra: str
) -> dict[str, str]:
    global _sid_counter
    _sid_counter += 1
    params = {
        "MessageSid": sid or f"SMin{_sid_counter:030d}",
        "AccountSid": "ACtest",
        "From": sender,
        "To": TWILIO_NUMBER,
        "Body": body,
        "NumMedia": "0",
    }
    params.update(extra)
    return params


def post_inbound(
    client: TestClient, params: Mapping[str, str], *, signature: str | None = None
) -> Any:
    path = "/webhooks/twilio/messages"
    headers = {
        "X-Twilio-Signature": signature if signature is not None else twilio_signature(path, params)
    }
    return client.post(path, data=dict(params), headers=headers)


def post_status(client: TestClient, params: Mapping[str, str]) -> Any:
    path = "/webhooks/twilio/status"
    return client.post(
        path, data=dict(params), headers={"X-Twilio-Signature": twilio_signature(path, params)}
    )


def visible_text(response: Any) -> str:
    return re.sub(r"\s+", " ", soup_of(response).get_text(" "))
