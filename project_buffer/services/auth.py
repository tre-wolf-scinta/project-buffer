"""Owner authentication: Argon2 passwords, TOTP, recovery codes, server-side sessions."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from project_buffer.config import Settings
from project_buffer.domain.enums import AuthAttemptKind
from project_buffer.infrastructure.crypto import EncryptionService
from project_buffer.infrastructure.db.models import AuthAttempt, RecoveryCode, User, UserSession
from project_buffer.services import audit

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()
# Verified against when the username is unknown, so timing does not reveal that.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

MIN_PASSWORD_LENGTH = 12
RECOVERY_CODE_COUNT = 10
ISSUER = "Project Buffer"
_OWNER_CREATION_LOCK = 7_203_151


class AuthError(Exception):
    pass


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- passwords ---------------------------------------------------------------


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return _hasher.hash(password)


def create_owner(
    session: Session, username: str, password: str, *, actor: str = "cli", ip: str | None = None
) -> User:
    if session.get_bind().dialect.name == "postgresql":
        # Two creations racing each other must not both pass the check below.
        # The lock is released when this transaction ends.
        session.execute(select(func.pg_advisory_xact_lock(_OWNER_CREATION_LOCK)))
    if session.scalar(select(func.count()).select_from(User)):
        raise AuthError("An owner account already exists.")
    user = User(username=username.strip().lower(), password_hash=hash_password(password))
    session.add(user)
    session.flush()
    detail = {"ip": ip[:64]} if ip else {}
    audit.record(
        session,
        actor=actor,
        action="owner_created",
        subject_type="user",
        subject_id=user.id,
        **detail,
    )
    return user


def set_password(session: Session, user: User, password: str, *, actor: str) -> None:
    user.password_hash = hash_password(password)
    revoke_all_sessions(session, user)
    audit.record(
        session, actor=actor, action="password_changed", subject_type="user", subject_id=user.id
    )


def verify_password(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.username == username.strip().lower()))
    try:
        _hasher.verify(user.password_hash if user else _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return None
    if user is not None and _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    return user


# --- throttling ----------------------------------------------------------------


def is_throttled(session: Session, settings: Settings, ip: str, now: datetime) -> bool:
    since = now - timedelta(minutes=settings.login_window_minutes)
    failures = (
        select(func.count())
        .select_from(AuthAttempt)
        .where(AuthAttempt.succeeded.is_(False), AuthAttempt.created_at >= since)
    )
    per_ip = session.scalar(failures.where(AuthAttempt.ip == ip)) or 0
    if per_ip >= settings.login_max_failures_per_ip:
        return True
    return (session.scalar(failures) or 0) >= settings.login_max_failures_global


def record_attempt(
    session: Session, kind: AuthAttemptKind, ip: str, succeeded: bool, now: datetime
) -> None:
    session.add(AuthAttempt(kind=kind, ip=ip[:64], succeeded=succeeded, created_at=now))
    if not succeeded:
        audit.record(session, actor="anonymous", action=f"{kind.value}_failed", ip=ip[:64])


# --- sessions ------------------------------------------------------------------


def session_token_hash(settings: Settings, token: str) -> str:
    """Keyed hash of a session token. SECRET_KEY acts as a pepper, so a database
    dump alone cannot be used to recognise a stolen cookie, and rotating
    SECRET_KEY signs every device out."""
    key = settings.secret_key.get_secret_value().encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def create_session(
    session: Session, settings: Settings, user: User, now: datetime, *, mfa_verified: bool
) -> tuple[UserSession, str]:
    """Create a session and return it with the raw cookie token (stored only as a hash)."""
    token = secrets.token_urlsafe(32)
    record = UserSession(
        user_id=user.id,
        token_hash=session_token_hash(settings, token),
        csrf_token=secrets.token_urlsafe(32),
        mfa_verified_at=now if mfa_verified else None,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(minutes=settings.session_absolute_minutes),
    )
    session.add(record)
    session.flush()
    return record, token


def load_session(
    session: Session, settings: Settings, token: str | None, now: datetime
) -> UserSession | None:
    if not token:
        return None
    record = session.scalar(
        select(UserSession).where(UserSession.token_hash == session_token_hash(settings, token))
    )
    if record is None or record.revoked_at is not None or record.expires_at <= now:
        return None
    if now - record.last_seen_at > timedelta(minutes=settings.session_idle_minutes):
        return None
    if now - record.last_seen_at > timedelta(minutes=1):
        record.last_seen_at = now
    return record


def revoke_session(record: UserSession, now: datetime) -> None:
    record.revoked_at = now


def revoke_all_sessions(
    session: Session, user: User, *, except_id: uuid.UUID | None = None
) -> None:
    from project_buffer.clock import utcnow

    statement = (
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    if except_id is not None:
        statement = statement.where(UserSession.id != except_id)
    session.execute(statement)


# --- TOTP ------------------------------------------------------------------------


def _totp_aad(user: User) -> bytes:
    return f"totp:{user.id}".encode()


def begin_totp_enrollment(session: Session, crypto: EncryptionService, user: User) -> str:
    """Store a new, not-yet-enabled secret and return it for display."""
    secret = pyotp.random_base32()
    user.totp_secret_encrypted = crypto.encrypt(secret.encode(), aad=_totp_aad(user))
    user.totp_enabled_at = None
    user.totp_last_counter = None
    return secret


def pending_totp_secret(crypto: EncryptionService, user: User) -> str | None:
    if user.totp_secret_encrypted is None:
        return None
    return crypto.decrypt(user.totp_secret_encrypted, aad=_totp_aad(user)).decode()


def provisioning_uri(user: User, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name=ISSUER)


def verify_totp(crypto: EncryptionService, user: User, code: str, now: datetime) -> bool:
    """Check a code, allowing one step of clock drift and refusing replays."""
    secret = pending_totp_secret(crypto, user)
    digits = "".join(ch for ch in code if ch.isdigit())
    if secret is None or len(digits) != 6:
        return False
    totp = pyotp.TOTP(secret)
    current = int(now.timestamp()) // totp.interval
    for counter in (current - 1, current, current + 1):
        if user.totp_last_counter is not None and counter <= user.totp_last_counter:
            continue
        if hmac.compare_digest(totp.at(counter * totp.interval), digits):
            user.totp_last_counter = counter
            return True
    return False


def confirm_totp_enrollment(
    session: Session, crypto: EncryptionService, user: User, code: str, now: datetime
) -> list[str] | None:
    """Enable TOTP if the code is right. Returns fresh recovery codes, shown once."""
    if not verify_totp(crypto, user, code, now):
        return None
    user.totp_enabled_at = now
    audit.record(
        session, actor="owner", action="mfa_enabled", subject_type="user", subject_id=user.id
    )
    return regenerate_recovery_codes(session, user)


# --- recovery codes ----------------------------------------------------------------


def _normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def regenerate_recovery_codes(session: Session, user: User) -> list[str]:
    for old in session.scalars(select(RecoveryCode).where(RecoveryCode.user_id == user.id)):
        session.delete(old)
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = secrets.token_hex(8)
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:]}")
        session.add(RecoveryCode(user_id=user.id, code_hash=_sha256(raw)))
    audit.record(
        session,
        actor="owner",
        action="recovery_codes_generated",
        subject_type="user",
        subject_id=user.id,
    )
    return codes


def use_recovery_code(session: Session, user: User, code: str, now: datetime) -> bool:
    record = session.scalar(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.code_hash == _sha256(_normalize_recovery(code)),
            RecoveryCode.used_at.is_(None),
        )
    )
    if record is None:
        return False
    record.used_at = now
    audit.record(
        session, actor="owner", action="recovery_code_used", subject_type="user", subject_id=user.id
    )
    return True


def remaining_recovery_codes(session: Session, user: User) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(RecoveryCode)
            .where(RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None))
        )
        or 0
    )
