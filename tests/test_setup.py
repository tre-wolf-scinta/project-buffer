"""The one-time browser page that creates the owner account."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from project_buffer.infrastructure.db.models import AuditEvent, User
from project_buffer.services.container import Services
from tests.conftest import (
    BASE_URL,
    OWNER_PASSWORD,
    OWNER_USERNAME,
    csrf_from,
    make_settings,
    visible_text,
)
from tests.test_accessibility import audit

NEW_PASSWORD = "a-long-fictional-passphrase"


def _open(services: Services) -> None:
    services.settings = make_settings(
        database_url=services.settings.database_url, allow_owner_setup=True
    )


def _submit(client: TestClient, **overrides: str) -> Any:
    page = client.get("/setup")
    assert page.status_code == 200, page.text
    data = {
        "username": "newowner",
        "password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
        "csrf_token": csrf_from(page),
    }
    data.update(overrides)
    return client.post("/setup", data=data)


def _user_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User)) or 0


def test_setup_is_closed_by_default(client: TestClient, db: Session) -> None:
    assert client.get("/setup").status_code == 404
    assert client.post("/setup", data={"username": "x"}).status_code in (403, 404)
    assert client.get("/login").status_code == 200
    assert _user_count(db) == 0


def test_setup_is_closed_once_an_account_exists(
    client: TestClient, services: Services, owner: User, db: Session
) -> None:
    _open(services)
    assert client.get("/setup").status_code == 404
    assert client.get("/login").status_code == 200
    # Even with a valid form token from elsewhere, nothing is created.
    token = csrf_from(client.get("/beta"))
    client.cookies.set("pb_public_form", token)
    response = client.post(
        "/setup",
        data={
            "username": "intruder",
            "password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
            "csrf_token": token,
        },
    )
    assert response.status_code == 404
    assert _user_count(db) == 1


def test_login_points_to_setup_while_it_is_open(client: TestClient, services: Services) -> None:
    _open(services)
    response = client.get("/login")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_setup_creates_the_owner_then_closes(
    client: TestClient, services: Services, db: Session
) -> None:
    _open(services)
    response = _submit(client, username="  NewOwner ")
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/account/mfa/setup"

    user = db.scalars(select(User)).one()
    assert user.username == "newowner"
    assert NEW_PASSWORD not in user.password_hash
    assert user.totp_enabled_at is None
    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "owner_created")).one()
    assert event.actor == "web_setup"
    assert NEW_PASSWORD not in str(event.detail)

    # The new session goes straight into two-step setup and nowhere else yet.
    page = client.get("/account/mfa/setup")
    assert page.status_code == 200
    assert audit(page.text) == []
    assert client.get("/inbox").status_code == 303

    # The page is gone for good, for this browser and any other.
    assert client.get("/setup").status_code == 404
    with TestClient(client.app, base_url=BASE_URL, follow_redirects=False) as other:
        assert other.get("/setup").status_code == 404
        assert other.get("/login").status_code == 200
    assert _user_count(db) == 1


def test_setup_rejects_bad_input_without_creating_anything(
    client: TestClient, services: Services, db: Session
) -> None:
    _open(services)
    mismatch = _submit(client, confirm_password="a-different-passphrase")
    assert mismatch.status_code == 400
    assert "do not match" in visible_text(mismatch)

    short = _submit(client, password="short", confirm_password="short")
    assert short.status_code == 400
    assert "at least 12 characters" in visible_text(short)

    bad_name = _submit(client, username="a b")
    assert bad_name.status_code == 400
    assert "Choose a username" in visible_text(bad_name)

    for response in (mismatch, short, bad_name):
        assert audit(response.text) == []
        assert response.text.count('role="alert"') == 1
        # Passwords are never echoed back into the page.
        assert NEW_PASSWORD not in response.text
    assert _user_count(db) == 0


def test_setup_requires_the_form_token_and_same_origin(
    client: TestClient, services: Services, db: Session
) -> None:
    _open(services)
    assert _submit(client, csrf_token="wrong").status_code == 403

    page = client.get("/setup")
    response = client.post(
        "/setup",
        data={
            "username": "newowner",
            "password": NEW_PASSWORD,
            "confirm_password": NEW_PASSWORD,
            "csrf_token": csrf_from(page),
        },
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert _user_count(db) == 0


def test_setup_page_is_accessible(client: TestClient, services: Services) -> None:
    _open(services)
    page = client.get("/setup")
    assert audit(page.text) == []
    text = visible_text(page)
    assert "Create your sign-in" in text
    assert 'autocomplete="new-password"' in page.text
    assert 'autocomplete="username"' in page.text
    assert page.headers["cache-control"].startswith("no-store")


def test_existing_owner_can_still_sign_in_when_the_flag_is_left_on(
    client: TestClient, services: Services, owner: User
) -> None:
    _open(services)
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": OWNER_USERNAME,
            "password": OWNER_PASSWORD,
            "csrf_token": csrf_from(page),
        },
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login/verify")
