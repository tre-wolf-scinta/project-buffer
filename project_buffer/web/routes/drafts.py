"""Drafting, review and explicit approval of outbound messages."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.domain.enums import DraftStatus
from project_buffer.infrastructure.db.models import Draft
from project_buffer.services import drafts as draft_service
from project_buffer.services import inbox, outbound
from project_buffer.services.container import Services
from project_buffer.web.deps import (
    AuthContext,
    csrf_protect,
    get_db,
    get_draft,
    get_message,
    get_services,
    require_owner,
)
from project_buffer.web.templating import render
from project_buffer.web.viewmodels import build_card

router = APIRouter()


def _redirect(path: str, notice: str | None = None) -> Response:
    return RedirectResponse(f"{path}?notice={notice}" if notice else path, status_code=303)


def _closed_draft_redirect(draft: Draft) -> Response:
    """Where to go when a draft is no longer editable."""
    if draft.sent_message_id:
        return _redirect(f"/messages/{draft.sent_message_id}", "already_sent")
    return _redirect("/inbox")


@router.get("/drafts")
def draft_list(
    request: Request,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    return render(request, db, "drafts.html", {"drafts": inbox.open_drafts(db)})


@router.get("/compose")
def compose_form(
    request: Request,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    return render(request, db, "reply.html", {"card": None, "action": "/compose", "error": None})


@router.get("/messages/{message_id}/reply")
def reply_form(
    request: Request,
    message_id: uuid.UUID,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    message = get_message(message_id, db, inbound_only=True)
    return render(
        request,
        db,
        "reply.html",
        {
            "card": build_card(message, services.settings, utcnow()),
            "action": f"/messages/{message_id}/reply",
            "error": None,
        },
    )


def _create(
    request: Request,
    db: Session,
    services: Services,
    instruction: str,
    message_id: uuid.UUID | None,
) -> Response:
    message = get_message(message_id, db, inbound_only=True) if message_id else None
    now = utcnow()
    if not instruction.strip():
        return render(
            request,
            db,
            "reply.html",
            {
                "card": build_card(message, services.settings, now) if message else None,
                "action": request.url.path,
                "error": "Say what you want to tell them before continuing.",
            },
            status_code=400,
        )
    draft = draft_service.create_draft(
        db, services, instruction=instruction, in_reply_to=message, now=now
    )
    db.commit()
    return _redirect(f"/drafts/{draft.id}", None if draft.ai_draft_text else "draft_ai_failed")


@router.post("/compose", dependencies=[Depends(csrf_protect)])
def compose(
    request: Request,
    instruction: str = Form(""),
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    return _create(request, db, services, instruction, None)


@router.post("/messages/{message_id}/reply", dependencies=[Depends(csrf_protect)])
def reply(
    request: Request,
    message_id: uuid.UUID,
    instruction: str = Form(""),
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    return _create(request, db, services, instruction, message_id)


@router.post("/messages/{message_id}/retry", dependencies=[Depends(csrf_protect)])
def retry_failed(
    message_id: uuid.UUID,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    """Copy a sent message's text into a new draft. It still needs review and approval."""
    message = get_message(message_id, db)
    if message.is_inbound:
        return _redirect(f"/messages/{message_id}")
    draft = draft_service.copy_to_new_draft(db, services, message)
    db.commit()
    return _redirect(f"/drafts/{draft.id}")


def _draft_page(
    request: Request,
    db: Session,
    services: Services,
    draft: Draft,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        db,
        "draft.html",
        {
            "draft": draft,
            "card": (
                build_card(draft.in_reply_to, services.settings, utcnow())
                if draft.in_reply_to
                else None
            ),
            "max_length": draft_service.MAX_SMS_BODY,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/drafts/{draft_id}")
def draft_edit(
    request: Request,
    draft_id: uuid.UUID,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    draft = get_draft(draft_id, db)
    if draft.status != DraftStatus.READY:
        return _closed_draft_redirect(draft)
    return _draft_page(request, db, services, draft)


@router.post("/drafts/{draft_id}", dependencies=[Depends(csrf_protect)])
def draft_update(
    request: Request,
    draft_id: uuid.UUID,
    action: str = Form("save"),
    body: str = Form(""),
    instruction: str = Form(""),
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    draft = get_draft(draft_id, db)
    if draft.status != DraftStatus.READY:
        return _closed_draft_redirect(draft)

    if action == "discard":
        draft_service.discard(db, draft)
        db.commit()
        return _redirect("/inbox", "draft_discarded")

    if action == "redraft":
        if not instruction.strip():
            return _draft_page(
                request,
                db,
                services,
                draft,
                error="Say what you want to tell them, then choose Redraft.",
                status_code=400,
            )
        ok = draft_service.regenerate(db, services, draft, instruction, utcnow())
        db.commit()
        return _redirect(f"/drafts/{draft.id}", "redrafted" if ok else "draft_ai_failed")

    draft_service.update_body(draft, body)
    db.commit()
    if action == "review":
        problem = draft_service.validate_body(draft.body)
        if problem:
            return _draft_page(request, db, services, draft, error=problem, status_code=400)
        return _redirect(f"/drafts/{draft.id}/review")
    return _redirect(f"/drafts/{draft.id}", "draft_saved")


@router.get("/drafts/{draft_id}/review")
def draft_review(
    request: Request,
    draft_id: uuid.UUID,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    """Shows the exact text that will be sent. Only this page can send."""
    draft = get_draft(draft_id, db)
    if draft.status != DraftStatus.READY:
        return _closed_draft_redirect(draft)
    if draft_service.validate_body(draft.body):
        return _redirect(f"/drafts/{draft.id}")
    return render(
        request,
        db,
        "draft_review.html",
        {"draft": draft, "digest": draft_service.body_digest(draft.body)},
    )


@router.post("/drafts/{draft_id}/send", dependencies=[Depends(csrf_protect)])
def draft_send(
    draft_id: uuid.UUID,
    reviewed_digest: str = Form(""),
    approve: str = Form(""),
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    if approve != "yes":
        return _redirect(f"/drafts/{draft_id}/review")
    result = outbound.approve_and_send(
        db, services, draft_id=draft_id, reviewed_digest=reviewed_digest, now=utcnow()
    )
    outcome = result.outcome
    if outcome == outbound.SendOutcome.SENT:
        return _redirect(f"/messages/{result.message_id}", "sent")
    if outcome == outbound.SendOutcome.UNCERTAIN:
        return _redirect(f"/messages/{result.message_id}", "uncertain")
    if outcome == outbound.SendOutcome.FAILED:
        return _redirect(f"/messages/{result.message_id}")
    if outcome == outbound.SendOutcome.ALREADY_HANDLED:
        if result.message_id:
            return _redirect(f"/messages/{result.message_id}", "already_sent")
        draft = db.get(Draft, draft_id)
        if draft is not None and draft.sent_message_id:
            return _redirect(f"/messages/{draft.sent_message_id}", "already_sent")
        return _redirect("/inbox", "already_sent")
    if outcome == outbound.SendOutcome.STALE_REVIEW:
        return _redirect(f"/drafts/{draft_id}/review", "stale_review")
    return _redirect(f"/drafts/{draft_id}")
