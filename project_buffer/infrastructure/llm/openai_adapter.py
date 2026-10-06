"""OpenAI adapter using a single forced function call for structured output."""

from __future__ import annotations

import json
from typing import Any

import openai

from project_buffer.infrastructure.llm.base import (
    LLMInvalidOutputError,
    LLMPermanentError,
    LLMTransientError,
    StructuredLLMProvider,
)


class OpenAIProvider(StructuredLLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        self.model = model
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=2)

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
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": tool_description,
                            "parameters": schema,
                        },
                    }
                ],
                tool_choice={"type": "function", "function": {"name": tool_name}},
            )
        except (openai.APIConnectionError, openai.RateLimitError) as exc:
            raise LLMTransientError(type(exc).__name__) from None
        except openai.APIStatusError as exc:
            label = f"{type(exc).__name__} status={exc.status_code}"
            if exc.status_code >= 500 or exc.status_code in (408, 409):
                raise LLMTransientError(label) from None
            raise LLMPermanentError(label) from None

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMInvalidOutputError("output truncated")
        for call in choice.message.tool_calls or []:
            if call.type == "function" and call.function.name == tool_name:
                try:
                    parsed = json.loads(call.function.arguments)
                except json.JSONDecodeError:
                    raise LLMInvalidOutputError("tool arguments were not JSON") from None
                if isinstance(parsed, dict):
                    return parsed
        raise LLMInvalidOutputError(f"no tool call (finish_reason={choice.finish_reason})")
