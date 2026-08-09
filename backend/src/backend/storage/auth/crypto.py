"""Secret hashing and reversible provider-key encryption helpers."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

_SERVER_CIPHER_PREFIX = "v2:"
_SERVER_CIPHER_AAD = b"mini-agent-provider-key:v2"


class SecretDecryptionError(ValueError):
    """A server-encrypted secret cannot be authenticated or decoded."""


class ServerSecretCipher:
    """Stable authenticated encryption for secrets persisted by the Web server."""

    def __init__(self, configured_secret: str) -> None:
        encoded = configured_secret.encode("utf-8")
        if len(encoded) < 32:
            raise ValueError("MINI_AGENT_SECRET_KEY must contain at least 32 UTF-8 bytes.")
        self._key = hashlib.sha256(encoded).digest()

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self._key).encrypt(nonce, value.encode("utf-8"), _SERVER_CIPHER_AAD)
        return _SERVER_CIPHER_PREFIX + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not value:
            return ""
        if not value.startswith(_SERVER_CIPHER_PREFIX):
            raise SecretDecryptionError("Stored provider credential uses an unsupported format.")
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            raw = base64.urlsafe_b64decode(value[len(_SERVER_CIPHER_PREFIX) :].encode("ascii"))
            if len(raw) <= 12:
                raise ValueError
            decrypted = AESGCM(self._key).decrypt(raw[:12], raw[12:], _SERVER_CIPHER_AAD)
            return decrypted.decode("utf-8")
        except (InvalidTag, UnicodeError, ValueError) as exc:
            raise SecretDecryptionError("Stored provider credential could not be decrypted.") from exc


def _key_material() -> bytes:
    configured = os.environ.get("MINI_AGENT_SECRET_KEY", "")
    if configured:
        return hashlib.sha256(configured.encode()).digest()
    try:
        login = os.getlogin()
    except OSError:
        login = ""
    return hashlib.sha256(f"{Path.home()}:{login}".encode()).digest()


def _encrypt_secret(value: str) -> str:
    if not value:
        return ""
    nonce = secrets.token_bytes(16)
    key = _key_material()
    raw = value.encode("utf-8")
    stream = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(raw))
    return base64.urlsafe_b64encode(nonce + stream).decode("ascii")


def _decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeError):
        return ""
    key = _key_material()
    payload = raw[16:]
    decoded = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(payload))
    return decoded.decode("utf-8", errors="replace")
