"""Shared validation and defaults for per-user agent settings.

Both the local SQLite repository and the optional PostgreSQL repository use
these values and validators. Keeping the contract here prevents the two
adapters from silently accepting different settings.
"""

from __future__ import annotations

from collections.abc import Mapping

from backend.domain import DEFAULT_TIME_ZONE, TIME_ZONE_OPTIONS, validate_time_zone

DEFAULT_PROFILE: dict[str, str] = {"display_name": "", "agent_preferences": ""}
DEFAULT_AGENT_CONFIG: dict[str, object] = {
    "tone": "balanced",
    "verbosity": "balanced",
    "initiative": "balanced",
    "custom_instructions": "",
    "display_mode": "medium",
    "timezone": DEFAULT_TIME_ZONE,
    "location_enabled": False,
}
SUPPORTED_DISPLAY_MODES = frozenset({"minimal", "medium", "verbose", "developer"})

DEFAULT_PROVIDER_CONFIG: dict[str, object] = {
    "id": "",
    "is_active": False,
    "provider_name": "deepseek",
    "protocol": "chat_completions",
    "base_url": "",
    "model": "",
    "max_tokens": 8192,
    "context_size": 1024000,
    "tokenizer_model": "deepseek-ai/DeepSeek-V3",
    "api_key_configured": False,
}
DEFAULT_CAPABILITY_CONFIG: dict[str, object] = {}
DEFAULT_RUNTIME_CONFIG: dict[str, object] = {"max_tool_calls": 32}


def normalize_runtime_config(current: Mapping[str, object], values: Mapping[str, object]) -> dict[str, object]:
    """Merge and validate execution limits stored per authenticated user."""

    raw = values.get("max_tool_calls", current.get("max_tool_calls", 32))
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("max_tool_calls must be an integer")
    max_tool_calls = raw
    if not 1 <= max_tool_calls <= 1000:
        raise ValueError("max_tool_calls must be between 1 and 1000")
    return {"max_tool_calls": max_tool_calls}


def normalize_agent_config(current: Mapping[str, object], values: Mapping[str, object]) -> dict[str, object]:
    """Merge and validate an agent-settings payload for either adapter."""

    result = dict(DEFAULT_AGENT_CONFIG)
    result.update(current)
    for key in result:
        if key not in values:
            continue
        raw = values[key]
        if key == "location_enabled":
            if not isinstance(raw, bool):
                raise ValueError("location_enabled must be a boolean")
            result[key] = raw
            continue
        value = str(raw or "").strip()
        limit = 4000 if key == "custom_instructions" else 40
        if len(value) > limit:
            raise ValueError(f"{key} exceeds {limit} characters")
        if key == "display_mode" and value not in SUPPORTED_DISPLAY_MODES:
            raise ValueError("display_mode must be minimal, medium, verbose, or developer")
        if key == "timezone":
            value = validate_time_zone(value)
        result[key] = value
    return result


def normalize_provider_config(current: Mapping[str, object], values: Mapping[str, object]) -> dict[str, object]:
    """Merge and validate provider settings without handling API-key storage."""

    protocol = str(values.get("protocol", current.get("protocol", "chat_completions")) or "").strip().lower()
    if protocol not in {"chat_completions", "responses", "messages"}:
        raise ValueError("protocol must be chat_completions, responses, or messages")
    explicit_name = values.get("provider_name")
    fallback_name = current.get("provider_name") or current.get("provider") or values.get("provider") or "deepseek"
    provider_name = str(explicit_name if explicit_name is not None else fallback_name or "deepseek").strip()
    if not provider_name:
        raise ValueError("provider_name is required")
    provider = str(
        values.get("provider_type", values.get("provider", current.get("provider", "deepseek"))) or "deepseek"
    ).strip().lower()
    base_url = str(values.get("base_url", current.get("base_url", "")) or "").strip()
    model = str(values.get("model", current.get("model", "")) or "").strip()
    tokenizer_model = str(
        values.get("tokenizer_model", current.get("tokenizer_model", "deepseek-ai/DeepSeek-V3")) or ""
    ).strip()
    if len(base_url) > 2000 or len(model) > 300 or len(tokenizer_model) > 300:
        raise ValueError("provider fields exceed their length limits")
    try:
        max_tokens = int(values.get("max_tokens", current.get("max_tokens", 8192)))
        context_size = int(values.get("context_size", current.get("context_size", 1024000)))
    except (TypeError, ValueError) as exc:
        raise ValueError("token limits must be integers") from exc
    if not 1 <= max_tokens <= 384000 or context_size <= max_tokens:
        raise ValueError("invalid token limits")
    if not base_url or not model:
        raise ValueError("base_url and model are required")
    return {
        "provider": provider,
        "provider_name": provider_name,
        "protocol": protocol,
        "base_url": base_url,
        "model": model,
        "max_tokens": max_tokens,
        "context_size": context_size,
        "tokenizer_model": tokenizer_model,
    }


def timezone_options() -> list[dict[str, str]]:
    return [{"identifier": option.identifier, "label": option.label} for option in TIME_ZONE_OPTIONS]
