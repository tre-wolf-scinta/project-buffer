"""LLM provider interface.

Application code depends only on ``LLMProvider``. Vendor adapters implement the
single ``_call`` method of ``StructuredLLMProvider``; prompt construction and
schema validation are shared so every vendor gets identical safeguards.
"""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from project_buffer.domain.analysis import DraftReplyResult, MessageAnalysisResult
from project_buffer.infrastructure.llm import prompts


class LLMError(Exception):
    """Base class. Messages must never contain prompt or message content."""

    retryable = True


class LLMTransientError(LLMError):
    """Timeouts, rate limits, 5xx. Worth retrying."""


class LLMInvalidOutputError(LLMError):
    """The model answered, but not with usable structured output."""


class LLMPermanentError(LLMError):
    """Configuration problems such as a rejected API key. Retrying will not help."""

    retryable = False


@dataclass(frozen=True)
class AnalysisRequest:
    message_text: str
    sender_name: str
    recipient_name: str
    received_at_local: str
    children_names: list[str] = field(default_factory=list)
    attachment_types: list[str] = field(default_factory=list)
    # Set when a previous output was rejected by a guard.
    retry_hint: str | None = None


@dataclass(frozen=True)
class DraftRequest:
    instruction: str
    owner_name: str
    recipient_name: str
    today_local: str
    children_names: list[str] = field(default_factory=list)
    # Sanitized summary of the message being answered. Never the original text.
    context_summary: str | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    def analyze_message(self, request: AnalysisRequest) -> MessageAnalysisResult: ...

    def draft_reply(self, request: DraftRequest) -> DraftReplyResult: ...


class StructuredLLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def _call(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Force one tool call and return its arguments. Raise an ``LLMError`` on failure."""

    def analyze_message(self, request: AnalysisRequest) -> MessageAnalysisResult:
        boundary = secrets.token_hex(8)
        raw = self._call(
            system=prompts.analysis_system_prompt(request),
            user=prompts.analysis_user_prompt(request, boundary),
            tool_name=prompts.ANALYSIS_TOOL,
            tool_description=prompts.ANALYSIS_TOOL_DESCRIPTION,
            schema=MessageAnalysisResult.model_json_schema(),
        )
        try:
            return MessageAnalysisResult.model_validate(raw)
        except ValidationError as exc:
            # Only the count is reported; error details could echo message text.
            raise LLMInvalidOutputError(f"schema validation failed ({exc.error_count()})") from None

    def draft_reply(self, request: DraftRequest) -> DraftReplyResult:
        raw = self._call(
            system=prompts.draft_system_prompt(request),
            user=prompts.draft_user_prompt(request),
            tool_name=prompts.DRAFT_TOOL,
            tool_description=prompts.DRAFT_TOOL_DESCRIPTION,
            schema=DraftReplyResult.model_json_schema(),
        )
        try:
            return DraftReplyResult.model_validate(raw)
        except ValidationError as exc:
            raise LLMInvalidOutputError(f"schema validation failed ({exc.error_count()})") from None
