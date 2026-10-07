"""First-time creation of the single owner account, in the browser.

Open only while ALLOW_OWNER_SETUP is true AND no account exists. Once an
account exists these routes answer 404 for good, whatever the setting says.
"""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.infrastructure.db.models import User
from project_buffer.services import auth
from project_buffer.services.container import Services
from project_buffer.web.deps import (
    PUBLIC_FORM_CSRF_COOKIE,
    client_ip,
    get_db,
    get_services,
    public_form_csrf_protect,
)
from project_buffer.web.routes.auth import set_session_cookie
from project_buffer.web.templating import render

router = APIRouter()

_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


def setup_open(db: Session, services: Services) -> bool:
    if not services.settings.allow_owner_setup:
        return False
    return not db.scalar(select(func.count()).select_from(User))


def _page(
    request: Request,
    services: Services,
    *,
    error: str | None = None,
    username: str = "",
    status_code: int = 200,
) -> Response:
    token = request.cookies.get(PUBLIC_FORM_CSRF_COOKIE) or secrets.token_urlsafe(32)
    response = render(
        request,
        None,
        "setup.html",
        {
            "csrf_token": token,
            "error": error,
            "username": username,
            "min_length": auth.MIN_PASSWORD_LENGTH,
        },
        status_code=status_code,
    )
    response.set_cookie(
        PUBLIC_FORM_CSRF_COOKIE,
        token,
        max_age=3600,
        httponly=True,
        secure=services.settings.cookie_secure,
        samesite="strict",
        path="/setup",
    )
    return response


@router.get("/setup")
def setup_form(
    request: Request, db: Session = Depends(get_db), services: Services = Depends(get_services)
) -> Response:
    if not setup_open(db, services):
        raise HTTPException(status_code=404)
    return _page(request, services)


@router.post("/setup", dependencies=[Depends(public_form_csrf_protect)])
def setup(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    if not setup_open(db, services):
        raise HTTPException(status_code=404)
    settings, now = services.settings, utcnow()
    name = username.strip().lower()

    def fail(message: str) -> Response:
        return _page(request, services, error=message, username=name[:64], status_code=400)

    if not _USERNAME.match(name):
        return fail(
            "Choose a username of 3 to 64 characters: letters, digits, dots, dashes or underscores."
        )
    if password != confirm_password:
        return fail("The two passwords do not match.")
    try:
        user = auth.create_owner(db, name, password, actor="web_setup", ip=client_ip(request))
    except auth.AuthError as exc:
        db.rollback()
        return fail(str(exc))
    _record, token = auth.create_session(db, settings, user, now, mfa_verified=False)
    db.commit()
    # Straight into two-step setup. The account is not usable until that is done.
    response = RedirectResponse("/account/mfa/setup", status_code=303)
    set_session_cookie(response, settings, token)
    return response
