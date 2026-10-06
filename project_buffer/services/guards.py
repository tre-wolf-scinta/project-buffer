"""Deterministic checks on model output.

The model is asked to remove abuse; these checks do not trust that it did.
They can only reject output or raise urgency, never loosen anything.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Unambiguous profanity and insults. Words with ordinary innocent uses
# ("sick", "crazy", "prick" as in a finger prick) are deliberately left out.
_ABUSIVE = re.compile(
    r"\b(?:"
    r"(?:mother)?fuck\w*|(?:bull|horse|dip)?shit\w*|bitch\w*|asshole\w*|dumbass\w*|jackass\w*|"
    r"bastard\w*|cunt\w*|whore\w*|slut\w*|goddamn\w*|dickhead\w*|retard(?:ed)?|"
    r"worthless|pathetic|loser|deadbeat|idiot\w*|moron\w*|psycho|scumbag\w*"
    r")\b",
    re.IGNORECASE,
)

# Terms in the ORIGINAL that suggest a safety matter. Used only to raise urgency.
_SAFETY = re.compile(
    r"\b(?:"
    r"911|emergency room|urgent care|hospital\w*|ambulance|paramedic\w*|"
    r"police|cops|sheriff|cps|"
    r"went missing|gone missing|ran away|can(?:'|\u2019)?t find (?:him|her|them)|"
    r"not breathing|can(?:'|\u2019)?t breathe|unconscious|unresponsive|overdos\w*|seizure\w*|"
    r"suicid\w*|kill (?:you|him|her|them|myself|yourself)|"
    r"bleeding|concussion|stitches|allergic reaction|poison\w*|"
    r"broke(?:n)? (?:his|her|their|my|an?) (?:arm|leg|wrist|ankle|nose|collarbone|finger|foot)|"
    r"car accident|car crash|gun|knife|weapon|"
    r"hurt (?:you|him|her|them|myself)"
    r")\b"
    r"|\bER\b",
    re.IGNORECASE,
)
# "ER" must be uppercase to count; the alternation above is case-insensitive.
_SAFETY_ER = re.compile(r"\bER\b")

_WORD = re.compile(r"[a-z0-9]+(?:['\u2019][a-z]+)?")

VERBATIM_RUN_LIMIT = 18


class GuardViolation(Exception):
    """Model output failed a deterministic check. ``reason`` contains no message text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def contains_abusive_language(texts: Iterable[str]) -> bool:
    return any(_ABUSIVE.search(text) for text in texts)


def safety_keywords_present(original: str) -> bool:
    for match in _SAFETY.finditer(original):
        if match.group(0).lower() == "er" and not _SAFETY_ER.fullmatch(match.group(0)):
            continue
        return True
    return False


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("\u2019", "'"))


def longest_verbatim_run(original: str, text: str) -> int:
    """Length in words of the longest run of ``text`` copied word-for-word from ``original``."""
    a, b = _words(original), _words(text)
    if not a or not b:
        return 0
    best = 0
    previous = [0] * (len(b) + 1)
    for word_a in a:
        current = [0] * (len(b) + 1)
        for j, word_b in enumerate(b, start=1):
            if word_a == word_b:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


def check_sanitized_output(original: str, texts: list[str]) -> None:
    """Raise GuardViolation if the output looks unsanitized."""
    if contains_abusive_language(texts):
        raise GuardViolation(
            "The output contained profanity or an insult. Restate the substance in neutral "
            "words and leave abusive wording out completely."
        )
    if any(longest_verbatim_run(original, text) >= VERBATIM_RUN_LIMIT for text in texts):
        raise GuardViolation(
            "The output copied a long passage of the message word for word. Summarize in "
            "your own neutral words instead of quoting."
        )
