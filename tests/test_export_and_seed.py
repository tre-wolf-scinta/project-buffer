"""Record export, triage actions, demo seed data."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from project_buffer.domain.enums import DeliveryStatus, Direction, DraftStatus
from project_buffer.infrastructure.db.models import AuditEvent, Draft, Message, User
from project_buffer.infrastructure.sms.base import FetchedMedia
from project_buffer.seed import seed_demo
from project_buffer.services.container import Services
from project_buffer.worker import drain
from tests.conftest import (
    FakeSms,
    ScriptedLLM,
    analysis,
    inbound_params,
    post_inbound,
    visible_text,
)

RAW = "You are impossible. OTTER-MARKER-2203. Dentist for Sam on Tuesday at 3:30."


def _export(client: TestClient, *, originals: bool) -> zipfile.ZipFile:
    today = datetime.now(UTC).date()
    data = {
        "start_date": (today - timedelta(days=2)).isoformat(),
        "end_date": (today + timedelta(days=1)).isoformat(),
        "csrf_token": client.csrf,
    }
    if originals:
        data["include_originals"] = "1"
    response = client.post("/export", data=data)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    return zipfile.ZipFile(io.BytesIO(response.content))


def _setup(client: TestClient, services: Services, llm: ScriptedLLM, sms: FakeSms) -> None:
    url = "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages/MM1/Media/ME0"
    sms.media[url] = FetchedMedia(content=b"\x89PNG-bytes", content_type="image/png")
    llm.analyses.append(
        analysis(short_summary="Sam has a dentist appointment on Tuesday.", topic="Dentist")
    )
    post_inbound(
        client,
        inbound_params(RAW, NumMedia="1", MediaUrl0=url, MediaContentType0="image/png"),
    )
    drain(services)


def test_export_without_originals_contains_no_original_text(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    _setup(auth_client, services, llm, sms)
    archive = _export(auth_client, originals=False)
    names = set(archive.namelist())
    assert "originals.json" not in names
    assert not any(name.startswith("attachments/") for name in names)
    for name in names:
        assert b"OTTER-MARKER" not in archive.read(name), name
    summaries = json.loads(archive.read("sanitized_summaries.json"))
    assert summaries[0]["ai_generated"] is True
    assert summaries[0]["analysis"]["topic"] == "Dentist"
    assert json.loads(archive.read("manifest.json"))["originals_included"] is False


def test_export_with_originals_keeps_records_separate_and_exact(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    _setup(auth_client, services, llm, sms)
    archive = _export(auth_client, originals=True)

    originals = json.loads(archive.read("originals.json"))
    assert len(originals) == 1
    record = originals[0]
    assert record["original_text"] == RAW
    assert record["integrity_verified"] is True
    assert record["direction"] == "inbound"
    assert record["provider_payload"]["Body"] == RAW
    assert record["provider_message_id"].startswith("SMin")
    assert archive.read(record["attachments"][0]["file"]) == b"\x89PNG-bytes"

    # The summary file is clearly labelled and never contains the original.
    assert b"OTTER-MARKER" not in archive.read("sanitized_summaries.json")
    assert b"OTTER-MARKER" not in archive.read("messages.csv")
    assert "AI-GENERATED" in archive.read("README.txt").decode()
    header = archive.read("messages.csv").decode().splitlines()[0]
    assert "provider_message_id" in header and "body_hmac_sha256" in header

    db.expire_all()
    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "export_created")).one()
    assert event.detail["originals_included"] is True


def test_export_rejects_bad_dates(auth_client: TestClient) -> None:
    response = auth_client.post(
        "/export",
        data={"start_date": "2026-02-01", "end_date": "2026-01-01", "csrf_token": auth_client.csrf},
    )
    assert response.status_code == 400
    assert "end date is before the start date" in response.text


def test_attachment_download_requires_post_and_is_audited(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM, sms: FakeSms
) -> None:
    _setup(auth_client, services, llm, sms)
    message = db.scalars(select(Message)).one()
    attachment = message.attachments[0]
    url = f"/messages/{message.id}/attachments/{attachment.id}"
    assert auth_client.get(url).status_code in (404, 405)
    response = auth_client.post(url, data={"csrf_token": auth_client.csrf})
    assert response.status_code == 200 and response.content == b"\x89PNG-bytes"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    db.expire_all()
    assert db.scalars(select(AuditEvent).where(AuditEvent.action == "attachment_downloaded")).one()
    # The summary page mentions the attachment but does not link to its content.
    detail = auth_client.get(f"/messages/{message.id}")
    assert str(attachment.id) not in detail.text


def test_handled_state_and_filters(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    llm.analyses.append(analysis(topic="Needs a reply", response_needed=True))
    llm.analyses.append(analysis(topic="Just information"))
    post_inbound(auth_client, inbound_params("one"))
    post_inbound(auth_client, inbound_params("two"))
    drain(services)
    first = db.scalars(select(Message).order_by(Message.created_at)).first()

    needs = visible_text(auth_client.get("/inbox?filter=needs_response"))
    assert "Needs a reply" in needs and "Just information" not in needs
    assert "Unread (2)" in visible_text(auth_client.get("/inbox"))

    # Opening a message marks it read; handling moves it between filters.
    auth_client.get(f"/messages/{first.id}")
    assert "Unread (1)" in visible_text(auth_client.get("/inbox"))
    response = auth_client.post(
        f"/messages/{first.id}/handled",
        data={"handled": "1", "return_to": "inbox", "csrf_token": auth_client.csrf},
    )
    assert response.headers["location"] == "/inbox?notice=handled"
    assert "Needs a reply" not in visible_text(auth_client.get("/inbox?filter=open"))
    assert "Needs a reply" in visible_text(auth_client.get("/inbox?filter=handled"))

    auth_client.post(
        f"/messages/{first.id}/handled", data={"handled": "0", "csrf_token": auth_client.csrf}
    )
    assert "Needs a reply" in visible_text(auth_client.get("/inbox?filter=open"))


def test_quarantined_message_can_be_released_for_processing(
    auth_client: TestClient, db: Session, services: Services, llm: ScriptedLLM
) -> None:
    post_inbound(auth_client, inbound_params("From my other phone", sender="+12025550199"))
    message = db.scalars(select(Message)).one()
    page = visible_text(auth_client.get(f"/messages/{message.id}"))
    assert "Process this message" in page and "other phone" not in page

    llm.analyses.append(analysis(topic="New number"))
    auth_client.post(f"/messages/{message.id}/reprocess", data={"csrf_token": auth_client.csrf})
    drain(services)
    assert "New number" in visible_text(auth_client.get("/inbox"))


def test_demo_seed_covers_every_required_scenario(
    client: TestClient, db: Session, services: Services
) -> None:
    now = datetime.now(UTC)
    assert seed_demo(db, services, now) == 6
    db.commit()
    assert seed_demo(db, services, now) == 0  # idempotent

    inbound = db.scalars(select(Message).where(Message.direction == Direction.INBOUND)).all()
    topics = {m.current_analysis.topic for m in inbound}
    assert topics == {
        "Soccer practice pickup",
        "School transportation",
        "Sam: dentist appointment",
        "Sam injured at school",
        "Custody schedule allegation",
    }
    assert sum(m.urgency.value == "emergency" for m in inbound) == 1
    outbound = db.scalars(select(Message).where(Message.direction == Direction.OUTBOUND)).one()
    assert outbound.delivery_status == DeliveryStatus.DELIVERED
    assert len(outbound.delivery_events) == 2
    assert (
        db.scalar(select(func.count()).select_from(Draft).where(Draft.status == DraftStatus.READY))
        == 1
    )
    assert db.scalars(select(User)).one().mfa_enabled

    # Seeded analyses pass the same guards as live model output.
    from project_buffer.domain.analysis import MessageAnalysisResult
    from project_buffer.services import guards

    for message in inbound:
        original = services.crypto.decrypt(message.body_ciphertext, aad=message.body_aad()).decode()
        result = MessageAnalysisResult.model_validate(message.current_analysis.structured)
        guards.check_sanitized_output(original, result.text_fields())


def test_committed_fixtures_use_only_reserved_fictional_numbers() -> None:
    """555-01xx numbers are reserved for fiction; nothing real belongs in the repo."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pattern = re.compile(r"\+1\d{10}")
    for path in [
        *root.glob("project_buffer/**/*.py"),
        *root.glob("tests/**/*.py"),
        root / ".env.example",
    ]:
        if not path.exists():
            continue
        for number in pattern.findall(path.read_text(encoding="utf-8")):
            assert re.fullmatch(r"\+1\d{3}55501\d{2}", number), f"{number} in {path.name}"
