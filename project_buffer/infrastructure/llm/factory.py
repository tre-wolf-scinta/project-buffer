from __future__ import annotations

from project_buffer.config import Settings
from project_buffer.infrastructure.llm.base import LLMProvider


def build_llm_provider(settings: Settings) -> LLMProvider:
    # Vendor SDKs are imported lazily so only the configured one is loaded.
    if settings.llm_provider == "anthropic":
        from project_buffer.infrastructure.llm.anthropic_adapter import AnthropicProvider

        return AnthropicProvider(
            settings.anthropic_api_key.get_secret_value(),
            settings.resolved_llm_model,
            settings.llm_timeout_seconds,
        )
    if settings.llm_provider == "openai":
        from project_buffer.infrastructure.llm.openai_adapter import OpenAIProvider

        return OpenAIProvider(
            settings.openai_api_key.get_secret_value(),
            settings.resolved_llm_model,
            settings.llm_timeout_seconds,
        )
    from project_buffer.infrastructure.llm.fake import FakeProvider

    return FakeProvider()
