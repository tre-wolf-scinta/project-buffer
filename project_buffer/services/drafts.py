"""Outbound drafts. Nothing here sends anything."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from project_buffer.domain.analysis import MessageAnalysisResult
from project_buffer.domain.enums import DraftStatus
from project_buffer.infrastructure.db.models import Draft, Message
from project_buffer.infrastructure.llm.base import DraftRequest, LLMError
from project_buffer.services import audit
from project_buffer.services.container import Services
from project_buffer.services.conversations import ensure_conversation

logger = logging.getLogger(__name__)

MAX_SMS_BODY = 1600
MAX_INSTRUCTION = 4000


def body_digest(body: str) -> str:
    """Digest of the exact text shown on the review page."""
    return hashlib.sha256(body.encode()).hexdigest()


def context_summary_for(message: Message | None) -> str | None:
    """Sanitized context for the drafting model. Never the original text."""
    analysis = message.current_analysis if message else None
    if analysis is None:
        return None
    result = MessageAnalysisResult.model_validate(analysis.structured)
    lines = [f"Topic: {result.topic}", f"Summary: {result.short_summary}"]
    for label, items in (
        ("Requests", result.requests),
        ("Questions", result.questions),
        ("Information", result.factual_information),
        ("Decisions needed", result.decisions_needed),
    ):
        if items:
            lines.append(f"{label}: " + "; ".join(items))
    for mention in result.dates_and_times:
        lines.append(f"Time: {mention.what} — {mention.when}")
    return "\n".join(lines)


def _generate(
    services: Services, instruction: str, in_reply_to: Message | None, now: datetime
) -> tuple[str | None, list[str]]:
    settings = services.settings
    request = DraftRequest(
        instruction=instruction,
        owner_name=settings.owner_display_name,
        recipient_name=settings.coparent_display_name,
        today_local=now.astimezone(settings.timezone).strftime("%A %Y-%m-%d"),
        children_names=settings.children,
        context_summary=context_summary_for(in_reply_to),
    )
    try:
        result = services.llm.draft_reply(request)
    except LLMError as exc:
        logger.warning("draft generation failed: %s", type(exc).__name__)
        return None, []
    return result.message_text, list(result.notes_for_owner)


def create_draft(
    session: Session,
    services: Services,
    *,
    instruction: str,
    in_reply_to: Message | None,
    now: datetime,
) -> Draft:
    """Create a draft from the owner's instruction. If drafting fails the draft is
    created empty so the owner can write the message by hand."""
    instruction = instruction.strip()[:MAX_INSTRUCTION]
    text, notes = _generate(services, instruction, in_reply_to, now) if instruction else (None, [])
    draft = Draft(
        conversation_id=ensure_conversation(session, services.settings).id,
        in_reply_to_id=in_reply_to.id if in_reply_to else None,
        instruction=instruction,
        ai_draft_text=text,
        ai_notes=notes,
        body=text or "",
    )
    session.add(draft)
    session.flush()
    audit.record(
        session,
        actor="owner",
        action="draft_created",
        subject_type="draft",
        subject_id=draft.id,
        ai_drafted=text is not None,
    )
    return draft


def copy_to_new_draft(session: Session, services: Services, sent: Message) -> Draft:
    """Start a new draft from the text of a message the owner already approved once."""
    body = services.crypto.decrypt(sent.body_ciphertext, aad=sent.body_aad()).decode()
    draft = Draft(
        conversation_id=sent.conversation_id, in_reply_to_id=sent.in_reply_to_id, body=body
    )
    session.add(draft)
    session.flush()
    audit.record(
        session,
        actor="owner",
        action="draft_copied_from_message",
        subject_type="draft",
        subject_id=draft.id,
        source_message_id=str(sent.id),
    )
    return draft


def regenerate(
    session: Session, services: Services, draft: Draft, instruction: str, now: datetime
) -> bool:
    """Replace the draft text with a fresh AI draft. Returns False if drafting failed."""
    if draft.status != DraftStatus.READY:
        return False
    instruction = instruction.strip()[:MAX_INSTRUCTION]
    text, notes = _generate(services, instruction, draft.in_reply_to, now)
    draft.instruction = instruction
    if text is None:
        return False
    draft.ai_draft_text, draft.ai_notes, draft.body = text, notes, text
    return True


def update_body(draft: Draft, body: str) -> bool:
    if draft.status != DraftStatus.READY:
        return False
    draft.body = body.replace("\r\n", "\n").strip()
    return True


def validate_body(body: str) -> str | None:
    """Return an error message suitable for display, or None if the text can be sent."""
    if not body.strip():
        return "The message is empty. Write something before sending."
    if len(body) > MAX_SMS_BODY:
        return (
            f"The message is {len(body)} characters long. The limit is {MAX_SMS_BODY}. "
            "Shorten it before sending."
        )
    return None


def discard(session: Session, draft: Draft) -> bool:
    if draft.status != DraftStatus.READY:
        return False
    draft.status = DraftStatus.DISCARDED
    audit.record(
        session, actor="owner", action="draft_discarded", subject_type="draft", subject_id=draft.id
    )
    return True
