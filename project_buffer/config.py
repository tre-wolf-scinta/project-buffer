"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from project_buffer.domain.phone import normalize_e164

DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-5", "fake": "fake"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./dev.db"
    application_base_url: str = "http://localhost:8000"

    secret_key: SecretStr
    raw_message_encryption_key: SecretStr
    # Comma-separated retired keys, still accepted for decryption.
    raw_message_encryption_keys_old: SecretStr = SecretStr("")

    sms_provider: Literal["twilio", "console"] = "console"
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str = "+12025550100"
    coparent_phone_number: str = "+12025550101"
    owner_phone_number: str = "+12025550102"

    coparent_display_name: str = "Co-parent"
    owner_display_name: str = "Owner"
    # Comma-separated first names; helps the model recognise who is a child.
    children_names: str = ""
    owner_timezone: str = "America/New_York"
    # Name shown on the public Privacy Policy and Terms pages. Carriers require it to
    # match the registered A2P brand name.
    sms_brand_name: str = ""
    # What a caller hears. This number is text-only; calls are never connected.
    voice_greeting: str = (
        "This number accepts text messages only. Calls are not answered and voicemail is "
        "not available. Please send a text message instead. If this is an emergency, "
        "hang up and call 9 1 1."
    )

    llm_provider: Literal["anthropic", "openai", "fake"] = "fake"
    llm_model: str = ""
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: float = 60.0

    notify_mode: Literal["summary", "link_only", "off"] = "summary"
    notify_emergency_repeat_minutes: int = 5
    notify_emergency_max_repeats: int = 2
    notify_unrecognized_senders: bool = True
    processing_delay_notice_minutes: int = 5

    job_max_attempts: int = Field(default=7, ge=1)
    job_visibility_timeout_seconds: int = 300
    worker_poll_seconds: float = 2.0
    worker_stale_seconds: int = 120

    session_idle_minutes: int = 60 * 24 * 7
    session_absolute_minutes: int = 60 * 24 * 30
    login_max_failures_per_ip: int = 10
    login_max_failures_global: int = 100
    login_window_minutes: int = 15

    max_media_bytes: int = 20 * 1024 * 1024
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        # Render and Heroku hand out postgres:// or postgresql:// URLs.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @field_validator("twilio_phone_number", "coparent_phone_number", "owner_phone_number")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        return normalize_e164(value)

    @field_validator("application_base_url")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("owner_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        numbers = {self.twilio_phone_number, self.coparent_phone_number, self.owner_phone_number}
        if len(numbers) != 3:
            raise ValueError(
                "TWILIO_PHONE_NUMBER, COPARENT_PHONE_NUMBER and OWNER_PHONE_NUMBER must differ"
            )
        if len(self.secret_key.get_secret_value()) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        if self.is_production:
            self._validate_production()
        return self

    def _validate_production(self) -> None:
        problems: list[str] = []
        if not self.application_base_url.startswith("https://"):
            problems.append("APPLICATION_BASE_URL must be https")
        if not self.database_url.startswith("postgresql"):
            problems.append("DATABASE_URL must be PostgreSQL")
        if self.sms_provider != "twilio":
            problems.append("SMS_PROVIDER must be twilio")
        if not self.twilio_account_sid or not self.twilio_auth_token.get_secret_value():
            problems.append("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are required")
        if self.llm_provider == "fake":
            problems.append("LLM_PROVIDER must be anthropic or openai")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key.get_secret_value():
            problems.append("ANTHROPIC_API_KEY is required")
        if self.llm_provider == "openai" and not self.openai_api_key.get_secret_value():
            problems.append("OPENAI_API_KEY is required")
        if problems:
            raise ValueError("Invalid production configuration: " + "; ".join(problems))

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cookie_secure(self) -> bool:
        return self.application_base_url.startswith("https://")

    @property
    def session_cookie_name(self) -> str:
        # The __Host- prefix requires Secure, so it is only usable over https.
        return "__Host-pb_session" if self.cookie_secure else "pb_session"

    @property
    def resolved_llm_model(self) -> str:
        return self.llm_model or DEFAULT_MODELS[self.llm_provider]

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.owner_timezone)

    @property
    def children(self) -> list[str]:
        return [name.strip() for name in self.children_names.split(",") if name.strip()]

    def url_for_message(self, message_id: object) -> str:
        return f"{self.application_base_url}/messages/{message_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
