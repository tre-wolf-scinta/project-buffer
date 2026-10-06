"""Inbox, timeline and search. Sanitized content only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.services import audit, inbox
from project_buffer.services.container import Services
from project_buffer.services.inbox import FILTER_LABELS, InboxFilter
from project_buffer.web.deps import AuthContext, csrf_protect, get_db, get_services, require_owner
from project_buffer.web.templating import render
from project_buffer.web.viewmodels import build_card

router = APIRouter()


@router.get("/")
def root(_context: AuthContext = Depends(require_owner)) -> Response:
    return RedirectResponse("/inbox", status_code=303)


@router.get("/inbox")
def inbox_page(
    request: Request,
    filter: str = InboxFilter.OPEN.value,
    page: int = 1,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    try:
        which = InboxFilter(filter)
    except ValueError:
        which = InboxFilter.OPEN
    page = max(page, 1)
    now = utcnow()
    messages, has_more = inbox.list_inbox(db, which, page)
    counts = inbox.filter_counts(db)
    visible = [f for f in InboxFilter if f != InboxFilter.QUARANTINED or counts[f]]
    return render(
        request,
        db,
        "inbox.html",
        {
            "cards": [build_card(m, services.settings, now) for m in messages],
            "active": which,
            "active_label": FILTER_LABELS[which],
            "filters": [(f, FILTER_LABELS[f], counts[f]) for f in visible],
            "page": page,
            "has_more": has_more,
            "drafts": inbox.open_drafts(db),
            "unread": counts[InboxFilter.UNREAD],
        },
    )


@router.get("/inbox/status")
def inbox_status(
    _context: AuthContext = Depends(require_owner), db: Session = Depends(get_db)
) -> JSONResponse:
    """Polled by the inbox page to announce new messages without a reload."""
    return JSONResponse({"unread": inbox.unread_count(db)})


@router.get("/timeline")
def timeline(
    request: Request,
    page: int = 1,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    page = max(page, 1)
    now = utcnow()
    messages, has_more = inbox.list_timeline(db, page)
    return render(
        request,
        db,
        "timeline.html",
        {
            "cards": [build_card(m, services.settings, now) for m in messages],
            "page": page,
            "has_more": has_more,
        },
    )


@router.get("/search")
def search_form(
    request: Request,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    return render(
        request, db, "search.html", {"term": "", "include_originals": False, "results": None}
    )


# POST keeps search terms out of URLs, browser history and access logs.
@router.post("/search", dependencies=[Depends(csrf_protect)])
def search(
    request: Request,
    q: str = Form(""),
    include_originals: str = Form(""),
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    term = q.strip()[:200]
    originals = include_originals == "1"
    now = utcnow()
    hits = inbox.search(db, services, term, include_originals=originals)
    if originals and term:
        audit.record(db, actor="owner", action="originals_searched", result_count=len(hits))
        db.commit()
    return render(
        request,
        db,
        "search.html",
        {
            "term": term,
            "include_originals": originals,
            "results": [
                (build_card(hit.message, services.settings, now), hit.matched_original_only)
                for hit in hits
            ],
        },
    )
