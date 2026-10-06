"""Public pages that need no sign-in.

US carriers require a text-messaging sender to publish a privacy policy and
terms. These pages describe this number honestly and contain nothing private.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from project_buffer.web.templating import render

router = APIRouter()


def _context(request: Request) -> dict[str, str]:
    settings = request.app.state.services.settings
    return {
        "brand": settings.sms_brand_name or "the account holder",
        "number": settings.twilio_phone_number,
    }


@router.get("/privacy")
def privacy(request: Request) -> Response:
    return render(request, None, "privacy.html", _context(request))


@router.get("/terms")
def terms(request: Request) -> Response:
    return render(request, None, "terms.html", _context(request))
