"""Public pages that need no sign-in.

US carriers require a text-messaging sender to publish how people opt in, a
privacy policy and terms. These pages describe the service honestly and contain
nothing private.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.services import beta
from project_buffer.services.container import Services
from project_buffer.web.deps import (
    PUBLIC_FORM_CSRF_COOKIE,
    AuthContext,
    client_ip,
    get_db,
    get_services,
    public_form_csrf_protect,
    require_owner,
)
from project_buffer.web.templating import render

router = APIRouter()


def _context(request: Request) -> dict[str, Any]:
    settings = request.app.state.services.settings
    return {
        "brand": settings.sms_brand_name or "the operator of this service",
        "number": settings.twilio_phone_number,
    }


@router.get("/privacy")
def privacy(request: Request) -> Response:
    return render(request, None, "privacy.html", _context(request))


@router.get("/terms")
def terms(request: Request) -> Response:
    return render(request, None, "terms.html", _context(request))


def _beta_page(
    request: Request,
    services: Services,
    *,
    error: str | None = None,
    done: bool = False,
    values: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    token = request.cookies.get(PUBLIC_FORM_CSRF_COOKIE) or secrets.token_urlsafe(32)
    response = render(
        request,
        None,
        "beta.html",
        {
            **_context(request),
            "csrf_token": token,
            "consent_text": beta.consent_text(services.settings),
            "error": error,
            "done": done,
            "values": values or {},
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
        path="/beta",
    )
    return response


@router.get("/beta")
def beta_form(request: Request, services: Services = Depends(get_services)) -> Response:
    return _beta_page(request, services)


@router.post("/beta", dependencies=[Depends(public_form_csrf_protect)])
def beta_request(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    sms_consent: str = Form(""),
    website: str = Form(""),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    """Record a request for beta access. Sends no text or email."""
    now, ip = utcnow(), client_ip(request)
    values = {"name": name[:80], "email": email[:254], "phone": phone[:32]}
    if website:
        # A field people cannot see was filled in: an automated submission. Store nothing.
        return _beta_page(request, services, done=True)
    if beta.is_throttled(db, ip, now):
        return _beta_page(
            request,
            services,
            error="Too many requests from this connection. Try again in an hour.",
            values=values,
            status_code=429,
        )
    try:
        details = beta.validate(name, email, phone, sms_consent == "yes")
    except beta.SignupError as exc:
        return _beta_page(request, services, error=str(exc), values=values, status_code=400)
    beta.record_signup(db, services, details, ip=ip, now=now)
    db.commit()
    # The same confirmation whether or not this number had already asked.
    return _beta_page(request, services, done=True)


@router.get("/account/beta-requests")
def beta_requests(
    request: Request,
    _context_auth: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    return render(request, db, "beta_requests.html", {"requests": beta.list_signups(db, services)})
