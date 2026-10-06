"""Authentication, authorization, CSRF, headers, logging hygiene, encryption."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from project_buffer.domain.analysis import DraftReplyResult
from project_buffer.infrastructure.crypto import (
    AesGcmEncryptionService,
    DecryptionError,
    generate_key,
)
from project_buffer.infrastructure.db.models import AuditEvent, Draft, Message, User, UserSession
from project_buffer.logging_setup import configure_logging
from project_buffer.services import auth
from project_buffer.services.container import Services
from project_buffer.services.drafts import body_digest
from project_buffer.worker import drain
from tests.conftest import (
    OWNER,
    OWNER_PASSWORD,
    OWNER_USERNAME,
    TOTP_SECRET,
    FakeSms,
    ScriptedLLM,
    analysis,
    csrf_from,
    inbound_params,
    make_settings,
    post_inbound,
    sign_in,
    visible_text,
)

MARKER = "WALRUS-MARKER-5567"
RAW = f"You are a worthless excuse for a parent. {MARKER}. Pick up Riley at 5 on Friday."

PROTECTED_GETS = [
    "/",
    "/inbox",
    "/inbox/status",
    "/timeline",
    "/search",
    "/drafts",
    "/compose",
    "/export",
    "/account",
    f"/messages/{uuid.uuid4()}",
    f"/messages/{uuid.uuid4()}/original",
    f"/messages/{uuid.uuid4()}/reply",
    f"/drafts/{uuid.uuid4()}",
    f"/drafts/{uuid.uuid4()}/review",
]
PROTECTED_POSTS = [
    "/search",
    "/compose",
    "/export",
    "/account/password",
    "/account/recovery-codes",
    "/account/mfa/reset",
    "/account/sessions/revoke",
    f"/messages/{uuid.uuid4()}/handled",
    f"/messages/{uuid.uuid4()}/urgency",
    f"/messages/{uuid.uuid4()}/reprocess",
    f"/messages/{uuid.uuid4()}/original",
    f"/messages/{uuid.uuid4()}/reply",
    f"/messages/{uuid.uuid4()}/retry",
    f"/messages/{uuid.uuid4()}/attachments/{uuid.uuid4()}",
    f"/drafts/{uuid.uuid4()}",
    f"/drafts/{uuid.uuid4()}/send",
]


def _processed_message(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> Message:
    llm.analyses.append(
        analysis(
            short_summary="Jordan asks you to pick Riley up at 5 PM on Friday.",
            topic="Friday pickup",
            requests=["Pick Riley up at 5 PM on Friday."],
            response_needed=True,
            omitted_content_present=True,
            omitted_content_categories=["insults"],
        )
    )
    post_inbound(client, inbound_params(RAW))
    drain(services)
    return db.scalars(select(Message)).one()


# --- authentication and authorization -------------------------------------------------------


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_unauthenticated_get_redirects_to_sign_in(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", PROTECTED_POSTS)
def test_unauthenticated_post_is_refused(client: TestClient, path: str) -> None:
    response = client.post(path, data={"csrf_token": "x"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", ["/inbox", "/timeline", "/account", "/export", "/search"])
def test_password_alone_is_not_enough(client: TestClient, owner: User, path: str) -> None:
    """A session that has not passed the second factor reaches nothing protected."""
    sign_in(client, complete_mfa=False)
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login/verify")


def test_wrong_password_and_unknown_user_look_the_same(client: TestClient, owner: User) -> None:
    page = client.get("/login")
    token = csrf_from(page)
    wrong = client.post(
        "/login", data={"username": OWNER_USERNAME, "password": "nope", "csrf_token": token}
    )
    unknown = client.post(
        "/login", data={"username": "nobody", "password": "nope", "csrf_token": token}
    )
    assert wrong.status_code == unknown.status_code == 401
    message = "The username or password is incorrect."
    assert message in wrong.text and message in unknown.text


def test_login_is_rate_limited(client: TestClient, owner: User, services: Services) -> None:
    token = csrf_from(client.get("/login"))
    for _ in range(services.settings.login_max_failures_per_ip):
        client.post(
            "/login", data={"username": OWNER_USERNAME, "password": "x", "csrf_token": token}
        )
    blocked = client.post(
        "/login",
        data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD, "csrf_token": token},
    )
    assert blocked.status_code == 429
    assert "Too many failed attempts" in blocked.text


def test_totp_code_cannot_be_replayed(services: Services, owner: User, db: Session) -> None:
    user = db.get(User, owner.id)
    now = datetime.now(UTC)
    code = pyotp.TOTP(TOTP_SECRET).at(now)
    assert auth.verify_totp(services.crypto, user, code, now) is True
    assert auth.verify_totp(services.crypto, user, code, now) is False
    assert auth.verify_totp(services.crypto, user, "000000", now) is False


def test_wrong_second_factor_is_refused(client: TestClient, owner: User) -> None:
    token = sign_in(client, complete_mfa=False)
    response = client.post("/login/verify", data={"code": "123456", "csrf_token": token})
    assert response.status_code == 401
    assert client.get("/inbox").status_code == 303


def test_recovery_code_works_once(client: TestClient, owner: User, db: Session) -> None:
    user = db.get(User, owner.id)
    codes = auth.regenerate_recovery_codes(db, user)
    db.commit()
    token = sign_in(client, complete_mfa=False)
    ok = client.post("/login/verify", data={"recovery_code": codes[0], "csrf_token": token})
    assert ok.status_code == 303 and client.get("/inbox").status_code == 200
    db.expire_all()
    assert auth.remaining_recovery_codes(db, user) == 9
    assert auth.use_recovery_code(db, user, codes[0], datetime.now(UTC)) is False


def test_new_account_is_forced_through_mfa_setup(
    client: TestClient, services: Services, db: Session
) -> None:
    auth.create_owner(db, "fresh", "a long enough password")
    db.commit()
    token = csrf_from(client.get("/login"))
    response = client.post(
        "/login",
        data={"username": "fresh", "password": "a long enough password", "csrf_token": token},
    )
    assert response.headers["location"] == "/account/mfa/setup"
    assert client.get("/inbox").headers["location"] == "/account/mfa/setup"

    page = client.get("/account/mfa/setup")
    link = next(
        a["href"]
        for a in BeautifulSoup(page.text, "html.parser").find_all("a")
        if a["href"].startswith("otpauth://")
    )
    secret = link.split("secret=")[1].split("&")[0]
    done = client.post(
        "/account/mfa/setup",
        data={"code": pyotp.TOTP(secret).now(), "csrf_token": csrf_from(page)},
    )
    assert done.status_code == 200 and "Save your recovery codes" in done.text
    assert client.get("/inbox").status_code == 200


def test_sign_out_ends_the_session(auth_client: TestClient) -> None:
    response = auth_client.post("/logout", data={"csrf_token": auth_client.csrf})
    assert response.status_code == 303
    assert auth_client.get("/inbox").status_code == 303


def test_idle_and_expired_sessions_are_rejected(
    auth_client: TestClient, db: Session, services: Services
) -> None:
    record = db.scalars(select(UserSession)).one()
    record.last_seen_at = datetime.now(UTC) - timedelta(
        minutes=services.settings.session_idle_minutes + 1
    )
    db.commit()
    assert auth_client.get("/inbox").status_code == 303


def test_password_change_signs_out_other_devices(
    auth_client: TestClient, services: Services, db: Session
) -> None:
    other = TestClient(auth_client.app, base_url="https://buffer.test", follow_redirects=False)
    # Step the clock forward so the second sign-in uses a fresh TOTP time-step.
    user = db.scalars(select(User)).one()
    user.totp_last_counter = None
    db.commit()
    sign_in(other)
    assert other.get("/inbox").status_code == 200
    response = auth_client.post(
        "/account/password",
        data={
            "current_password": OWNER_PASSWORD,
            "new_password": "an entirely new password",
            "confirm_password": "an entirely new password",
            "csrf_token": auth_client.csrf,
        },
    )
    assert response.status_code == 303
    assert auth_client.get("/inbox").status_code == 200
    assert other.get("/inbox").status_code == 303


def test_session_cookie_flags(client: TestClient, owner: User) -> None:
    token = csrf_from(client.get("/login"))
    response = client.post(
        "/login",
        data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD, "csrf_token": token},
    )
    cookie = next(
        value
        for name, value in response.headers.multi_items()
        if name == "set-cookie" and "pb_session" in value
    )
    assert cookie.startswith("__Host-pb_session=")
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert flag in cookie
    # Only a hash of the token is stored server-side.
    raw = cookie.split("=", 1)[1].split(";")[0]
    with client.app.state.services.session_factory() as session:
        stored = session.scalars(select(UserSession)).one()
        assert stored.token_hash != raw and len(stored.token_hash) == 64


def test_open_redirect_is_not_possible(client: TestClient, owner: User) -> None:
    token = csrf_from(client.get("/login"))
    client.post(
        "/login",
        data={
            "username": OWNER_USERNAME,
            "password": OWNER_PASSWORD,
            "csrf_token": token,
            "next": "https://evil.example/",
        },
    )
    page = client.get("/login/verify")
    response = client.post(
        "/login/verify",
        data={
            "code": pyotp.TOTP(TOTP_SECRET).now(),
            "csrf_token": csrf_from(page),
            "next": "//evil.example/",
        },
    )
    assert response.headers["location"] == "/inbox"


# --- CSRF -----------------------------------------------------------------------------------


def test_post_without_csrf_token_is_refused(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    message = _processed_message(auth_client, db, services, llm)
    for data in ({}, {"csrf_token": "wrong"}):
        response = auth_client.post(f"/messages/{message.id}/handled", data=data)
        assert response.status_code == 403
    db.expire_all()
    assert db.get(Message, message.id).handled_at is None


def test_cross_origin_post_is_refused_even_with_a_valid_token(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    message = _processed_message(auth_client, db, services, llm)
    response = auth_client.post(
        f"/messages/{message.id}/handled",
        data={"csrf_token": auth_client.csrf},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_login_form_requires_its_csrf_cookie(client: TestClient, owner: User) -> None:
    response = client.post(
        "/login",
        data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD, "csrf_token": "guess"},
    )
    assert response.status_code == 403


# --- original text exposure -----------------------------------------------------------------


def test_original_text_appears_nowhere_by_default(
    auth_client: TestClient,
    db: Session,
    services: Services,
    llm: ScriptedLLM,
    sms: FakeSms,
) -> None:
    message = _processed_message(auth_client, db, services, llm)
    llm.drafts.append(DraftReplyResult(message_text="Yes, I can.", notes_for_owner=[]))
    auth_client.post(
        f"/messages/{message.id}/reply",
        data={"instruction": "say yes", "csrf_token": auth_client.csrf},
    )
    draft = db.scalars(select(Draft)).one()

    pages = [
        auth_client.get("/inbox"),
        auth_client.get("/inbox?filter=all"),
        auth_client.get("/inbox?filter=needs_response"),
        auth_client.get("/inbox/status"),
        auth_client.get("/timeline"),
        auth_client.get(f"/messages/{message.id}"),
        auth_client.get(f"/messages/{message.id}/original"),  # the warning page only
        auth_client.get(f"/messages/{message.id}/reply"),
        auth_client.get(f"/drafts/{draft.id}"),
        auth_client.get(f"/drafts/{draft.id}/review"),
        auth_client.get("/drafts"),
        auth_client.post("/search", data={"q": "pickup", "csrf_token": auth_client.csrf}),
    ]
    for page in pages:
        assert page.status_code == 200
        for fragment in (MARKER, "worthless", "excuse for a parent"):
            assert fragment not in page.text, f"{fragment!r} leaked on {page.url}"
    # Searching originals echoes the owner's own search term and nothing else.
    opted_in = auth_client.post(
        "/search", data={"q": MARKER, "include_originals": "1", "csrf_token": auth_client.csrf}
    )
    assert opted_in.text.count(MARKER) == 2  # the search box and the results heading
    assert "worthless" not in opted_in.text and "excuse for a parent" not in opted_in.text
    for sent in sms.sent:
        assert MARKER not in sent["body"] and "worthless" not in sent["body"]
    # Nothing in the request context for drafting carries it either.
    assert MARKER not in (llm.draft_requests[0].context_summary or "")


def test_original_requires_deliberate_confirmation_and_is_audited(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    message = _processed_message(auth_client, db, services, llm)

    warning = auth_client.get(f"/messages/{message.id}/original")
    assert "You're about to view the unfiltered original message." in visible_text(warning)
    assert MARKER not in warning.text

    # Posting without the confirmation field shows nothing.
    unconfirmed = auth_client.post(
        f"/messages/{message.id}/original", data={"csrf_token": auth_client.csrf}
    )
    assert unconfirmed.status_code == 303
    assert db.scalars(select(AuditEvent).where(AuditEvent.action == "original_viewed")).all() == []

    shown = auth_client.post(
        f"/messages/{message.id}/original",
        data={"csrf_token": auth_client.csrf, "confirm": "yes"},
    )
    assert shown.status_code == 200 and RAW in visible_text(shown)
    assert shown.headers["cache-control"] == "no-store"
    # The page title gives nothing away.
    assert "<title>Original message - Buffer</title>" in shown.text
    db.expire_all()
    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "original_viewed")).one()
    assert event.subject_id == str(message.id)


def test_search_defaults_to_sanitized_content(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    _processed_message(auth_client, db, services, llm)
    token = auth_client.csrf
    by_summary = auth_client.post("/search", data={"q": "friday pickup", "csrf_token": token})
    assert "1 result" in visible_text(by_summary)
    # A word that exists only in the original finds nothing by default...
    hidden = auth_client.post("/search", data={"q": MARKER, "csrf_token": token})
    assert "0 results" in visible_text(hidden)
    # ...and with the explicit choice, the match is reported without the text.
    opted = auth_client.post(
        "/search", data={"q": MARKER, "include_originals": "1", "csrf_token": token}
    )
    text = visible_text(opted)
    assert "1 result" in text and "matched in its original text only" in text
    assert text.count(MARKER) == 1  # only the echoed search term
    assert "worthless" not in text


def test_raw_message_text_never_reaches_the_logs(
    auth_client: TestClient,
    db: Session,
    services: Services,
    llm: ScriptedLLM,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    message = _processed_message(auth_client, db, services, llm)
    # Exercise failure paths too: a guard rejection and a provider error.
    llm.analyses.append(analysis(short_summary=f"He called you worthless. {MARKER}"))
    llm.analyses.append(analysis(short_summary=f"Still echoing {MARKER} you idiot"))
    auth_client.post(f"/messages/{message.id}/reprocess", data={"csrf_token": auth_client.csrf})
    drain(services)
    auth_client.post(
        f"/messages/{message.id}/original",
        data={"csrf_token": auth_client.csrf, "confirm": "yes"},
    )
    auth_client.post(
        "/search", data={"q": MARKER, "include_originals": "1", "csrf_token": auth_client.csrf}
    )

    assert caplog.records, "expected the application to log something"
    for record in caplog.records:
        line = record.getMessage()
        assert MARKER not in line and "worthless" not in line, line
        assert OWNER_PASSWORD not in line


def test_log_filter_masks_phone_numbers(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logging.getLogger("test").info("sending to %s now", OWNER)
    captured = capsys.readouterr().err
    assert OWNER not in captured and OWNER[-4:] in captured


# --- headers ----------------------------------------------------------------------------------


def test_security_headers_are_set(auth_client: TestClient) -> None:
    for response in (auth_client.get("/inbox"), auth_client.get("/login")):
        headers = response.headers
        assert "default-src 'none'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "'unsafe-inline'" not in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cache-control"] == "no-store"
        assert "max-age=" in headers["strict-transport-security"]


def test_pages_have_no_inline_script_or_style(auth_client: TestClient) -> None:
    page = auth_client.get("/inbox").text
    assert "<script>" not in page and "<style" not in page and " style=" not in page
    assert " onclick=" not in page


def test_unknown_message_id_is_a_plain_404(auth_client: TestClient) -> None:
    assert auth_client.get(f"/messages/{uuid.uuid4()}").status_code == 404
    assert auth_client.get("/messages/not-a-uuid").status_code == 404


# --- configuration and encryption --------------------------------------------------------------


def test_production_configuration_refuses_unsafe_settings() -> None:
    with pytest.raises(ValidationError) as raised:
        make_settings(environment="production", application_base_url="http://example.com")
    text = str(raised.value)
    for problem in (
        "APPLICATION_BASE_URL must be https",
        "DATABASE_URL must be PostgreSQL",
        "SMS_PROVIDER must be twilio",
        "LLM_PROVIDER must be anthropic or openai",
    ):
        assert problem in text


def test_configuration_rejects_short_secret_and_shared_numbers() -> None:
    with pytest.raises(ValidationError):
        make_settings(secret_key="short")
    with pytest.raises(ValidationError):
        make_settings(owner_phone_number="+12025550101")
    with pytest.raises(ValidationError):
        make_settings(coparent_phone_number="not-a-number")


def test_render_database_url_is_normalized() -> None:
    settings = make_settings(database_url="postgres://u:p@host:5432/db")
    assert settings.database_url == "postgresql+psycopg://u:p@host:5432/db"


def test_encryption_round_trip_binding_and_tamper_detection() -> None:
    service = AesGcmEncryptionService(generate_key())
    sealed = service.encrypt(b"original text", aad=b"message-body:1")
    assert b"original text" not in sealed
    assert service.decrypt(sealed, aad=b"message-body:1") == b"original text"
    assert service.encrypt(b"original text", aad=b"message-body:1") != sealed  # fresh nonce

    with pytest.raises(DecryptionError):  # ciphertext moved to another row
        service.decrypt(sealed, aad=b"message-body:2")
    tampered = sealed[:-1] + bytes([sealed[-1] ^ 1])
    with pytest.raises(DecryptionError):
        service.decrypt(tampered, aad=b"message-body:1")
    with pytest.raises(DecryptionError):
        AesGcmEncryptionService(generate_key()).decrypt(sealed, aad=b"message-body:1")


def test_retired_key_still_decrypts_after_rotation() -> None:
    old_key, new_key = generate_key(), generate_key()
    sealed = AesGcmEncryptionService(old_key).encrypt(b"kept", aad=b"a")
    rotated = AesGcmEncryptionService(new_key, [old_key])
    assert rotated.decrypt(sealed, aad=b"a") == b"kept"
    assert (
        AesGcmEncryptionService(new_key).decrypt(rotated.encrypt(b"new", aad=b"a"), aad=b"a")
        == b"new"
    )


def test_encryption_key_must_be_32_bytes() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        AesGcmEncryptionService("dG9vLXNob3J0")


def test_digest_of_reviewed_text_is_stable() -> None:
    assert body_digest("a") == body_digest("a") != body_digest("a ")
