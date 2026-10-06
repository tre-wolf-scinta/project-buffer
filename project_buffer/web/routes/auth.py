"""Sign-in, second factor, sign-out."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.config import Settings
from project_buffer.domain.enums import AuthAttemptKind
from project_buffer.services import audit, auth
from project_buffer.services.container import Services
from project_buffer.web.deps import (
    PRELOGIN_CSRF_COOKIE,
    AuthContext,
    client_ip,
    csrf_protect,
    get_db,
    get_services,
    optional_auth,
    prelogin_csrf_protect,
    require_password_session,
)
from project_buffer.web.templating import render

router = APIRouter()

THROTTLED = "Too many failed attempts. Wait 15 minutes, then try again."


def safe_next(value: str | None) -> str:
    """Only same-site absolute paths are accepted as a post-login destination."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/inbox"


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_absolute_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _login_page(
    request: Request,
    services: Services,
    *,
    next_url: str,
    error: str | None = None,
    username: str = "",
    status_code: int = 200,
) -> Response:
    token = request.cookies.get(PRELOGIN_CSRF_COOKIE) or secrets.token_urlsafe(32)
    response = render(
        request,
        None,
        "login.html",
        {"csrf_token": token, "error": error, "next_url": next_url, "username": username},
        status_code=status_code,
    )
    response.set_cookie(
        PRELOGIN_CSRF_COOKIE,
        token,
        max_age=3600,
        httponly=True,
        secure=services.settings.cookie_secure,
        samesite="strict",
        path="/login",
    )
    return response


@router.get("/login")
def login_form(
    request: Request,
    next: str | None = None,
    context: AuthContext | None = Depends(optional_auth),
    services: Services = Depends(get_services),
) -> Response:
    if context is not None:
        return RedirectResponse("/inbox", status_code=303)
    return _login_page(request, services, next_url=safe_next(next))


@router.post("/login", dependencies=[Depends(prelogin_csrf_protect)])
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/inbox"),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    settings, now, ip = services.settings, utcnow(), client_ip(request)
    next_url = safe_next(next)
    if auth.is_throttled(db, settings, ip, now):
        return _login_page(request, services, next_url=next_url, error=THROTTLED, status_code=429)
    user = auth.verify_password(db, username, password)
    auth.record_attempt(db, AuthAttemptKind.PASSWORD, ip, user is not None, now)
    if user is None:
        db.commit()
        return _login_page(
            request,
            services,
            next_url=next_url,
            error="The username or password is incorrect.",
            username=username[:64],
            status_code=401,
        )
    _record, token = auth.create_session(db, settings, user, now, mfa_verified=False)
    audit.record(db, actor="owner", action="password_accepted", ip=ip)
    db.commit()
    destination = "/login/verify" if user.mfa_enabled else "/account/mfa/setup"
    if user.mfa_enabled and next_url != "/inbox":
        destination += f"?next={next_url}"
    response = RedirectResponse(destination, status_code=303)
    set_session_cookie(response, settings, token)
    response.delete_cookie(PRELOGIN_CSRF_COOKIE, path="/login")
    return response


@router.get("/login/verify")
def verify_form(
    request: Request,
    next: str | None = None,
    context: AuthContext = Depends(require_password_session),
) -> Response:
    if not context.user.mfa_enabled:
        return RedirectResponse("/account/mfa/setup", status_code=303)
    if context.fully_authenticated:
        return RedirectResponse("/inbox", status_code=303)
    return render(request, None, "login_verify.html", {"next_url": safe_next(next), "error": None})


@router.post("/login/verify", dependencies=[Depends(csrf_protect)])
def verify(
    request: Request,
    code: str = Form(""),
    recovery_code: str = Form(""),
    next: str = Form("/inbox"),
    context: AuthContext = Depends(require_password_session),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    settings, now, ip = services.settings, utcnow(), client_ip(request)
    next_url = safe_next(next)
    if not context.user.mfa_enabled:
        return RedirectResponse("/account/mfa/setup", status_code=303)

    def fail(message: str, status_code: int) -> Response:
        return render(
            request,
            None,
            "login_verify.html",
            {"next_url": next_url, "error": message},
            status_code=status_code,
        )

    if auth.is_throttled(db, settings, ip, now):
        return fail(THROTTLED, 429)
    if recovery_code.strip():
        accepted = auth.use_recovery_code(db, context.user, recovery_code, now)
    else:
        accepted = auth.verify_totp(services.crypto, context.user, code, now)
    auth.record_attempt(db, AuthAttemptKind.TOTP, ip, accepted, now)
    if not accepted:
        db.commit()
        return fail("That code was not accepted. Check the code and try again.", 401)
    context.session.mfa_verified_at = now
    audit.record(db, actor="owner", action="signed_in", ip=ip)
    db.commit()
    return RedirectResponse(next_url, status_code=303)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(
    context: AuthContext = Depends(require_password_session),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    auth.revoke_session(context.session, utcnow())
    audit.record(db, actor="owner", action="signed_out")
    db.commit()
    response = RedirectResponse("/login?notice=signed_out", status_code=303)
    response.delete_cookie(services.settings.session_cookie_name, path="/")
    return response
