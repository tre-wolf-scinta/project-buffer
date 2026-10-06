"""Private, durable attachment storage behind an interface."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from project_buffer.infrastructure.crypto import EncryptionService
from project_buffer.infrastructure.db.models import MediaBlob


class MediaStorage(Protocol):
    """Backends that do not use the database (for example S3) ignore ``session``."""

    backend: str

    def put(self, session: Session, key: str, data: bytes) -> None: ...

    def get(self, session: Session, key: str) -> bytes: ...


class DatabaseMediaStorage:
    """Stores encrypted bytes in Postgres so attachments share the messages' backup."""

    backend = "database"

    def __init__(self, crypto: EncryptionService) -> None:
        self._crypto = crypto

    def put(self, session: Session, key: str, data: bytes) -> None:
        ciphertext = self._crypto.encrypt(data, aad=f"media:{key}".encode())
        session.merge(MediaBlob(storage_key=key, ciphertext=ciphertext))

    def get(self, session: Session, key: str) -> bytes:
        blob = session.get(MediaBlob, key)
        if blob is None:
            raise KeyError(key)
        return self._crypto.decrypt(blob.ciphertext, aad=f"media:{key}".encode())
