"""Inbound voice calls (text-only notice) and the public policy pages."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from project_buffer.infrastructure.db.models import AuditEvent, Message
from project_buffer.services.container import Services
from project_buffer.worker import drain
from tests.conftest import (
    COPARENT,
    OWNER,
    STRANGER,
    TWILIO_NUMBER,
    FakeSms,
    make_settings,
    twilio_signature,
    visible_text,
)
from tests.test_accessibility import audit

VOICE = "/webhooks/twilio/voice"


def _call(
    client: TestClient, caller: str, sid: str = "CA0001", signature: str | None = None
) -> Any:
    params = {"CallSid": sid, "From": caller, "To": TWILIO_NUMBER, "CallStatus": "ringing"}
    headers = {
        "X-Twilio-Signature": signature
        if signature is not None
        else twilio_signature(VOICE, params)
    }
    return client.post(VOICE, data=params, headers=headers)


def test_call_from_coparent_hears_text_only_notice_and_owner_is_told(
    client: TestClient, db: Session, services: Services, sms: FakeSms
) -> None:
    response = _call(client, COPARENT)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    # The call is answered with speech and ended. It is never forwarded or recorded.
    assert "<Say>This number accepts text messages only." in response.text
    assert "<Hangup/>" in response.text
    for verb in ("<Dial", "<Record", "<Gather", "<Redirect"):
        assert verb not in response.text

    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "call_received")).one()
    assert event.detail == {"caller": "coparent", "from_number": COPARENT}
    assert event.subject_id == "CA0001"
    assert db.scalars(select(Message)).all() == []

    drain(services)
    notices = [m["body"] for m in sms.sent_to(OWNER)]
    assert len(notices) == 1
    assert notices[0].startswith("Jordan called the co-parenting number at ")
    assert "No voicemail was recorded." in notices[0]
    assert sms.sent_to(COPARENT) == []


def test_twilio_retrying_the_same_call_notifies_once(
    client: TestClient, services: Services, sms: FakeSms
) -> None:
    _call(client, COPARENT, sid="CA0002")
    _call(client, COPARENT, sid="CA0002")
    drain(services)
    assert len(sms.sent_to(OWNER)) == 1


def test_unknown_callers_are_throttled_and_masked(
    client: TestClient, services: Services, sms: FakeSms
) -> None:
    for index in range(3):
        assert _call(client, STRANGER, sid=f"CA10{index}").status_code == 200
    drain(services)
    notices = [m["body"] for m in sms.sent_to(OWNER)]
    assert len(notices) == 1
    assert "An unrecognized number ending 0199 called" in notices[0]
    assert STRANGER not in notices[0]


def test_owner_calling_the_number_sends_no_notice(
    client: TestClient, services: Services, sms: FakeSms
) -> None:
    assert "<Hangup/>" in _call(client, OWNER).text
    drain(services)
    assert sms.sent == []


def test_forged_voice_webhook_is_rejected(client: TestClient, db: Session) -> None:
    assert _call(client, COPARENT, signature="forged").status_code == 403
    assert db.scalars(select(AuditEvent)).all() == []


def test_voice_greeting_is_configurable_and_escaped(client: TestClient, services: Services) -> None:
    services.settings = make_settings(
        database_url=services.settings.database_url, voice_greeting="Texts only <please> & thanks"
    )
    response = _call(client, COPARENT, sid="CA0003")
    assert "<Say>Texts only &lt;please&gt; &amp; thanks</Say>" in response.text


def test_policy_pages_are_public_and_meet_carrier_requirements(
    client: TestClient, services: Services
) -> None:
    services.settings = make_settings(
        database_url=services.settings.database_url, sms_brand_name="Alex Example"
    )
    privacy = client.get("/privacy")
    assert privacy.status_code == 200
    text = visible_text(privacy)
    assert "<title>Privacy Policy - Buffer</title>" in privacy.text
    assert "Alex Example" in text
    assert (
        "We do not sell or share your SMS opt-in data or personal information with third "
        "parties for marketing purposes." in text
    )
    assert audit(privacy.text) == []

    terms = client.get("/terms")
    assert terms.status_code == 200
    text = visible_text(terms)
    assert "Terms & Conditions" in text and "SMS Terms" in text
    assert "Message and data rates may apply." in text
    assert "Alex Example" in text and "STOP" in text and "HELP" in text
    assert audit(terms.text) == []


def test_policy_pages_reveal_nothing_private(client: TestClient) -> None:
    for path in ("/privacy", "/terms"):
        page = client.get(path).text
        # No names from configuration other than the brand, no owner or co-parent numbers.
        for private in ("Jordan", "Riley", "Sam", COPARENT, OWNER):
            assert private not in page
        assert 'href="/inbox"' not in page  # no signed-in navigation
