"""Read-side queries for the inbox, timeline and search, plus owner triage actions.

Nothing in the list or search paths returns original text. Search that
includes originals reports only *which* messages matched.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from project_buffer.domain.enums import Direction, DraftStatus, ProcessingStatus, Urgency
from project_buffer.infrastructure.db.models import Draft, Message, MessageAnalysis
from project_buffer.services import audit
from project_buffer.services.container import Services

PAGE_SIZE = 20


class InboxFilter(StrEnum):
    OPEN = "open"
    NEEDS_RESPONSE = "needs_response"
    URGENT = "urgent"
    UNREAD = "unread"
    HANDLED = "handled"
    ALL = "all"
    QUARANTINED = "quarantined"


FILTER_LABELS = {
    InboxFilter.OPEN: "Open",
    InboxFilter.NEEDS_RESPONSE: "Needs response",
    InboxFilter.URGENT: "Urgent",
    InboxFilter.UNREAD: "Unread",
    InboxFilter.HANDLED: "Handled",
    InboxFilter.ALL: "All",
    InboxFilter.QUARANTINED: "Unrecognized senders",
}

_ELEVATED = [Urgency.EMERGENCY, Urgency.URGENT]


def _inbound() -> Select[Any]:
    return select(Message).where(Message.direction == Direction.INBOUND)


def _apply_filter(statement: Select[Any], which: InboxFilter) -> Select[Any]:
    quarantined = Message.processing_status == ProcessingStatus.QUARANTINED
    if which == InboxFilter.QUARANTINED:
        return statement.where(quarantined)
    statement = statement.where(~quarantined)
    if which == InboxFilter.OPEN:
        return statement.where(Message.handled_at.is_(None))
    if which == InboxFilter.NEEDS_RESPONSE:
        return statement.where(Message.handled_at.is_(None), Message.requires_response.is_(True))
    if which == InboxFilter.URGENT:
        return statement.where(Message.handled_at.is_(None), Message.urgency.in_(_ELEVATED))
    if which == InboxFilter.UNREAD:
        return statement.where(Message.read_at.is_(None))
    if which == InboxFilter.HANDLED:
        return statement.where(Message.handled_at.is_not(None))
    return statement


def filter_counts(session: Session) -> dict[InboxFilter, int]:
    return {
        which: session.scalar(
            select(func.count()).select_from(_apply_filter(_inbound(), which).subquery())
        )
        or 0
        for which in InboxFilter
    }


def list_inbox(session: Session, which: InboxFilter, page: int) -> tuple[list[Message], bool]:
    """Messages for one inbox page, most urgent unhandled first, and whether more exist."""
    urgency_rank = case(
        (Message.urgency == Urgency.EMERGENCY, 0), (Message.urgency == Urgency.URGENT, 1), else_=2
    )
    statement = _apply_filter(_inbound(), which).options(selectinload(Message.analyses))
    if which in (InboxFilter.OPEN, InboxFilter.NEEDS_RESPONSE, InboxFilter.URGENT):
        statement = statement.order_by(urgency_rank, Message.occurred_at.desc())
    else:
        statement = statement.order_by(Message.occurred_at.desc())
    rows = list(
        session.scalars(statement.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    )
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def list_timeline(session: Session, page: int) -> tuple[list[Message], bool]:
    """All messages, both directions, newest first. Quarantined ones are left out."""
    statement = (
        select(Message)
        .where(Message.processing_status != ProcessingStatus.QUARANTINED)
        .options(selectinload(Message.analyses))
        .order_by(Message.occurred_at.desc())
    )
    rows = list(
        session.scalars(statement.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    )
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def open_drafts(session: Session) -> list[Draft]:
    return list(
        session.scalars(
            select(Draft).where(Draft.status == DraftStatus.READY).order_by(Draft.updated_at.desc())
        ).all()
    )


def unread_count(session: Session) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.direction == Direction.INBOUND,
                Message.read_at.is_(None),
                Message.processing_status != ProcessingStatus.QUARANTINED,
            )
        )
        or 0
    )


# --- triage actions ---------------------------------------------------------------


def mark_read(message: Message, now: datetime) -> None:
    if message.read_at is None:
        message.read_at = now


def set_handled(session: Session, message: Message, handled: bool, now: datetime) -> None:
    message.handled_at = now if handled else None
    if handled:
        mark_read(message, now)
    audit.record(
        session,
        actor="owner",
        action="message_handled" if handled else "message_reopened",
        subject_type="message",
        subject_id=message.id,
    )


def override_urgency(session: Session, message: Message, urgency: Urgency) -> None:
    previous = message.urgency
    message.urgency = urgency
    message.urgency_overridden = True
    audit.record(
        session,
        actor="owner",
        action="urgency_overridden",
        subject_type="message",
        subject_id=message.id,
        previous=previous.value,
        new=urgency.value,
    )


# --- search -------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    message: Message
    # True when the term was found only in the original text, not the sanitized summary.
    matched_original_only: bool


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(
    session: Session, services: Services, term: str, *, include_originals: bool, limit: int = 50
) -> list[SearchHit]:
    """Search sanitized summaries and the owner's own sent messages.

    With ``include_originals`` the co-parent's original text is searched too,
    by decrypting in memory. Hits never carry original text.
    """
    term = term.strip()
    if not term:
        return []
    needle = term.lower()
    hits: dict[uuid.UUID, SearchHit] = {}

    pattern = f"%{_escape_like(needle)}%"
    sanitized = session.scalars(
        select(Message)
        .join(MessageAnalysis, MessageAnalysis.message_id == Message.id)
        .where(
            MessageAnalysis.is_current.is_(True),
            or_(
                func.lower(MessageAnalysis.search_text).like(pattern, escape="\\"),
                func.lower(MessageAnalysis.topic).like(pattern, escape="\\"),
            ),
        )
        .options(selectinload(Message.analyses))
        .order_by(Message.occurred_at.desc())
        .limit(limit)
    ).all()
    for message in sanitized:
        hits[message.id] = SearchHit(message, matched_original_only=False)

    scan = select(Message).options(selectinload(Message.analyses))
    if not include_originals:
        scan = scan.where(Message.direction == Direction.OUTBOUND)
    else:
        scan = scan.where(Message.processing_status != ProcessingStatus.QUARANTINED)
    for message in session.scalars(scan.order_by(Message.occurred_at.desc())).yield_per(200):
        if message.id in hits:
            continue
        body = services.crypto.decrypt(message.body_ciphertext, aad=message.body_aad()).decode()
        if needle in body.lower():
            hits[message.id] = SearchHit(
                message, matched_original_only=message.direction == Direction.INBOUND
            )

    ordered = sorted(hits.values(), key=lambda hit: hit.message.occurred_at, reverse=True)
    return ordered[:limit]
