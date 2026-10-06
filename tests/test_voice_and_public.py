"""Inbound voice calls (text-only notice) and the public policy pages."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from project_buffer.infrastructure.db.models import AuditEvent, BetaSignup, Message
from project_buffer.services import beta
from project_buffer.services.container import Services
from project_buffer.worker import drain
from tests.conftest import (
    COPARENT,
    OWNER,
    STRANGER,
    TWILIO_NUMBER,
    FakeSms,
    csrf_from,
    make_settings,
    soup_of,
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


def _beta_post(client: TestClient, **overrides: str) -> Any:
    page = client.get("/beta")
    data = {
        "csrf_token": csrf_from(page),
        "name": "Pat Example",
        "email": "pat@example.com",
        "phone": "(202) 555-0143",
        "sms_consent": "yes",
    }
    data.update(overrides)
    return client.post("/beta", data=data)


def test_beta_page_shows_real_consent_wording_and_passes_audit(
    client: TestClient, services: Services
) -> None:
    services.settings = make_settings(
        database_url=services.settings.database_url, sms_brand_name="Alex Example"
    )
    page = client.get("/beta")
    assert page.status_code == 200
    assert audit(page.text) == []
    soup = soup_of(page)
    checkbox = soup.find("input", {"name": "sms_consent"})
    # Consent is never pre-ticked.
    assert not checkbox.has_attr("checked")
    label = soup.find("label", {"for": "sms_consent"}).get_text()
    assert label == beta.consent_text(services.settings)
    for required in (
        "Alex Example",
        "Message frequency varies.",
        "Message and data rates may apply.",
        "Reply STOP to opt out or HELP for help.",
    ):
        assert required in label
    text = visible_text(page)
    assert "private beta" in text and "does not guarantee a place" in text
    assert soup.find("a", href="/privacy") and soup.find("a", href="/terms")


def test_beta_request_is_stored_encrypted_with_consent_and_sends_nothing(
    client: TestClient, db: Session, services: Services, sms: FakeSms
) -> None:
    response = _beta_post(client)
    assert response.status_code == 200
    assert "Request received" in response.text
    assert audit(response.text) == []

    row = db.scalars(select(BetaSignup)).one()
    assert row.consent_text == beta.consent_text(services.settings)
    assert row.consent_version == beta.CONSENT_VERSION and row.consented_at is not None
    for secret in (b"Pat Example", b"pat@example.com", b"2025550143"):
        assert secret not in row.details_ciphertext
    ((_, details),) = beta.list_signups(db, services)
    assert (details.name, details.email, details.phone) == (
        "Pat Example",
        "pat@example.com",
        "+12025550143",
    )
    # Asking for access never triggers a text, to anyone.
    drain(services)
    assert sms.sent == []


def test_beta_request_requires_consent_and_valid_details(client: TestClient, db: Session) -> None:
    cases = {
        "Check the box": {"sms_consent": ""},
        "Enter your name": {"name": "  "},
        "valid email": {"email": "not-an-email"},
        "mobile number with area code": {"phone": "12345"},
    }
    for message, overrides in cases.items():
        response = _beta_post(client, **overrides)
        assert response.status_code == 400, message
        assert message in visible_text(response)
        assert audit(response.text) == []
    assert db.scalars(select(BetaSignup)).all() == []


def test_beta_request_is_deduplicated_throttled_and_csrf_protected(
    client: TestClient, db: Session
) -> None:
    assert _beta_post(client).status_code == 200
    assert _beta_post(client).status_code == 200  # same number: same answer, one row
    assert len(db.scalars(select(BetaSignup)).all()) == 1

    # Automated submissions that fill the hidden field are accepted silently and dropped.
    assert _beta_post(client, phone="2025550144", website="http://spam.example").status_code == 200
    assert len(db.scalars(select(BetaSignup)).all()) == 1

    for index in range(beta.MAX_REQUESTS_PER_IP_PER_HOUR - 1):
        assert _beta_post(client, phone=f"202555015{index}").status_code == 200
    assert _beta_post(client, phone="2025550160").status_code == 429

    forged = client.post(
        "/beta",
        data={
            "csrf_token": "guess",
            "name": "x",
            "email": "x@example.com",
            "phone": "2025550161",
            "sms_consent": "yes",
        },
        cookies={},
    )
    assert forged.status_code in (403, 429)


def test_beta_requests_list_is_owner_only(
    client: TestClient, auth_client: TestClient, services: Services
) -> None:
    _beta_post(auth_client)
    page = auth_client.get("/account/beta-requests")
    assert page.status_code == 200
    assert "Pat Example" in page.text and "pat@example.com" in page.text
    assert audit(page.text) == []
    anonymous = TestClient(auth_client.app, base_url="https://buffer.test", follow_redirects=False)
    assert anonymous.get("/account/beta-requests").status_code == 303


def test_policy_pages_reveal_nothing_private(client: TestClient) -> None:
    for path in ("/privacy", "/terms", "/beta"):
        page = client.get(path).text
        # No names from configuration other than the brand, no owner or co-parent numbers.
        for private in ("Jordan", "Riley", "Sam", COPARENT, OWNER):
            assert private not in page
        assert 'href="/inbox"' not in page  # no signed-in navigation
