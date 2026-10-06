"""Twilio webhook ingestion."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from project_buffer.domain.enums import (
    AttachmentStatus,
    JobKind,
    ProcessingStatus,
    SenderStatus,
)
from project_buffer.infrastructure.db.models import (
    Attachment,
    AuditEvent,
    ImmutableRecordError,
    Message,
    MessageAnalysis,
    ProcessingJob,
)
from project_buffer.infrastructure.sms.base import FetchedMedia
from project_buffer.infrastructure.sms.twilio_adapter import TwilioGateway
from project_buffer.services.container import Services
from project_buffer.worker import drain
from tests.conftest import (
    BASE_URL,
    OWNER,
    STRANGER,
    TEST_AUTH_TOKEN,
    FakeSms,
    ScriptedLLM,
    analysis,
    inbound_params,
    post_inbound,
    twilio_signature,
)

BODY = "Can you take Riley to practice on Thursday at 5:30?"


def _count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_valid_signature_stores_encrypted_original_and_queues_job(
    client: TestClient, db: Session, services: Services
) -> None:
    params = inbound_params(BODY)
    response = client.post(
        "/webhooks/twilio/messages",
        data=params,
        headers={"X-Twilio-Signature": twilio_signature("/webhooks/twilio/messages", params)},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert "<Response>" in response.text and "Message" not in response.text

    message = db.scalars(select(Message)).one()
    assert message.provider_message_id == params["MessageSid"]
    assert message.from_number == params["From"]
    assert message.to_number == params["To"]
    assert message.sender_status == SenderStatus.AUTHORIZED
    assert message.processing_status == ProcessingStatus.PENDING
    assert message.occurred_at is not None
    # Encrypted at rest: the plaintext is not in the stored bytes or readable metadata.
    assert BODY.encode() not in message.body_ciphertext
    assert BODY not in json.dumps(message.provider_metadata)
    assert "Body" not in message.provider_metadata
    assert message.provider_metadata["AccountSid"] == "ACtest"
    # ...and decrypts to exactly what was received.
    plaintext = services.crypto.decrypt(message.body_ciphertext, aad=message.body_aad()).decode()
    assert plaintext == BODY
    payload = json.loads(
        services.crypto.decrypt(message.raw_payload_ciphertext, aad=message.payload_aad())
    )
    assert payload == params

    job = db.scalars(select(ProcessingJob)).one()
    assert job.kind == JobKind.ANALYZE_MESSAGE and job.message_id == message.id
    # Ingestion never calls the model.
    assert _count(db, MessageAnalysis) == 0


@pytest.mark.parametrize("signature", ["", "not-a-signature", "A" * 28])
def test_invalid_or_missing_signature_is_rejected(
    client: TestClient, db: Session, signature: str
) -> None:
    response = post_inbound(client, inbound_params(BODY), signature=signature)
    assert response.status_code == 403
    assert _count(db, Message) == 0
    assert _count(db, ProcessingJob) == 0


def test_signature_for_different_content_is_rejected(client: TestClient, db: Session) -> None:
    """A valid signature cannot be replayed with an altered body."""
    params = inbound_params(BODY)
    signature = twilio_signature("/webhooks/twilio/messages", params)
    tampered = {**params, "Body": "Something else entirely"}
    assert post_inbound(client, tampered, signature=signature).status_code == 403
    assert _count(db, Message) == 0


def test_real_twilio_adapter_validates_signatures() -> None:
    gateway = TwilioGateway("ACtest", TEST_AUTH_TOKEN, "+12025550100")
    params = inbound_params(BODY)
    url = BASE_URL + "/webhooks/twilio/messages"
    good = twilio_signature("/webhooks/twilio/messages", params)
    assert gateway.validate_signature(url, params, good)
    assert not gateway.validate_signature(url, params, "")
    assert not gateway.validate_signature(url, {**params, "Body": "x"}, good)
    assert not gateway.validate_signature(url.replace("https", "http"), params, good)


def test_duplicate_webhook_creates_one_message_and_one_job(client: TestClient, db: Session) -> None:
    params = inbound_params(BODY)
    assert post_inbound(client, params).status_code == 200
    assert post_inbound(client, params).status_code == 200
    assert post_inbound(client, params).status_code == 200
    assert _count(db, Message) == 1
    assert _count(db, ProcessingJob) == 1


def test_unrecognized_sender_is_quarantined_not_processed(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    secret = "QUARANTINE-MARKER text from a stranger"
    assert post_inbound(client, inbound_params(secret, sender=STRANGER)).status_code == 200
    assert post_inbound(client, inbound_params("second", sender=STRANGER)).status_code == 200

    messages = db.scalars(select(Message)).all()
    assert len(messages) == 2
    assert all(m.processing_status == ProcessingStatus.QUARANTINED for m in messages)
    assert all(m.sender_status == SenderStatus.UNRECOGNIZED for m in messages)
    kinds = [job.kind for job in db.scalars(select(ProcessingJob))]
    assert JobKind.ANALYZE_MESSAGE not in kinds
    # One neutral notice for the hour, however many texts arrive.
    assert kinds == [JobKind.NOTIFY_OWNER]

    drain(services)
    assert llm.analysis_requests == []
    notices = sms.sent_to(OWNER)
    assert len(notices) == 1
    assert "unrecognized number ending 0199" in notices[0]["body"]
    assert "QUARANTINE-MARKER" not in notices[0]["body"]


def test_text_from_owner_number_is_not_stored(client: TestClient, db: Session) -> None:
    response = post_inbound(client, inbound_params("ok thanks", sender=OWNER))
    assert response.status_code == 200
    assert "does not accept replies" in response.text
    assert _count(db, Message) == 0
    assert db.scalars(select(AuditEvent.action)).all() == ["owner_sms_ignored"]


def test_empty_message_is_stored_and_summarized_without_the_model(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    assert post_inbound(client, inbound_params("")).status_code == 200
    drain(services)
    message = db.scalars(select(Message)).one()
    assert message.processing_status == ProcessingStatus.PROCESSED
    assert message.current_analysis.short_summary == "The message was empty."
    assert llm.analysis_requests == []


def test_message_with_media_records_metadata_and_stores_files_privately(
    client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    url0 = "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MM1/Media/ME0"
    url1 = "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MM1/Media/ME1"
    sms.media[url0] = FetchedMedia(content=b"\x89PNG-image-bytes", content_type="image/png")
    sms.media[url1] = FetchedMedia(content=b"%PDF-notice", content_type="application/pdf")
    llm.analyses.append(analysis(topic="School notice"))
    params = inbound_params(
        "See the attached notice from school",
        NumMedia="2",
        MediaUrl0=url0,
        MediaContentType0="image/png",
        MediaUrl1=url1,
        MediaContentType1="application/pdf",
    )
    assert post_inbound(client, params).status_code == 200

    message = db.scalars(select(Message)).one()
    assert message.num_media == 2
    attachments = db.scalars(select(Attachment).order_by(Attachment.position)).all()
    assert [a.provider_media_url for a in attachments] == [url0, url1]
    assert [a.content_type for a in attachments] == ["image/png", "application/pdf"]
    assert all(a.status == AttachmentStatus.PENDING for a in attachments)

    drain(services)
    db.expire_all()
    attachments = db.scalars(select(Attachment).order_by(Attachment.position)).all()
    assert all(a.status == AttachmentStatus.STORED for a in attachments)
    assert attachments[0].size_bytes == len(b"\x89PNG-image-bytes")
    stored = services.media.get(db, attachments[0].storage_key)
    assert stored == b"\x89PNG-image-bytes"
    # The model was told the attachment types but cannot see the files.
    assert llm.analysis_requests[0].attachment_types == ["image/png", "application/pdf"]


def test_original_columns_cannot_be_changed_or_deleted(client: TestClient, db: Session) -> None:
    post_inbound(client, inbound_params(BODY))
    message = db.scalars(select(Message)).one()

    message.body_ciphertext = b"rewritten"
    with pytest.raises(ImmutableRecordError):
        db.flush()
    db.rollback()

    message = db.scalars(select(Message)).one()
    message.from_number = "+12025550155"
    with pytest.raises(ImmutableRecordError):
        db.flush()
    db.rollback()

    message = db.scalars(select(Message)).one()
    message.provider_message_id = "SMdifferent"
    with pytest.raises(ImmutableRecordError):
        db.flush()
    db.rollback()

    db.delete(db.scalars(select(Message)).one())
    with pytest.raises(ImmutableRecordError):
        db.flush()
    db.rollback()
    assert _count(db, Message) == 1


def test_webhook_missing_message_id_is_refused(client: TestClient, db: Session) -> None:
    params = {"From": "+12025550101", "To": "+12025550100", "Body": "x"}
    assert post_inbound(client, params).status_code == 400
    assert _count(db, Message) == 0
