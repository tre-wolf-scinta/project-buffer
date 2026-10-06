"""Message detail, triage actions, and the opt-in original viewer."""

from __future__ import annotations

import mimetypes
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.domain.enums import (
    AttachmentStatus,
    DeliveryStatus,
    DraftStatus,
    ProcessingStatus,
    Urgency,
)
from project_buffer.infrastructure.db.models import Attachment, Draft, Message
from project_buffer.services import analysis, audit, inbox, media
from project_buffer.services.container import Services
from project_buffer.services.outbound import describe_error
from project_buffer.web.deps import (
    AuthContext,
    csrf_protect,
    get_db,
    get_message,
    get_services,
    require_owner,
)
from project_buffer.web.templating import render
from project_buffer.web.viewmodels import (
    OMITTED_LABELS,
    URGENCY_LABELS,
    analysis_of,
    build_card,
    detail_sections,
    flag_labels,
    format_when,
)

router = APIRouter(prefix="/messages")


@router.get("/{message_id}")
def message_detail(
    request: Request,
    message_id: uuid.UUID,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    message = get_message(message_id, db)
    now = utcnow()
    settings = services.settings
    card = build_card(message, settings, now)

    if not message.is_inbound:
        # The owner's own sent text is not filtered content, so it is shown directly.
        body = analysis.decrypt_body(services, message)
        failed = message.delivery_status in (DeliveryStatus.FAILED, DeliveryStatus.UNDELIVERED)
        parent = db.get(Message, message.in_reply_to_id) if message.in_reply_to_id else None
        return render(
            request,
            db,
            "message_outbound.html",
            {
                "card": card,
                "body": body,
                "message": message,
                "failure_reason": describe_error(message.delivery_error_code) if failed else None,
                "events": [
                    (format_when(e.received_at, settings.timezone, now), e.status)
                    for e in message.delivery_events
                ],
                "in_reply_to": build_card(parent, settings, now) if parent else None,
            },
        )

    if message.processing_status != ProcessingStatus.QUARANTINED and message.read_at is None:
        inbox.mark_read(message, now)
        db.commit()
        card.unread = False

    result = analysis_of(message) if card.state == "processed" else None
    replies = db.scalars(
        select(Message).where(Message.in_reply_to_id == message.id).order_by(Message.occurred_at)
    ).all()
    open_draft = db.scalar(
        select(Draft).where(Draft.in_reply_to_id == message.id, Draft.status == DraftStatus.READY)
    )
    current = message.current_analysis
    return render(
        request,
        db,
        "message_detail.html",
        {
            "card": card,
            "message": message,
            "result": result,
            "sections": detail_sections(result) if result else [],
            "flags": flag_labels(result) if result else [],
            "omitted_labels": (
                [OMITTED_LABELS[c] for c in result.omitted_content_categories] if result else []
            ),
            "urgency_options": [(u.value, URGENCY_LABELS[u]) for u in Urgency],
            "safety_keywords": bool(current and current.safety_keywords_detected),
            "replies": [build_card(r, settings, now) for r in replies],
            "open_draft": open_draft,
        },
    )


@router.post("/{message_id}/handled", dependencies=[Depends(csrf_protect)])
def set_handled(
    message_id: uuid.UUID,
    handled: str = Form("1"),
    return_to: str = Form(""),
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    message = get_message(message_id, db, inbound_only=True)
    is_handled = handled == "1"
    inbox.set_handled(db, message, is_handled, utcnow())
    db.commit()
    notice = "handled" if is_handled else "reopened"
    target = "/inbox" if return_to == "inbox" else f"/messages/{message_id}"
    return RedirectResponse(f"{target}?notice={notice}", status_code=303)


@router.post("/{message_id}/urgency", dependencies=[Depends(csrf_protect)])
def set_urgency(
    message_id: uuid.UUID,
    urgency: str = Form(...),
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    message = get_message(message_id, db, inbound_only=True)
    try:
        level = Urgency(urgency)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown urgency level.") from None
    inbox.override_urgency(db, message, level)
    db.commit()
    return RedirectResponse(f"/messages/{message_id}?notice=urgency", status_code=303)


@router.post("/{message_id}/reprocess", dependencies=[Depends(csrf_protect)])
def reprocess(
    message_id: uuid.UUID,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    message = get_message(message_id, db, inbound_only=True)
    was_quarantined = message.processing_status == ProcessingStatus.QUARANTINED
    queued = analysis.request_reprocess(db, services, message, utcnow())
    db.commit()
    notice = "reprocess_active" if not queued else ("released" if was_quarantined else "reprocess")
    return RedirectResponse(f"/messages/{message_id}?notice={notice}", status_code=303)


# --- originals ---------------------------------------------------------------------
# GET shows only a warning. The original is returned solely in response to a POST,
# so it can never be reached by following a link, a prefetch, or browser history.


@router.get("/{message_id}/original")
def original_warning(
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
        "original_warning.html",
        {"card": build_card(message, services.settings, utcnow()), "message": message},
    )


@router.post("/{message_id}/original", dependencies=[Depends(csrf_protect)])
def original_view(
    request: Request,
    message_id: uuid.UUID,
    confirm: str = Form(""),
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    message = get_message(message_id, db, inbound_only=True)
    if confirm != "yes":
        return RedirectResponse(f"/messages/{message_id}/original", status_code=303)
    body = analysis.decrypt_body(services, message)
    audit.record(
        db, actor="owner", action="original_viewed", subject_type="message", subject_id=message.id
    )
    db.commit()
    return render(
        request,
        db,
        "original_view.html",
        {
            "card": build_card(message, services.settings, utcnow()),
            "message": message,
            "body": body,
            "attachments": message.attachments,
            "stored": AttachmentStatus.STORED,
        },
    )


@router.post("/{message_id}/attachments/{attachment_id}", dependencies=[Depends(csrf_protect)])
def download_attachment(
    message_id: uuid.UUID,
    attachment_id: uuid.UUID,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    """Attachments are unfiltered content, reachable only from the original view."""
    attachment = db.get(Attachment, attachment_id)
    if attachment is None or attachment.message_id != message_id:
        raise HTTPException(status_code=404)
    try:
        content = media.read_attachment(db, services, attachment)
    except KeyError:
        raise HTTPException(status_code=404) from None
    audit.record(
        db,
        actor="owner",
        action="attachment_downloaded",
        subject_type="attachment",
        subject_id=attachment.id,
    )
    db.commit()
    extension = mimetypes.guess_extension(attachment.content_type) or ".bin"
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="attachment-{attachment.position + 1}{extension}"'
            )
        },
    )
