"""Append-only audit trail. Detail values must never contain message text."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from project_buffer.infrastructure.db.models import AuditEvent


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    subject_type: str | None = None,
    subject_id: object | None = None,
    **detail: Any,
) -> None:
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=None if subject_id is None else str(subject_id),
            detail=detail,
        )
    )
