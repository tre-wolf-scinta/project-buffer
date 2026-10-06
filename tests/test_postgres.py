"""Behaviour that only PostgreSQL provides. Skipped unless TEST_DATABASE_URL is set."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from project_buffer.domain.analysis import DraftReplyResult
from project_buffer.domain.enums import Direction, JobKind
from project_buffer.infrastructure.db.models import Draft, Message
from project_buffer.services import jobs, outbound
from project_buffer.services.container import Services
from project_buffer.services.drafts import body_digest, create_draft
from tests.conftest import (
    COPARENT,
    POSTGRES_URL,
    FakeSms,
    ScriptedLLM,
    inbound_params,
    post_inbound,
)

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="requires TEST_DATABASE_URL (Postgres)")


def _message(client: TestClient, db: Session) -> Message:
    post_inbound(client, inbound_params("Pickup at 5?"))
    return db.scalars(select(Message)).one()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE messages SET body_ciphertext = 'x'::bytea WHERE id = :id",
        "UPDATE messages SET from_number = '+12025550155' WHERE id = :id",
        "UPDATE messages SET occurred_at = now() WHERE id = :id",
        "UPDATE messages SET provider_message_id = 'SMother' WHERE id = :id",
        "UPDATE messages SET raw_payload_ciphertext = NULL WHERE id = :id",
        "DELETE FROM messages WHERE id = :id",
    ],
)
def test_database_trigger_blocks_changes_to_originals(
    client: TestClient, db: Session, statement: str
) -> None:
    """Even raw SQL that bypasses the application cannot rewrite or delete an original."""
    message = _message(client, db)
    with pytest.raises(DBAPIError):
        db.execute(text(statement), {"id": message.id})
    db.rollback()


def test_database_trigger_allows_triage_updates(client: TestClient, db: Session) -> None:
    message = _message(client, db)
    db.execute(text("UPDATE messages SET handled_at = now() WHERE id = :id"), {"id": message.id})
    db.commit()


def test_audit_trail_is_append_only(client: TestClient, db: Session) -> None:
    _message(client, db)
    for statement in ("UPDATE audit_events SET action = 'x'", "DELETE FROM audit_events"):
        with pytest.raises(DBAPIError):
            db.execute(text(statement))
        db.rollback()


def test_two_workers_never_claim_the_same_job(services: Services) -> None:
    """FOR UPDATE SKIP LOCKED: a job row locked by one claim is invisible to the other."""
    now = datetime.now(UTC)
    with services.session_factory() as session:
        jobs.enqueue(session, JobKind.NOTIFY_OWNER, now=now, max_attempts=3)
        jobs.enqueue(session, JobKind.NOTIFY_OWNER, now=now + timedelta(seconds=1), max_attempts=3)
        session.commit()

    claimed: list[object] = []
    barrier = threading.Barrier(4)

    def claim() -> None:
        with services.session_factory() as session:
            barrier.wait()
            job = jobs.claim_next(
                session,
                worker_id=threading.current_thread().name,
                now=now + timedelta(seconds=5),
                visibility_timeout=timedelta(minutes=5),
            )
            if job is not None:
                claimed.append(job.id)

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == 2 and len(set(claimed)) == 2


def test_simultaneous_approvals_send_exactly_once(
    services: Services, db: Session, llm: ScriptedLLM, sms: FakeSms
) -> None:
    """A true double-click: two requests approve the same draft at the same instant."""
    llm.drafts.append(DraftReplyResult(message_text="See you at 5.", notes_for_owner=[]))
    now = datetime.now(UTC)
    draft = create_draft(db, services, instruction="say see you at 5", in_reply_to=None, now=now)
    db.commit()
    digest = body_digest(draft.body)

    outcomes: list[outbound.SendOutcome] = []
    barrier = threading.Barrier(6)

    def approve() -> None:
        with services.session_factory() as session:
            barrier.wait()
            result = outbound.approve_and_send(
                session, services, draft_id=draft.id, reviewed_digest=digest, now=now
            )
            outcomes.append(result.outcome)

    threads = [threading.Thread(target=approve) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(outbound.SendOutcome.SENT) == 1
    assert outcomes.count(outbound.SendOutcome.ALREADY_HANDLED) == 5
    assert len(sms.sent_to(COPARENT)) == 1
    db.expire_all()
    assert (
        len(db.scalars(select(Message).where(Message.direction == Direction.OUTBOUND)).all()) == 1
    )
    assert db.get(Draft, draft.id).sent_message_id is not None


def test_simultaneous_duplicate_webhooks_store_one_message(client: TestClient, db: Session) -> None:
    params = inbound_params("Retry storm")
    barrier = threading.Barrier(5)
    statuses: list[int] = []

    def deliver() -> None:
        barrier.wait()
        statuses.append(post_inbound(client, params).status_code)

    threads = [threading.Thread(target=deliver) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert statuses == [200] * 5
    assert len(db.scalars(select(Message)).all()) == 1
