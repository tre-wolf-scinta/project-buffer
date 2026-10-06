"""Drafting, explicit approval, sending, delivery tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from project_buffer.domain.analysis import DraftReplyResult
from project_buffer.domain.enums import DeliveryStatus, Direction, DraftStatus, JobKind
from project_buffer.infrastructure.db.models import (
    AuditEvent,
    DeliveryEvent,
    Draft,
    Message,
    ProcessingJob,
)
from project_buffer.infrastructure.llm.base import LLMTransientError
from project_buffer.infrastructure.sms.base import SentSms, SmsRejectedError, SmsUncertainError
from project_buffer.services.container import Services
from project_buffer.services.drafts import body_digest
from project_buffer.worker import drain, run_once
from tests.conftest import (
    COPARENT,
    OWNER,
    FakeSms,
    ScriptedLLM,
    analysis,
    csrf_from,
    inbound_params,
    post_inbound,
    post_status,
    soup_of,
    visible_text,
)

INSTRUCTION = "We can't do Wednesday. Remind her I'm still okay with the bus."
DRAFT_TEXT = (
    "We aren't available to provide transportation Wednesday. I continue to support using "
    "the school bus if you'd like to revisit that."
)
ORIGINAL = "You useless jerk TIGER-MARKER-9912. Take them to school Wednesday."


def _inbound(client: TestClient, db: Session, services: Services, llm: ScriptedLLM) -> Message:
    llm.analyses.append(
        analysis(
            short_summary="Jordan asks for school transportation on Wednesday.",
            topic="School transportation",
            requests=["Transportation to and from school Wednesday."],
            response_needed=True,
        )
    )
    post_inbound(client, inbound_params(ORIGINAL))
    drain(services)
    return db.scalars(select(Message).where(Message.direction == Direction.INBOUND)).one()


def _draft(client: TestClient, db: Session, llm: ScriptedLLM, message: Message) -> Draft:
    llm.drafts.append(DraftReplyResult(message_text=DRAFT_TEXT, notes_for_owner=[]))
    response = client.post(
        f"/messages/{message.id}/reply",
        data={"instruction": INSTRUCTION, "csrf_token": client.csrf},
    )
    assert response.status_code == 303
    return db.scalars(select(Draft)).one()


def _send(client: TestClient, draft: Draft, **overrides: str) -> object:
    data = {
        "csrf_token": client.csrf,
        "approve": "yes",
        "reviewed_digest": body_digest(draft.body),
    }
    data.update(overrides)
    return client.post(f"/drafts/{draft.id}/send", data=data)


def _outbound(db: Session) -> list[Message]:
    db.expire_all()
    return list(db.scalars(select(Message).where(Message.direction == Direction.OUTBOUND)))


def test_draft_creation_uses_sanitized_context_only(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)

    assert draft.status == DraftStatus.READY
    assert draft.body == DRAFT_TEXT and draft.ai_draft_text == DRAFT_TEXT
    assert draft.instruction == INSTRUCTION and draft.in_reply_to_id == message.id
    request = llm.draft_requests[0]
    assert request.instruction == INSTRUCTION
    assert "School transportation" in request.context_summary
    # The drafting model never sees the original text.
    assert "TIGER-MARKER" not in request.context_summary and "jerk" not in request.context_summary
    # Creating a draft sends nothing to the co-parent.
    assert sms.sent_to(COPARENT) == []


def test_reply_continues_an_existing_draft_instead_of_starting_another(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    response = auth_client.get(f"/messages/{message.id}/reply")
    assert response.status_code == 303
    assert response.headers["location"] == f"/drafts/{draft.id}"


def test_ai_failure_still_lets_the_owner_write_by_hand(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    message = _inbound(auth_client, db, services, llm)
    llm.drafts.append(LLMTransientError("APITimeoutError"))
    response = auth_client.post(
        f"/messages/{message.id}/reply",
        data={"instruction": INSTRUCTION, "csrf_token": auth_client.csrf},
    )
    assert "notice=draft_ai_failed" in response.headers["location"]
    draft = db.scalars(select(Draft)).one()
    assert draft.body == "" and draft.ai_draft_text is None
    page = auth_client.get(response.headers["location"])
    assert "AI drafting is unavailable" in page.text


def test_owner_can_edit_draft_and_sends_the_edited_text(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    edited = "We can't do Wednesday. The bus is still fine with me."
    response = auth_client.post(
        f"/drafts/{draft.id}",
        data={"action": "review", "body": edited, "csrf_token": auth_client.csrf},
    )
    assert response.headers["location"] == f"/drafts/{draft.id}/review"

    review = auth_client.get(f"/drafts/{draft.id}/review")
    assert soup_of(review).find(id="exact-text").get_text() == edited
    digest = soup_of(review).find("input", {"name": "reviewed_digest"})["value"]
    assert sms.sent_to(COPARENT) == []  # reviewing sends nothing

    response = auth_client.post(
        f"/drafts/{draft.id}/send",
        data={"csrf_token": auth_client.csrf, "approve": "yes", "reviewed_digest": digest},
    )
    assert response.status_code == 303 and "notice=sent" in response.headers["location"]
    assert [m["body"] for m in sms.sent_to(COPARENT)] == [edited]

    db.expire_all()
    draft = db.get(Draft, draft.id)
    assert draft.status == DraftStatus.SENT and draft.ai_draft_text == DRAFT_TEXT
    sent = _outbound(db)[0]
    assert services.crypto.decrypt(sent.body_ciphertext, aad=sent.body_aad()).decode() == edited
    assert sent.provider_message_id == sms.sent_to(COPARENT)[0]["sid"]
    assert sent.delivery_status == DeliveryStatus.QUEUED
    assert sent.in_reply_to_id == message.id
    assert sms.sent_to(COPARENT)[0]["callback"] == "https://buffer.test/webhooks/twilio/status"
    # Replying marks the inbound message handled.
    assert db.get(Message, message.id).handled_at is not None
    audit = db.scalars(select(AuditEvent).where(AuditEvent.action == "outbound_approved")).one()
    assert audit.detail["edited_after_ai"] is True


def test_nothing_is_sent_without_explicit_approval(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)

    # Viewing, saving and reviewing never send.
    auth_client.get(f"/drafts/{draft.id}")
    auth_client.get(f"/drafts/{draft.id}/review")
    auth_client.post(
        f"/drafts/{draft.id}",
        data={"action": "save", "body": DRAFT_TEXT, "csrf_token": auth_client.csrf},
    )
    # The send endpoint refuses without the approval field...
    assert _send(auth_client, draft, approve="").status_code == 303
    # ...without a CSRF token...
    assert _send(auth_client, draft, csrf_token="").status_code == 403
    # ...with a digest for text other than what is stored...
    stale = _send(auth_client, draft, reviewed_digest=body_digest("something else"))
    assert "notice=stale_review" in stale.headers["location"]
    # ...and has no GET form at all.
    assert auth_client.get(f"/drafts/{draft.id}/send").status_code in (404, 405)

    assert sms.sent_to(COPARENT) == []
    assert _outbound(db) == []
    drain(services)
    assert sms.sent_to(COPARENT) == []


def test_text_edited_after_review_is_not_sent_until_reviewed_again(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    reviewed = body_digest(draft.body)
    auth_client.post(
        f"/drafts/{draft.id}",
        data={"action": "save", "body": "Changed in another tab", "csrf_token": auth_client.csrf},
    )
    response = _send(auth_client, draft, reviewed_digest=reviewed)
    assert "stale_review" in response.headers["location"]
    assert sms.sent_to(COPARENT) == []


def test_double_click_sends_exactly_once(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    first = _send(auth_client, draft)
    second = _send(auth_client, draft)
    third = _send(auth_client, draft)
    assert "notice=sent" in first.headers["location"]
    assert "notice=already_sent" in second.headers["location"]
    assert "notice=already_sent" in third.headers["location"]
    assert len(sms.sent_to(COPARENT)) == 1
    assert len(_outbound(db)) == 1
    page = auth_client.get(second.headers["location"])
    assert "It was not sent again." in page.text


def test_empty_or_overlong_draft_cannot_be_sent(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    response = auth_client.post(
        f"/drafts/{draft.id}",
        data={"action": "review", "body": "   ", "csrf_token": auth_client.csrf},
    )
    assert response.status_code == 400 and "The message is empty." in response.text
    response = auth_client.post(
        f"/drafts/{draft.id}",
        data={"action": "review", "body": "x" * 1700, "csrf_token": auth_client.csrf},
    )
    assert response.status_code == 400 and "The limit is 1600" in response.text
    assert sms.sent_to(COPARENT) == []


def test_delivery_callbacks_update_status_and_tolerate_reordering(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    _send(auth_client, draft)
    sid = sms.sent[-1]["sid"]

    assert (
        post_status(auth_client, {"MessageSid": sid, "MessageStatus": "delivered"}).status_code
        == 200
    )
    # A late 'sent' callback must not move the status backwards.
    assert post_status(auth_client, {"MessageSid": sid, "MessageStatus": "sent"}).status_code == 200
    sent = _outbound(db)[0]
    assert sent.delivery_status == DeliveryStatus.DELIVERED
    assert [e.status for e in sent.delivery_events] == ["delivered", "sent"]

    page = auth_client.get(f"/messages/{sent.id}")
    text = visible_text(page)
    assert "Delivered." in text and draft.body in text


def test_delivery_callback_requires_valid_signature(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    _send(auth_client, _draft(auth_client, db, llm, message))
    response = auth_client.post(
        "/webhooks/twilio/status",
        data={"MessageSid": sms.sent[-1]["sid"], "MessageStatus": "delivered"},
        headers={"X-Twilio-Signature": "forged"},
    )
    assert response.status_code == 403
    assert _outbound(db)[0].delivery_status == DeliveryStatus.QUEUED
    assert db.scalar(select(func.count()).select_from(DeliveryEvent)) == 0


def test_undelivered_callback_notifies_the_owner(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    _send(auth_client, _draft(auth_client, db, llm, message))
    sid = sms.sent[-1]["sid"]
    post_status(
        auth_client, {"MessageSid": sid, "MessageStatus": "undelivered", "ErrorCode": "30003"}
    )
    drain(services)
    sent = _outbound(db)[0]
    assert sent.delivery_status == DeliveryStatus.UNDELIVERED
    assert sent.delivery_error_code == "30003"
    notices = [m["body"] for m in sms.sent_to(OWNER)]
    assert any("could not be delivered" in body for body in notices)
    page = auth_client.get(f"/messages/{sent.id}")
    assert "Not delivered." in page.text and "unreachable" in page.text


def test_callback_arriving_before_the_send_is_recorded_is_adopted(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    # Twilio's callback wins the race against our own commit of the SID.
    next_sid = "SMout" + f"{len(sms.sent) + 1:029d}"
    post_status(auth_client, {"MessageSid": next_sid, "MessageStatus": "delivered"})
    _send(auth_client, draft)
    sent = _outbound(db)[0]
    assert sent.provider_message_id == next_sid
    assert sent.delivery_status == DeliveryStatus.DELIVERED


def test_provider_rejection_marks_failed_and_offers_a_new_draft(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    sms.failures.append(SmsRejectedError(code="21610", http_status=400))
    response = _send(auth_client, draft)

    sent = _outbound(db)[0]
    assert sent.delivery_status == DeliveryStatus.FAILED and sent.provider_message_id is None
    assert db.get(Draft, draft.id).status == DraftStatus.FAILED
    assert sms.sent_to(COPARENT) == []
    # The inbound message is not marked handled by a failed reply.
    assert db.get(Message, message.id).handled_at is None
    page = auth_client.get(response.headers["location"])
    assert "Failed, not sent." in page.text and "texting STOP" in page.text

    # Retrying creates a fresh draft that again needs review and approval.
    retry = auth_client.post(f"/messages/{sent.id}/retry", data={"csrf_token": auth_client.csrf})
    new_draft = db.scalars(select(Draft).where(Draft.status == DraftStatus.READY)).one()
    assert retry.headers["location"] == f"/drafts/{new_draft.id}"
    assert new_draft.body == draft.body
    assert sms.sent_to(COPARENT) == []


def test_uncertain_send_is_reconciled_instead_of_resent(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    sms.failures.append(SmsUncertainError("ReadTimeout"))
    response = _send(auth_client, draft)
    assert "notice=uncertain" in response.headers["location"]
    sent = _outbound(db)[0]
    assert sent.delivery_status == DeliveryStatus.UNCERTAIN
    assert "Do not send it again yet" in auth_client.get(response.headers["location"]).text
    # A second click while uncertain does not send either.
    assert "already_sent" in _send(auth_client, draft).headers["location"]

    # The provider did receive it: the reconcile job adopts the existing message.
    sms.findable = SentSms(provider_message_id="SMfound", status="sent")
    drain(services, now=datetime.now(UTC) + timedelta(minutes=1))
    sent = _outbound(db)[0]
    assert sent.provider_message_id == "SMfound"
    assert sent.delivery_status == DeliveryStatus.SENT
    assert db.get(Draft, draft.id).status == DraftStatus.SENT
    assert sms.sent_to(COPARENT) == []  # never sent a second time by us


def test_uncertain_send_never_found_is_marked_failed_and_owner_told(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    sms.failures.append(SmsUncertainError("ConnectTimeout"))
    _send(auth_client, draft)
    job = db.scalars(
        select(ProcessingJob).where(ProcessingJob.kind == JobKind.RECONCILE_OUTBOUND)
    ).one()
    now = datetime.now(UTC)
    for attempt in range(job.max_attempts + 1):
        run_once(services, "test", now + timedelta(hours=attempt + 1))
    drain(services, now=now + timedelta(days=1))

    sent = _outbound(db)[0]
    assert sent.delivery_status == DeliveryStatus.FAILED
    assert db.get(Draft, draft.id).status == DraftStatus.FAILED
    assert sms.sent_to(COPARENT) == []
    assert any("could not be delivered" in m["body"] for m in sms.sent_to(OWNER))


def test_compose_without_a_message_to_reply_to(
    auth_client: TestClient, db: Session, llm: ScriptedLLM, sms: FakeSms
) -> None:
    llm.drafts.append(
        DraftReplyResult(message_text="Riley's recital is Friday at 6.", notes_for_owner=["n"])
    )
    page = auth_client.get("/compose")
    response = auth_client.post(
        "/compose",
        data={"instruction": "Tell her the recital is Friday at 6", "csrf_token": csrf_from(page)},
    )
    draft = db.scalars(select(Draft)).one()
    assert response.headers["location"] == f"/drafts/{draft.id}"
    assert llm.draft_requests[0].context_summary is None
    assert _send(auth_client, draft).status_code == 303
    assert [m["body"] for m in sms.sent_to(COPARENT)] == ["Riley's recital is Friday at 6."]


def test_discarded_draft_cannot_be_sent(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    message = _inbound(auth_client, db, services, llm)
    draft = _draft(auth_client, db, llm, message)
    auth_client.post(
        f"/drafts/{draft.id}", data={"action": "discard", "csrf_token": auth_client.csrf}
    )
    _send(auth_client, draft)
    assert sms.sent_to(COPARENT) == []
    assert _outbound(db) == []
