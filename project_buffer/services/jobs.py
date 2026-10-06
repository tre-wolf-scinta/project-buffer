"""Durable job queue on the database.

Postgres claims use ``FOR UPDATE SKIP LOCKED`` so several workers never take the
same job. A job left ``running`` by a dead worker is reclaimed once its lock is
older than the visibility timeout, so handlers must be idempotent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from project_buffer.domain.enums import JobKind, JobStatus
from project_buffer.infrastructure.db.models import ProcessingJob

_BACKOFF_SECONDS = (10, 30, 60, 120, 300, 600, 900)


def backoff_delay(attempts: int) -> timedelta:
    """Delay before the next try, given how many attempts have been made."""
    index = min(max(attempts, 1), len(_BACKOFF_SECONDS)) - 1
    return timedelta(seconds=_BACKOFF_SECONDS[index])


def enqueue(
    session: Session,
    kind: JobKind,
    *,
    now: datetime,
    max_attempts: int,
    message_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
) -> ProcessingJob | None:
    """Add a job. Returns None if a job with the same dedupe key already exists."""
    if dedupe_key is not None:
        existing = session.scalar(
            select(ProcessingJob.id).where(ProcessingJob.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return None
    job = ProcessingJob(
        kind=kind,
        message_id=message_id,
        payload=payload or {},
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
        run_after=run_after or now,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        return None
    return job


def claim_next(
    session: Session, *, worker_id: str, now: datetime, visibility_timeout: timedelta
) -> ProcessingJob | None:
    """Claim one runnable job and commit the claim."""
    stale_before = now - visibility_timeout
    job = session.scalar(
        select(ProcessingJob)
        .where(
            or_(
                and_(ProcessingJob.status == JobStatus.QUEUED, ProcessingJob.run_after <= now),
                and_(
                    ProcessingJob.status == JobStatus.RUNNING,
                    ProcessingJob.locked_at < stale_before,
                ),
            )
        )
        .order_by(ProcessingJob.run_after)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        session.rollback()
        return None
    if job.attempts >= job.max_attempts:
        # Died on its last attempt without recording the failure.
        job.status = JobStatus.FAILED
        job.last_error = job.last_error or "abandoned"
        job.finished_at = now
        session.commit()
        return job
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.locked_at = now
    job.locked_by = worker_id
    session.commit()
    return job


def mark_succeeded(session: Session, job: ProcessingJob, now: datetime) -> None:
    job.status = JobStatus.SUCCEEDED
    job.finished_at = now
    job.locked_at = None
    job.last_error = None


def mark_failed(
    session: Session, job: ProcessingJob, now: datetime, error: str, *, retryable: bool = True
) -> bool:
    """Record a failure. Returns True if the job is finished for good."""
    job.last_error = error[:200]
    job.locked_at = None
    if retryable and job.attempts < job.max_attempts:
        job.status = JobStatus.QUEUED
        job.run_after = now + backoff_delay(job.attempts)
        return False
    job.status = JobStatus.FAILED
    job.finished_at = now
    return True


def has_active_job(session: Session, kind: JobKind, message_id: uuid.UUID) -> bool:
    return (
        session.scalar(
            select(ProcessingJob.id)
            .where(
                ProcessingJob.kind == kind,
                ProcessingJob.message_id == message_id,
                ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            .limit(1)
        )
        is not None
    )
