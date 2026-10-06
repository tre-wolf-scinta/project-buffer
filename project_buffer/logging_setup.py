"""Logging configuration.

Rule for the whole codebase: log identifiers and states, never message text,
prompts, or secrets. The filter below is a last line of defence that masks
phone numbers; it is not a substitute for that rule.
"""

from __future__ import annotations

import logging
import re

_PHONE = re.compile(r"\+\d{6,11}(\d{4})\b")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _PHONE.sub(r"+*******\1", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # These libraries log request details at DEBUG/INFO that could include content.
    for noisy in ("httpx", "httpcore", "anthropic", "openai", "twilio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
