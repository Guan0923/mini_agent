"""Secret hashing and reversible provider-key encryption helpers."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path


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
