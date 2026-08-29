"""Secret hashing and reversible provider-key encryption helpers."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets


class SecretDecryptionError(ValueError):
    """A server-encrypted secret cannot be authenticated or decoded."""


class LocalKeyStoreError(RuntimeError):
    """The per-user local data key is unavailable."""


class UserDataKeyStore:
    """Store one random user DEK in the operating-system credential vault."""

    service_name = "mini-agent-user-data-key"

    @staticmethod
    def _fallback(user_id: str, cause: Exception) -> bytes:
        """Return deterministic test/headless material or preserve the error.

        Some Windows credential backends raise ``OSError``/backend-specific
        exceptions rather than ``KeyringError`` when the process has no
        interactive logon session.  An explicitly configured fallback is the
        documented way to run such environments; without it, fail closed so
        encrypted data is never silently made unrecoverable.
        """

        configured = os.environ.get("MINI_AGENT_LOCAL_DEK_FALLBACK", "")
        if len(configured.encode("utf-8")) < 32:
            raise LocalKeyStoreError(
                "OS credential storage is unavailable and MINI_AGENT_LOCAL_DEK_FALLBACK is not configured."
            ) from cause
        return hashlib.sha256(f"{configured}:{user_id}".encode()).digest()

    def get(self, user_id: str) -> bytes | None:
        if not user_id:
            raise LocalKeyStoreError("A user id is required for local secret encryption.")
        try:
            import keyring

            encoded = keyring.get_password(self.service_name, user_id)
            if encoded:
                key = base64.urlsafe_b64decode(encoded.encode("ascii"))
                if len(key) != 32:
                    raise LocalKeyStoreError("The stored user data key has an invalid length.")
                return key
            return None
        except Exception as exc:
            # Headless test/server environments and Windows keyring backends
            # can surface several exception classes (including OSError).
            # They must explicitly supply stable fallback material; a random
            # process-local key would make persisted data unrecoverable.
            return self._fallback(user_id, exc)

    def set(self, user_id: str, key: bytes) -> None:
        if len(key) != 32:
            raise LocalKeyStoreError("A user data key must contain exactly 32 bytes.")
        try:
            import keyring

            keyring.set_password(self.service_name, user_id, base64.urlsafe_b64encode(key).decode("ascii"))
        except Exception as exc:
            configured = os.environ.get("MINI_AGENT_LOCAL_DEK_FALLBACK", "")
            if len(configured.encode("utf-8")) < 32:
                raise LocalKeyStoreError("OS credential storage is unavailable.") from exc

    def get_or_create(self, user_id: str) -> bytes:
        existing = self.get(user_id)
        if existing is not None:
            return existing
        configured = os.environ.get("MINI_AGENT_LOCAL_DEK_FALLBACK", "")
        key = (
            hashlib.sha256(f"{configured}:{user_id}".encode()).digest()
            if len(configured.encode("utf-8")) >= 32
            else secrets.token_bytes(32)
        )
        self.set(user_id, key)
        # ``set`` may have had to fall back after a keyring backend rejected
        # the write.  Re-read so the first encryption operation uses the same
        # deterministic material that later decryptions will derive.
        return self.get(user_id) or key


_LOCAL_CIPHER_PREFIX = "v3:"
_LOCAL_KEY_STORE = UserDataKeyStore()


def _encrypt_secret(value: str, user_id: str) -> str:
    if not value:
        return ""
    if not user_id:
        raise LocalKeyStoreError("A user id is required for local secret encryption.")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    aad = f"mini-agent-local-secret:v3:{user_id}".encode()
    encrypted = AESGCM(_LOCAL_KEY_STORE.get_or_create(user_id)).encrypt(nonce, value.encode("utf-8"), aad)
    return _LOCAL_CIPHER_PREFIX + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def _decrypt_secret(value: str, user_id: str) -> str:
    if not value:
        return ""
    if value.startswith(_LOCAL_CIPHER_PREFIX):
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            raw = base64.urlsafe_b64decode(value[len(_LOCAL_CIPHER_PREFIX) :].encode("ascii"))
            if len(raw) <= 12:
                raise ValueError
            aad = f"mini-agent-local-secret:v3:{user_id}".encode()
            decrypted = AESGCM(_LOCAL_KEY_STORE.get_or_create(user_id)).decrypt(raw[:12], raw[12:], aad)
            return decrypted.decode("utf-8")
        except (InvalidTag, UnicodeError, ValueError) as exc:
            raise SecretDecryptionError("Stored local credential could not be decrypted.") from exc
    raise SecretDecryptionError("Stored local credential uses an unsupported format.")
