"""Template rendering with the context every page needs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from project_buffer.services import inbox
from project_buffer.web.deps import AuthContext

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# Fixed wording only. A notice code in a URL can never inject text into a page.
NOTICES = {
    "sent": "Message sent.",
    "already_sent": "That draft was already sent. It was not sent again.",
    "uncertain": (
        "The message may or may not have been sent. The app is checking with the carrier "
        "now. Do not send it again yet."
    ),
    "stale_review": "The draft changed after you reviewed it. Review it again before sending.",
    "draft_saved": "Draft saved.",
    "draft_discarded": "Draft discarded.",
    "draft_ai_failed": (
        "AI drafting is unavailable right now. You can write the message yourself below, "
        "or try redrafting later."
    ),
    "redrafted": "New draft written.",
    "handled": "Marked as handled.",
    "reopened": "Marked as not handled.",
    "urgency": "Urgency updated.",
    "reprocess": "Filtering has been queued again. This usually takes under a minute.",
    "reprocess_active": "Filtering is already in progress for this message.",
    "released": "The message is being processed now.",
    "password_changed": "Password changed. Other devices have been signed out.",
    "sessions_revoked": "All other devices have been signed out.",
    "signed_out": "You have been signed out.",
}


def render(
    request: Request,
    db: Session | None,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    services = request.app.state.services
    auth: AuthContext | None = getattr(request.state, "auth", None)
    data: dict[str, Any] = {
        "settings": services.settings,
        "auth": auth,
        "csrf_token": auth.csrf_token if auth else "",
        "notice": NOTICES.get(request.query_params.get("notice", "")),
        "nav": None,
    }
    if auth is not None and auth.fully_authenticated and db is not None:
        data["nav"] = {
            "unread": inbox.unread_count(db),
            "drafts": len(inbox.open_drafts(db)),
        }
    data.update(context or {})
    return templates.TemplateResponse(request, name, data, status_code=status_code)
