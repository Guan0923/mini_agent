"""OS-backed encryption for local provider credentials."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets


class SecretDecryptionError(ValueError):
    """A stored provider credential cannot be authenticated or decoded."""


class LocalKeyStoreError(RuntimeError):
    """The installation-local data key is unavailable."""


class LocalDataKeyStore:
    """Store one installation key in the operating-system credential vault."""

    service_name = "mini-agent-local-data-key"
    account_name = "default"

    @staticmethod
    def _fallback(cause: Exception) -> bytes:
        configured = os.environ.get("MINI_AGENT_LOCAL_DEK_FALLBACK", "")
        if len(configured.encode("utf-8")) < 32:
            raise LocalKeyStoreError(
                "OS credential storage is unavailable and MINI_AGENT_LOCAL_DEK_FALLBACK is not configured."
            ) from cause
        return hashlib.sha256(configured.encode()).digest()

    def get(self) -> bytes | None:
        try:
            import keyring

            encoded = keyring.get_password(self.service_name, self.account_name)
            if not encoded:
                return None
            key = base64.urlsafe_b64decode(encoded.encode("ascii"))
            if len(key) != 32:
                raise LocalKeyStoreError("The stored local data key has an invalid length.")
            return key
        except Exception as exc:
            return self._fallback(exc)

    def set(self, key: bytes) -> None:
        if len(key) != 32:
            raise LocalKeyStoreError("A local data key must contain exactly 32 bytes.")
        try:
            import keyring

            keyring.set_password(self.service_name, self.account_name, base64.urlsafe_b64encode(key).decode("ascii"))
        except Exception as exc:
            configured = os.environ.get("MINI_AGENT_LOCAL_DEK_FALLBACK", "")
            if len(configured.encode("utf-8")) < 32:
                raise LocalKeyStoreError("OS credential storage is unavailable.") from exc

    def get_or_create(self) -> bytes:
        existing = self.get()
        if existing is not None:
            return existing
        configured = os.environ.get("MINI_AGENT_LOCAL_DEK_FALLBACK", "")
        key = (
            hashlib.sha256(configured.encode()).digest()
            if len(configured.encode("utf-8")) >= 32
            else secrets.token_bytes(32)
        )
        self.set(key)
        return self.get() or key


_CIPHER_PREFIX = "v4:"
_KEY_STORE = LocalDataKeyStore()


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    aad = b"mini-agent-local-secret:v4"
    encrypted = AESGCM(_KEY_STORE.get_or_create()).encrypt(nonce, value.encode("utf-8"), aad)
    return _CIPHER_PREFIX + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_CIPHER_PREFIX):
        raise SecretDecryptionError("Stored local credential uses an unsupported format.")
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.urlsafe_b64decode(value[len(_CIPHER_PREFIX) :].encode("ascii"))
        if len(raw) <= 12:
            raise ValueError
        decrypted = AESGCM(_KEY_STORE.get_or_create()).decrypt(raw[:12], raw[12:], b"mini-agent-local-secret:v4")
        return decrypted.decode("utf-8")
    except (InvalidTag, UnicodeError, ValueError) as exc:
        raise SecretDecryptionError("Stored local credential could not be decrypted.") from exc


__all__ = ["LocalKeyStoreError", "SecretDecryptionError", "decrypt_secret", "encrypt_secret"]
