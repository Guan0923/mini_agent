"""End-to-end encrypted JSON event envelopes used by push/pull sync."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Mapping, Sequence
from typing import Any


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encrypt_event_batch(events: Sequence[Mapping[str, object]], key: bytes, *, aad: str) -> dict[str, object]:
    """Encrypt an ordered JSON event batch while retaining verifiable metadata."""

    if len(key) != 32:
        raise ValueError("A sync data key must contain exactly 32 bytes.")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    payload = [dict(event) for event in events]
    plaintext = _canonical(payload)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad.encode("utf-8"))
    return {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        "checksum": hashlib.sha256(plaintext).hexdigest(),
        "event_count": len(events),
    }


def decrypt_event_batch(envelope: Mapping[str, object], key: bytes, *, aad: str) -> list[dict[str, object]]:
    """Authenticate, decrypt, and validate one event envelope."""

    if len(key) != 32:
        raise ValueError("A sync data key must contain exactly 32 bytes.")
    if envelope.get("algorithm") != "AES-256-GCM":
        raise ValueError("Unsupported sync event encryption algorithm.")
    try:
        nonce = base64.urlsafe_b64decode(str(envelope["nonce"]).encode("ascii"))
        ciphertext = base64.urlsafe_b64decode(str(envelope["ciphertext"]).encode("ascii"))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Invalid encrypted sync event envelope.") from exc
    if len(nonce) != 12:
        raise ValueError("Invalid encrypted sync event nonce.")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad.encode("utf-8"))
    except Exception as exc:
        raise ValueError("Sync event decryption failed.") from exc
    checksum = hashlib.sha256(plaintext).hexdigest()
    if checksum != str(envelope.get("checksum") or ""):
        raise ValueError("Sync event checksum mismatch.")
    try:
        decoded = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Sync event plaintext is not valid JSON.") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise ValueError("Sync event plaintext must be a list of objects.")
    expected_count = envelope.get("event_count")
    if expected_count is not None and int(expected_count) != len(decoded):
        raise ValueError("Sync event count does not match its envelope.")
    return [dict(item) for item in decoded]


__all__ = ["decrypt_event_batch", "encrypt_event_batch"]
