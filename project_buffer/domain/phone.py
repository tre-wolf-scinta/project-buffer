"""Phone number helpers."""

from __future__ import annotations

import re

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_e164(value: str) -> str:
    """Return the number in E.164 form or raise ValueError."""
    cleaned = re.sub(r"[\s\-().]", "", value.strip())
    if not _E164.match(cleaned):
        raise ValueError("phone number must be in E.164 format, for example +12025550123")
    return cleaned


def try_normalize_e164(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_e164(value)
    except ValueError:
        return None


def mask_phone(value: str | None) -> str:
    """Mask all but the last four digits, for logs and neutral notices."""
    if not value:
        return "unknown number"
    digits = re.sub(r"\D", "", value)
    return f"number ending {digits[-4:]}" if len(digits) >= 4 else "unknown number"
