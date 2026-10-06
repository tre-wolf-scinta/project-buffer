"""Request dependencies: database session, authentication, CSRF."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.infrastructure.db.models import Draft, Message, User, UserSession
from project_buffer.services import auth
from project_buffer.services.container import Services

PRELOGIN_CSRF_COOKIE = "pb_prelogin"
PUBLIC_FORM_CSRF_COOKIE = "pb_public_form"


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


def get_db(services: Services = Depends(get_services)) -> Iterator[Session]:
    """One session per request. Routes commit explicitly; anything else is rolled back."""
    session = services.session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@dataclass
class AuthContext:
    session: UserSession
    user: User
    # Plain copies, so templates (including error pages rendered after the database
    # session has closed) never touch a detached ORM object.
    csrf_token: str = ""
    username: str = ""
    fully_authenticated: bool = False

    def __post_init__(self) -> None:
        self.csrf_token = self.session.csrf_token
        self.username = self.user.username
        self.fully_authenticated = (
            self.user.mfa_enabled and self.session.mfa_verified_at is not None
        )


class RedirectTo(Exception):
    """Raised by dependencies to send the browser elsewhere."""

    def __init__(self, location: str) -> None:
        self.location = location


def _safe_next(request: Request) -> str:
    if request.method != "GET":
        return ""
    path = request.url.path
    return f"?next={path}" if path not in ("/", "/inbox") else ""


def optional_auth(
    request: Request, db: Session = Depends(get_db), services: Services = Depends(get_services)
) -> AuthContext | None:
    token = request.cookies.get(services.settings.session_cookie_name)
    record = auth.load_session(db, services.settings, token, utcnow())
    if record is None:
        return None
    db.commit()  # persists last_seen_at
    context = AuthContext(session=record, user=record.user)
    request.state.auth = context
    return context


def require_password_session(
    request: Request, context: AuthContext | None = Depends(optional_auth)
) -> AuthContext:
    """A session that has passed the password step (MFA may still be pending)."""
    if context is None:
        raise RedirectTo(f"/login{_safe_next(request)}")
    return context


def require_owner(
    request: Request, context: AuthContext = Depends(require_password_session)
) -> AuthContext:
    """A fully authenticated owner session. Every protected route depends on this."""
    if not context.user.mfa_enabled:
        raise RedirectTo("/account/mfa/setup")
    if context.session.mfa_verified_at is None:
        raise RedirectTo(f"/login/verify{_safe_next(request)}")
    return context


def _check_origin(request: Request, services: Services) -> None:
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return
    expected = urlparse(services.settings.application_base_url)
    actual = urlparse(origin)
    if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
        raise HTTPException(status_code=403, detail="Cross-origin request refused.")


async def csrf_protect(
    request: Request,
    context: AuthContext = Depends(require_password_session),
    services: Services = Depends(get_services),
) -> None:
    """Synchronizer-token CSRF check for authenticated form posts."""
    _check_origin(request, services)
    form = await request.form()
    submitted = form.get("csrf_token")
    if not isinstance(submitted, str) or not hmac.compare_digest(submitted, context.csrf_token):
        raise HTTPException(status_code=403, detail="This form has expired. Go back and retry.")


async def _double_submit(request: Request, services: Services, cookie_name: str) -> None:
    _check_origin(request, services)
    form = await request.form()
    submitted = form.get("csrf_token")
    cookie = request.cookies.get(cookie_name)
    if not cookie or not isinstance(submitted, str) or not hmac.compare_digest(submitted, cookie):
        raise HTTPException(status_code=403, detail="This form has expired. Reload and retry.")


async def prelogin_csrf_protect(
    request: Request, services: Services = Depends(get_services)
) -> None:
    """Double-submit CSRF check for the login form, which has no session yet."""
    await _double_submit(request, services, PRELOGIN_CSRF_COOKIE)


async def public_form_csrf_protect(
    request: Request, services: Services = Depends(get_services)
) -> None:
    """Double-submit CSRF check for public forms such as the beta request."""
    await _double_submit(request, services, PUBLIC_FORM_CSRF_COOKIE)


def get_message(message_id: uuid.UUID, db: Session, *, inbound_only: bool = False) -> Message:
    message = db.get(Message, message_id)
    if message is None or (inbound_only and not message.is_inbound):
        raise HTTPException(status_code=404, detail="Message not found.")
    return message


def get_draft(draft_id: uuid.UUID, db: Session) -> Draft:
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found.")
    return draft
