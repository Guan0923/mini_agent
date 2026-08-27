"""Validation and defaults for local profile and agent settings."""

from __future__ import annotations

from collections.abc import Mapping

from backend.domain import DEFAULT_TIME_ZONE, TIME_ZONE_OPTIONS, validate_time_zone
from backend.domain.terminal import DEFAULT_TERMINAL_TYPE, normalize_terminal_type
from backend.sandbox import (
    NetworkMode,
    NetworkRule,
    PermissionMode,
    SandboxLimits,
    normalize_permission_mode,
)

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
    "provider_name": "default",
    "protocol": "chat_completions",
    "base_url": "",
    "model": "",
    "max_tokens": 8192,
    "context_size": 1024000,
    "tokenizer_model": "",
    "api_key_configured": False,
}
DEFAULT_CAPABILITY_CONFIG: dict[str, object] = {}
DEFAULT_RUNTIME_CONFIG: dict[str, object] = {"max_tool_calls": 32, "terminal_type": DEFAULT_TERMINAL_TYPE}
DEFAULT_SANDBOX_CONFIG: dict[str, object] = {
    "policy_version": 2,
    "enabled": True,
    "file_mode": PermissionMode.READ_ONLY.value,
    "network_mode": NetworkMode.NO_NETWORK.value,
    "network_allowlist": [],
    "limits": SandboxLimits().to_dict(),
}


def normalize_runtime_config(current: Mapping[str, object], values: Mapping[str, object]) -> dict[str, object]:
    """Merge and validate local execution limits."""

    raw = values.get("max_tool_calls", current.get("max_tool_calls", 32))
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("max_tool_calls must be an integer")
    max_tool_calls = raw
    if not 1 <= max_tool_calls <= 1000:
        raise ValueError("max_tool_calls must be between 1 and 1000")
    terminal_type = normalize_terminal_type(values.get("terminal_type", current.get("terminal_type")))
    return {"max_tool_calls": max_tool_calls, "terminal_type": terminal_type}


def normalize_sandbox_config(
    current: Mapping[str, object] | None = None,
    values: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate the local-only sandbox settings contract."""

    result = dict(DEFAULT_SANDBOX_CONFIG)
    if isinstance(current, Mapping):
        current_values = dict(current)
        if current_values.get("policy_version") != 2:
            raise ValueError("Unsupported sandbox policy version.")
        result.update(current_values)
    if isinstance(values, Mapping):
        result.update(values)
    enabled = result.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("sandbox enabled must be a boolean")
    if isinstance(values, Mapping) and values.get("enabled") is False:
        raise ValueError("sandbox cannot be disabled")
    file_mode = normalize_permission_mode(result.get("file_mode"))
    network_mode = str(result.get("network_mode") or NetworkMode.NO_NETWORK.value)
    try:
        network = NetworkMode(network_mode)
    except ValueError as exc:
        raise ValueError("network_mode must be no_network, restricted_network, or full_network") from exc
    raw_allowlist = result.get("network_allowlist") or []
    if not isinstance(raw_allowlist, (list, tuple)):
        raise ValueError("network_allowlist must be an array")
    if len(raw_allowlist) > 64:
        raise ValueError("network_allowlist must contain at most 64 rules")
    allowlist: list[dict[str, object]] = []
    for item in raw_allowlist:
        if not isinstance(item, Mapping):
            raise ValueError("network_allowlist entries must be objects")
        try:
            raw_port = item.get("port")
            if isinstance(raw_port, bool) or not isinstance(raw_port, int):
                raise ValueError("network port must be an integer")
            rule = NetworkRule(str(item.get("host") or ""), raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("network_allowlist entry is invalid") from exc
        allowlist.append({"host": rule.host, "port": rule.port})
    if network is NetworkMode.RESTRICTED_NETWORK and not allowlist:
        raise ValueError("restricted_network requires at least one network rule")
    if file_mode is PermissionMode.FULL_ACCESS and network is not NetworkMode.FULL_NETWORK:
        raise ValueError("full_access requires full_network")
    if (
        file_mode is PermissionMode.FULL_ACCESS
        and isinstance(values, Mapping)
        and values.get("file_mode") == PermissionMode.FULL_ACCESS.value
        and not bool(values.get("full_access_acknowledged", False))
        and not (isinstance(current, Mapping) and current.get("file_mode") == PermissionMode.FULL_ACCESS.value)
    ):
        raise ValueError("full_access requires explicit joint file and network confirmation")
    limits = SandboxLimits.from_mapping(result.get("limits") if isinstance(result.get("limits"), Mapping) else None)
    return {
        "policy_version": 2,
        "enabled": True,
        "file_mode": file_mode.value,
        "network_mode": network.value,
        "network_allowlist": allowlist,
        "limits": limits.to_dict(),
    }


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
    fallback_name = current.get("provider_name") or "default"
    provider_name = str(explicit_name if explicit_name is not None else fallback_name or "default").strip()
    if provider_name.casefold() == "deepseek":
        provider_name = "default"
    if not provider_name:
        raise ValueError("provider_name is required")
    if len(provider_name) > 80:
        raise ValueError("provider_name exceeds 80 characters")
    # ``provider`` is the runtime adapter kind; protocol is its canonical value.
    provider = protocol
    base_url = str(values.get("base_url", current.get("base_url", "")) or "").strip()
    model = str(values.get("model", current.get("model", "")) or "").strip()
    tokenizer_model = str(values.get("tokenizer_model", current.get("tokenizer_model", "")) or "").strip()
    if tokenizer_model.casefold().startswith("deepseek-ai/"):
        tokenizer_model = ""
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
