"""Anthropic adapter using a single forced tool call for structured output."""

from __future__ import annotations

from typing import Any

import anthropic

from project_buffer.infrastructure.llm.base import (
    LLMInvalidOutputError,
    LLMPermanentError,
    LLMTransientError,
    StructuredLLMProvider,
)


class AnthropicProvider(StructuredLLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        self.model = model
        # The SDK retries 429/5xx/connection errors itself; the job queue retries beyond that.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=2)

    def _call(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[
                    {"name": tool_name, "description": tool_description, "input_schema": schema}
                ],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError) as exc:
            raise LLMTransientError(type(exc).__name__) from None
        except anthropic.APIStatusError as exc:
            label = f"{type(exc).__name__} status={exc.status_code}"
            if exc.status_code >= 500 or exc.status_code in (408, 409):
                raise LLMTransientError(label) from None
            raise LLMPermanentError(label) from None

        if response.stop_reason == "max_tokens":
            raise LLMInvalidOutputError("output truncated")
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                if not isinstance(block.input, dict):
                    break
                return block.input
        raise LLMInvalidOutputError(f"no tool call (stop_reason={response.stop_reason})")
