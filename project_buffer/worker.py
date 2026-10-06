"""Background worker: claims durable jobs and runs them.

Run with ``python -m project_buffer.worker``.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.config import get_settings
from project_buffer.domain.enums import JobKind, JobStatus
from project_buffer.infrastructure.db.models import ProcessingJob, WorkerHeartbeat
from project_buffer.infrastructure.llm.base import LLMError
from project_buffer.infrastructure.sms.base import MediaFetchError, SmsUncertainError
from project_buffer.logging_setup import configure_logging
from project_buffer.services import analysis, audit, jobs, media, notifications, outbound
from project_buffer.services.container import Services, build_services
from project_buffer.services.conversations import sync_contacts

logger = logging.getLogger(__name__)


def _safe_error(exc: Exception) -> str:
    """A description of a failure that is safe to store and log.

    Only exceptions raised by this codebase have messages known to be free of
    message content; anything else is reduced to its class name.
    """
    if isinstance(exc, LLMError | SmsUncertainError | MediaFetchError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _dispatch(session: Session, services: Services, job: ProcessingJob, now: datetime) -> None:
    final = job.attempts >= job.max_attempts
    if job.kind == JobKind.ANALYZE_MESSAGE and job.message_id:
        analysis.analyze_message(session, services, job.message_id, now)
    elif job.kind == JobKind.NOTIFY_OWNER:
        notifications.run_notification_job(
            session, services, job.message_id, job.payload, job.dedupe_key, now
        )
    elif job.kind == JobKind.FETCH_MEDIA:
        media.fetch_attachment(
            session, services, uuid.UUID(job.payload["attachment_id"]), now, final=final
        )
    elif job.kind == JobKind.RECONCILE_OUTBOUND and job.message_id:
        outbound.reconcile_outbound(session, services, job.message_id, now, final=final)


def _on_final_failure(
    session: Session, services: Services, job: ProcessingJob, now: datetime
) -> None:
    if job.kind == JobKind.ANALYZE_MESSAGE and job.message_id:
        analysis.mark_analysis_failed(session, services, job.message_id, now)
    audit.record(
        session,
        actor="worker",
        action="job_failed",
        subject_type="job",
        subject_id=job.id,
        kind=job.kind.value,
        error=job.last_error,
    )


def run_once(services: Services, worker_id: str, now: datetime | None = None) -> bool:
    """Claim and run at most one job. Returns True if a job was claimed."""
    now = now or utcnow()
    with services.session_factory() as session:
        job = jobs.claim_next(
            session,
            worker_id=worker_id,
            now=now,
            visibility_timeout=timedelta(seconds=services.settings.job_visibility_timeout_seconds),
        )
        if job is None:
            return False
        if job.status == JobStatus.FAILED:
            _on_final_failure(session, services, job, now)
            session.commit()
            return True
        try:
            _dispatch(session, services, job, now)
            jobs.mark_succeeded(session, job, now)
            session.commit()
        except Exception as exc:
            session.rollback()
            error = _safe_error(exc)
            final = jobs.mark_failed(
                session, job, now, error, retryable=getattr(exc, "retryable", True)
            )
            if final:
                _on_final_failure(session, services, job, now)
            session.commit()
            logger.warning(
                "job failed id=%s kind=%s attempt=%d final=%s error=%s",
                job.id,
                job.kind.value,
                job.attempts,
                final,
                error,
            )
        return True


def drain(
    services: Services, worker_id: str = "drain", limit: int = 100, now: datetime | None = None
) -> int:
    """Run jobs until none are runnable. Used by tests and the CLI."""
    count = 0
    while count < limit and run_once(services, worker_id, now):
        count += 1
    return count


def beat(services: Services, worker_id: str, now: datetime) -> None:
    with services.session_factory() as session:
        session.merge(WorkerHeartbeat(worker_id=worker_id, last_seen_at=now))
        session.commit()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    services = build_services(settings)
    worker_id = f"{socket.gethostname()}-{os.getpid()}"[:64]

    stopping = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        logger.info("worker stopping after current job")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    with services.session_factory() as session:
        sync_contacts(session, settings)
        session.commit()

    logger.info("worker started id=%s", worker_id)
    last_beat = last_watchdog = 0.0
    while not stopping:
        try:
            tick = time.monotonic()
            if tick - last_beat >= 30:
                beat(services, worker_id, utcnow())
                last_beat = tick
            if run_once(services, worker_id):
                continue
            if tick - last_watchdog >= 60:
                with services.session_factory() as session:
                    notifications.notify_delayed_messages(session, services, utcnow())
                    session.commit()
                last_watchdog = tick
        except Exception as exc:
            # Never let one bad iteration kill the loop; the platform would restart us anyway.
            logger.error("worker loop error: %s", type(exc).__name__)
        time.sleep(settings.worker_poll_seconds)
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
