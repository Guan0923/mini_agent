"""Validate and convert structured model output into domain values."""

from __future__ import annotations

import json
from typing import Any

from backend.domain import ModelOutputError


def parse_json_object(raw: str, operation: str | None = None) -> dict[str, Any]:
    """Parse a JSON object, accepting one surrounding Markdown code fence."""

    normalized = raw.lstrip("\ufeff").strip()
    lines = normalized.splitlines()
    if len(lines) >= 2 and lines[0].strip().lower() in {"```", "```json"} and lines[-1].strip() == "```":
        normalized = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ModelOutputError("Model did not return valid JSON.", operation=operation, invalid_output=raw) from exc
    if not isinstance(payload, dict):
        raise ModelOutputError("Model JSON must be an object.", operation=operation, invalid_output=raw)
    return payload
