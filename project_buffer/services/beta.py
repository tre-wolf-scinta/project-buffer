"""Beta access requests.

The public form records who asked for access and the exact SMS consent wording
they agreed to. It sends nothing: no text or email goes out when someone asks.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from project_buffer.config import Settings
from project_buffer.domain.phone import normalize_e164
from project_buffer.infrastructure.db.models import BetaSignup
from project_buffer.services import audit
from project_buffer.services.container import Services

CONSENT_VERSION = "2026-10-06"
MAX_REQUESTS_PER_IP_PER_HOUR = 5

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def consent_text(settings: Settings) -> str:
    """The wording shown next to the consent checkbox, stored with every request."""
    brand = settings.sms_brand_name or "the operator of this service"
    return (
        f"I agree to receive text messages from {brand} at the mobile number above about my "
        "co-parenting messages, such as a notice when a new message arrives. Message "
        "frequency varies. Message and data rates may apply. Reply STOP to opt out or HELP "
        "for help. Consent is not a condition of any purchase."
    )


def normalize_us_phone(value: str) -> str:
    """Accept common US formats as well as E.164."""
    digits = re.sub(r"\D", "", value)
    if not value.strip().startswith("+"):
        if len(digits) == 10:
            return normalize_e164("+1" + digits)
        if len(digits) == 11 and digits.startswith("1"):
            return normalize_e164("+" + digits)
    return normalize_e164(value)


@dataclass(frozen=True)
class SignupDetails:
    name: str
    email: str
    phone: str


class SignupError(Exception):
    """A problem with the request, worded for display on the form."""


def validate(name: str, email: str, phone: str, consent: bool) -> SignupDetails:
    name, email = " ".join(name.split()), email.strip()
    if not name or len(name) > 80:
        raise SignupError("Enter your name, up to 80 characters.")
    if len(email) > 254 or not _EMAIL.match(email):
        raise SignupError("Enter a valid email address, for example name@example.com.")
    try:
        normalized = normalize_us_phone(phone)
    except ValueError:
        raise SignupError(
            "Enter a mobile number with area code, for example 325 555 0123."
        ) from None
    if not consent:
        raise SignupError(
            "Check the box to agree to receive text messages. Without it we cannot add you."
        )
    return SignupDetails(name=name, email=email, phone=normalized)


def is_throttled(session: Session, ip: str, now: datetime) -> bool:
    recent = session.scalar(
        select(func.count())
        .select_from(BetaSignup)
        .where(BetaSignup.ip == ip, BetaSignup.created_at >= now - timedelta(hours=1))
    )
    return (recent or 0) >= MAX_REQUESTS_PER_IP_PER_HOUR


def record_signup(
    session: Session, services: Services, details: SignupDetails, *, ip: str, now: datetime
) -> bool:
    """Store a request. Returns False if this mobile number had already asked."""
    crypto = services.crypto
    signup = BetaSignup(
        id=uuid.uuid4(),
        details_ciphertext=b"",
        phone_mac=crypto.mac(details.phone.encode()),
        consent_text=consent_text(services.settings),
        consent_version=CONSENT_VERSION,
        consented_at=now,
        ip=ip[:64],
        created_at=now,
    )
    signup.details_ciphertext = crypto.encrypt(
        json.dumps(details.__dict__).encode(), aad=signup.details_aad()
    )
    try:
        with session.begin_nested():
            session.add(signup)
            session.flush()
    except IntegrityError:
        return False
    audit.record(
        session,
        actor="public",
        action="beta_access_requested",
        subject_type="beta_signup",
        subject_id=signup.id,
        consent_version=CONSENT_VERSION,
    )
    return True


def list_signups(session: Session, services: Services) -> list[tuple[BetaSignup, SignupDetails]]:
    rows = session.scalars(select(BetaSignup).order_by(BetaSignup.created_at.desc())).all()
    result = []
    for row in rows:
        raw = services.crypto.decrypt(row.details_ciphertext, aad=row.details_aad())
        result.append((row, SignupDetails(**json.loads(raw))))
    return result
