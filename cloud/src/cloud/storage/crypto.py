"""Cloud-only key envelope primitives."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets


class SecretDecryptionError(ValueError):
    """A cloud key envelope could not be authenticated or decoded."""


class CloudMasterCipher:
    """Versioned AES-GCM wrapper for per-user data encryption keys."""

    def __init__(self, configured_secret: str | None = None) -> None:
        self.active_version = os.environ.get("MINI_AGENT_ACTIVE_MASTER_KEY_VERSION", "v1").strip() or "v1"
        self.configured_secret = (configured_secret or "").strip()

    def _key(self, version: str) -> bytes:
        configured = os.environ.get(f"MINI_AGENT_MASTER_KEY_{version.upper()}", "")
        if not configured:
            configured = self.configured_secret or os.environ.get("MINI_AGENT_SECRET_KEY", "")
        if len(configured.encode("utf-8")) < 32:
            raise RuntimeError(f"MINI_AGENT_MASTER_KEY_{version.upper()} must contain at least 32 UTF-8 bytes.")
        try:
            decoded = base64.urlsafe_b64decode(configured.encode("ascii"))
        except (UnicodeError, ValueError):
            decoded = b""
        return decoded if len(decoded) == 32 else hashlib.sha256(configured.encode("utf-8")).digest()

    def validate(self) -> None:
        """Fail fast when no active cloud master key is configured."""

        self._key(self.active_version)

    def wrap(self, user_id: str, dek: bytes) -> tuple[str, bytes, bytes]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        version = self.active_version
        nonce = secrets.token_bytes(12)
        aad = f"mini-agent-user-dek:{version}:{user_id}".encode()
        return version, nonce, AESGCM(self._key(version)).encrypt(nonce, dek, aad)

    def unwrap(self, user_id: str, version: str, nonce: bytes, wrapped: bytes) -> bytes:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        aad = f"mini-agent-user-dek:{version}:{user_id}".encode()
        try:
            key = AESGCM(self._key(version)).decrypt(nonce, wrapped, aad)
        except InvalidTag as exc:
            raise SecretDecryptionError("The cloud user data key could not be unwrapped.") from exc
        if len(key) != 32:
            raise SecretDecryptionError("The cloud user data key has an invalid length.")
        return key
