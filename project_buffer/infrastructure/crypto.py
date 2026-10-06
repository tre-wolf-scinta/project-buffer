"""Application-level encryption for original message content.

Ciphertext layout: version (1 byte) | key id (4 bytes) | nonce (12 bytes) | AES-GCM output.
The key id lets retired keys keep decrypting old rows after a rotation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"
_KEY_ID_LEN = 4
_NONCE_LEN = 12


class DecryptionError(Exception):
    """Ciphertext could not be authenticated or no matching key is configured."""


class EncryptionService(Protocol):
    def encrypt(self, plaintext: bytes, *, aad: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes, *, aad: bytes) -> bytes: ...

    def mac(self, data: bytes) -> str: ...


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _decode_key(encoded: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(encoded.strip() + "=" * (-len(encoded.strip()) % 4))
    except ValueError as exc:
        raise ValueError("encryption key must be url-safe base64") from exc
    if len(key) != 32:
        raise ValueError("encryption key must decode to exactly 32 bytes")
    return key


def _key_id(key: bytes) -> bytes:
    return hashlib.sha256(b"project-buffer-key-id" + key).digest()[:_KEY_ID_LEN]


class AesGcmEncryptionService:
    def __init__(self, primary_key: str, retired_keys: list[str] | None = None) -> None:
        primary = _decode_key(primary_key)
        self._primary_id = _key_id(primary)
        self._keys = {self._primary_id: AESGCM(primary)}
        for encoded in retired_keys or []:
            key = _decode_key(encoded)
            self._keys[_key_id(key)] = AESGCM(key)
        self._mac_key = hashlib.sha256(b"project-buffer-mac" + primary).digest()

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        sealed = self._keys[self._primary_id].encrypt(nonce, plaintext, aad)
        return _VERSION + self._primary_id + nonce + sealed

    def decrypt(self, ciphertext: bytes, *, aad: bytes) -> bytes:
        header = 1 + _KEY_ID_LEN + _NONCE_LEN
        if len(ciphertext) < header or ciphertext[:1] != _VERSION:
            raise DecryptionError("unrecognised ciphertext format")
        cipher = self._keys.get(ciphertext[1 : 1 + _KEY_ID_LEN])
        if cipher is None:
            raise DecryptionError("no configured key matches this ciphertext")
        try:
            return cipher.decrypt(ciphertext[1 + _KEY_ID_LEN : header], ciphertext[header:], aad)
        except InvalidTag as exc:
            raise DecryptionError("ciphertext failed authentication") from exc

    def mac(self, data: bytes) -> str:
        """Keyed digest of content, for integrity checks that reveal nothing in a dump."""
        return hmac.new(self._mac_key, data, hashlib.sha256).hexdigest()
