"""Wires configuration to concrete adapters."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from project_buffer.config import Settings
from project_buffer.infrastructure.crypto import AesGcmEncryptionService, EncryptionService
from project_buffer.infrastructure.db.session import create_db_engine, create_session_factory
from project_buffer.infrastructure.llm.base import LLMProvider
from project_buffer.infrastructure.llm.factory import build_llm_provider
from project_buffer.infrastructure.media import DatabaseMediaStorage, MediaStorage
from project_buffer.infrastructure.sms.base import SmsGateway


@dataclass
class Services:
    settings: Settings
    session_factory: sessionmaker[Session]
    crypto: EncryptionService
    llm: LLMProvider
    sms: SmsGateway
    media: MediaStorage


def build_crypto(settings: Settings) -> AesGcmEncryptionService:
    retired = [
        key.strip()
        for key in settings.raw_message_encryption_keys_old.get_secret_value().split(",")
        if key.strip()
    ]
    return AesGcmEncryptionService(settings.raw_message_encryption_key.get_secret_value(), retired)


def build_sms_gateway(settings: Settings) -> SmsGateway:
    if settings.sms_provider == "twilio":
        from project_buffer.infrastructure.sms.twilio_adapter import TwilioGateway

        return TwilioGateway(
            settings.twilio_account_sid,
            settings.twilio_auth_token.get_secret_value(),
            settings.twilio_phone_number,
        )
    from project_buffer.infrastructure.sms.console import ConsoleGateway

    return ConsoleGateway()


def build_services(settings: Settings) -> Services:
    crypto = build_crypto(settings)
    return Services(
        settings=settings,
        session_factory=create_session_factory(create_db_engine(settings.database_url)),
        crypto=crypto,
        llm=build_llm_provider(settings),
        sms=build_sms_gateway(settings),
        media=DatabaseMediaStorage(crypto),
    )
