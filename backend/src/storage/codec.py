"""Backend-neutral codecs for persisted runtime and conversation values."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from backend.domain import DEFAULT_SESSION_TITLE, RunStatus
from backend.runtime.core.context import RuntimeState

WEB_DEFAULT_SESSION_TITLE = "新对话"


def normalize_session_title(title: str | None) -> str:
    """Return the bounded title shared by every session adapter."""

    return " ".join((title or "").split())[:80] or DEFAULT_SESSION_TITLE


def is_default_session_title(title: str) -> bool:
    """Return whether a persisted title is an uncustomized session placeholder."""

    return title in {DEFAULT_SESSION_TITLE, WEB_DEFAULT_SESSION_TITLE}


def assistant_content(status: RunStatus, answer: str | None) -> str:
    """Build the durable assistant projection for a terminal run status."""

    if status == "completed":
        return answer or ""
    if status == "cancelled":
        return "Task cancelled by user."
    return answer or f"Task {status}."


def encode_runtime_state(state: RuntimeState) -> str:
    """Serialize one resumable runtime without duplicated audit messages."""

    return json.dumps(state.to_dict(), ensure_ascii=False)


def decode_runtime_state(payload: str | bytes | bytearray) -> RuntimeState:
    """Deserialize one stored runtime state."""

    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Persisted runtime state must be a JSON object.")
    return RuntimeState.from_dict(value)


def encode_message_data(data: Mapping[str, Any]) -> str:
    """Serialize runtime-message metadata consistently across adapters."""

    return json.dumps(dict(data), ensure_ascii=False, default=str)


def decode_message_data(payload: str | bytes | bytearray) -> dict[str, Any]:
    """Return object metadata and reject non-object persisted JSON."""

    value = json.loads(payload)
    return dict(value) if isinstance(value, dict) else {}
