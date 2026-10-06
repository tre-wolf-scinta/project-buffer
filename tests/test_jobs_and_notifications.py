"""Durable queue semantics and owner notifications."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from project_buffer.domain.enums import (
    JobKind,
    JobStatus,
    NotificationKind,
    NotificationStatus,
    ProcessingStatus,
    Urgency,
)
from project_buffer.infrastructure.db.models import Message, Notification, ProcessingJob
from project_buffer.infrastructure.sms.base import SmsRejectedError, SmsUncertainError
from project_buffer.services import jobs, notifications
from project_buffer.services.container import Services
from project_buffer.worker import drain
from tests.conftest import (
    OWNER,
    FakeSms,
    ScriptedLLM,
    analysis,
    inbound_params,
    make_settings,
    post_inbound,
)

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
TIMEOUT = timedelta(seconds=300)


def _receive(client: TestClient, db: Session, body: str = "Pickup at 5?") -> Message:
    post_inbound(client, inbound_params(body))
    return db.scalars(select(Message).order_by(Message.created_at.desc())).first()


# --- queue ---------------------------------------------------------------------------------


def test_job_is_claimed_once_and_dedupe_key_prevents_duplicates(db: Session) -> None:
    first = jobs.enqueue(db, JobKind.NOTIFY_OWNER, now=NOW, max_attempts=3, dedupe_key="k")
    second = jobs.enqueue(db, JobKind.NOTIFY_OWNER, now=NOW, max_attempts=3, dedupe_key="k")
    db.commit()
    assert first is not None and second is None

    claimed = jobs.claim_next(db, worker_id="a", now=NOW, visibility_timeout=TIMEOUT)
    assert claimed is not None and claimed.status == JobStatus.RUNNING and claimed.attempts == 1
    assert jobs.claim_next(db, worker_id="b", now=NOW, visibility_timeout=TIMEOUT) is None


def test_job_abandoned_by_a_dead_worker_is_reclaimed(db: Session) -> None:
    """A worker that dies mid-job leaves it 'running'; it becomes claimable again."""
    jobs.enqueue(db, JobKind.NOTIFY_OWNER, now=NOW, max_attempts=3)
    db.commit()
    first = jobs.claim_next(db, worker_id="dies", now=NOW, visibility_timeout=TIMEOUT)
    assert first is not None

    soon = NOW + timedelta(seconds=60)
    assert jobs.claim_next(db, worker_id="b", now=soon, visibility_timeout=TIMEOUT) is None
    later = NOW + timedelta(seconds=301)
    again = jobs.claim_next(db, worker_id="b", now=later, visibility_timeout=TIMEOUT)
    assert again is not None and again.id == first.id
    assert again.attempts == 2 and again.locked_by == "b"


def test_failures_back_off_then_fail_for_good(db: Session) -> None:
    job = jobs.enqueue(db, JobKind.NOTIFY_OWNER, now=NOW, max_attempts=2)
    db.commit()
    jobs.claim_next(db, worker_id="a", now=NOW, visibility_timeout=TIMEOUT)
    assert jobs.mark_failed(db, job, NOW, "boom") is False
    assert job.status == JobStatus.QUEUED and job.run_after == NOW + jobs.backoff_delay(1)
    db.commit()
    jobs.claim_next(db, worker_id="a", now=NOW + timedelta(hours=1), visibility_timeout=TIMEOUT)
    assert jobs.mark_failed(db, job, NOW, "boom") is True
    assert job.status == JobStatus.FAILED


def test_worker_crash_during_analysis_does_not_lose_the_message(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    """The worker dies after claiming the job. After the visibility timeout the job runs
    again and the message is processed normally."""
    message = _receive(client, db)
    now = datetime.now(UTC)
    crashed = jobs.claim_next(db, worker_id="crashed", now=now, visibility_timeout=TIMEOUT)
    assert crashed is not None and crashed.kind == JobKind.ANALYZE_MESSAGE
    assert drain(services, now=now + timedelta(seconds=30)) == 0

    llm.analyses.append(analysis(topic="Pickup"))
    assert drain(services, now=now + timedelta(seconds=400)) >= 1
    db.expire_all()
    assert db.get(Message, message.id).processing_status == ProcessingStatus.PROCESSED
    assert len(sms.sent_to(OWNER)) == 1


def test_job_that_dies_on_its_last_attempt_still_reports_failure(
    client: TestClient, db: Session, services: Services, sms: FakeSms
) -> None:
    message = _receive(client, db)
    job = db.scalars(select(ProcessingJob)).one()
    job.attempts = job.max_attempts
    job.status = JobStatus.RUNNING
    job.locked_at = datetime.now(UTC) - timedelta(hours=1)
    db.commit()
    drain(services)
    db.expire_all()
    assert db.get(Message, message.id).processing_status == ProcessingStatus.FAILED
    assert "automatic filtering failed" in sms.sent_to(OWNER)[0]["body"]


def test_analysis_job_is_idempotent(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    """Running the same analysis twice (a retry after a crash post-commit) notifies once."""
    message = _receive(client, db)
    llm.analyses.extend([analysis(), analysis()])
    drain(services)
    jobs.enqueue(
        db, JobKind.ANALYZE_MESSAGE, now=datetime.now(UTC), max_attempts=3, message_id=message.id
    )
    db.commit()
    drain(services)
    assert len(sms.sent_to(OWNER)) == 1


# --- notifications ---------------------------------------------------------------------------


def _processed(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, **fields: object
) -> Message:
    llm.analyses.append(analysis(**fields))
    message = _receive(client, db)
    drain(services)
    db.expire_all()
    return db.get(Message, message.id)


def test_summary_notification_content(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _processed(
        client,
        db,
        services,
        llm,
        topic="Soccer pickup",
        short_summary="Jordan asks you to pick Riley up at 5.",
        response_needed=True,
    )
    body = sms.sent_to(OWNER)[0]["body"]
    assert body == (
        "Jordan: Soccer pickup. Jordan asks you to pick Riley up at 5. Reply needed. "
        f"https://buffer.test/messages/{message.id}"
    )
    record = db.scalars(select(Notification)).one()
    assert record.status == NotificationStatus.SENT and record.body == body


def test_long_summaries_are_truncated_but_keep_the_link(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _processed(client, db, services, llm, short_summary="Long detail. " * 24)
    body = sms.sent_to(OWNER)[0]["body"]
    assert len(body) <= notifications.MAX_SMS_CHARS
    assert body.endswith(f"/messages/{message.id}")


def test_link_only_mode_sends_no_summary(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    services.settings = make_settings(
        database_url=services.settings.database_url, notify_mode="link_only"
    )
    _processed(client, db, services, llm, topic="Secret topic", urgency=Urgency.URGENT)
    body = sms.sent_to(OWNER)[0]["body"]
    assert body.startswith("URGENT message from Jordan.") and "Secret topic" not in body


def test_notifications_can_be_turned_off(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    services.settings = make_settings(
        database_url=services.settings.database_url, notify_mode="off"
    )
    _processed(client, db, services, llm)
    assert sms.sent == []


def test_emergency_is_repeated_until_read(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _processed(
        client, db, services, llm, topic="Sam at the hospital", urgency=Urgency.EMERGENCY
    )
    assert sms.sent_to(OWNER)[0]["body"].startswith("EMERGENCY from Jordan: Sam at the hospital.")
    now = datetime.now(UTC)

    drain(services, now=now + timedelta(minutes=6))
    assert sms.sent_to(OWNER)[1]["body"].startswith("Reminder: unread EMERGENCY message")

    # Once the owner has read it, the next scheduled reminder is skipped.
    message.read_at = now
    db.commit()
    drain(services, now=now + timedelta(minutes=30))
    assert len(sms.sent_to(OWNER)) == 2


def test_normal_messages_get_no_reminders(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    _processed(client, db, services, llm, urgency=Urgency.URGENT)
    drain(services, now=datetime.now(UTC) + timedelta(hours=2))
    assert len(sms.sent_to(OWNER)) == 1


def test_uncertain_notification_send_is_retried(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    """A missing notice is worse than a duplicate, so transient failures retry."""
    llm.analyses.append(analysis())
    _receive(client, db)
    sms.failures.append(SmsUncertainError("ReadTimeout"))
    now = datetime.now(UTC)
    drain(services, now=now)
    assert sms.sent_to(OWNER) == []
    drain(services, now=now + timedelta(seconds=30))
    assert len(sms.sent_to(OWNER)) == 1


def test_rejected_notification_is_recorded_not_retried_forever(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    llm.analyses.append(analysis())
    _receive(client, db)
    sms.failures.append(SmsRejectedError(code="21610", http_status=400))
    drain(services, now=datetime.now(UTC) + timedelta(hours=1))
    assert db.scalars(select(Notification)).one().status == NotificationStatus.FAILED
    assert sms.sent == []


def test_watchdog_tells_owner_about_a_stuck_message_once(
    client: TestClient, db: Session, services: Services, sms: FakeSms
) -> None:
    """No worker is running. The web-side watchdog still tells the owner."""
    message = _receive(client, db, "TURTLE-MARKER stuck message")
    now = datetime.now(UTC)
    assert notifications.notify_delayed_messages(db, services, now + timedelta(minutes=1)) == 0
    assert sms.sent == []

    later = now + timedelta(minutes=10)
    assert notifications.notify_delayed_messages(db, services, later) == 1
    notifications.notify_delayed_messages(db, services, later + timedelta(minutes=5))
    bodies = [m["body"] for m in sms.sent_to(OWNER)]
    assert len(bodies) == 1
    assert "Automatic filtering is delayed" in bodies[0]
    assert "TURTLE-MARKER" not in bodies[0] and str(message.id) in bodies[0]


def test_notification_text_is_built_without_reading_the_original(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    message = _processed(client, db, services, llm)

    class ExplodingCrypto:
        def __getattr__(self, name: str) -> object:
            raise AssertionError("notification code must not touch encryption")

    services.crypto = ExplodingCrypto()  # type: ignore[assignment]
    for kind in NotificationKind:
        notifications.build_text(services.settings, kind, message)
