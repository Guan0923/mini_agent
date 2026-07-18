"""Normalize runtime events into durable, secret-safe diagnostic records."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from mini_agent.domain import ToolSpec, message_to_dict

from ..core.context import PreparedResponse, RuntimeExchange, RuntimeState
from ..core.events import RuntimeEvent

_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|cookie|password|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(r"(?i)\b(api[_-]?key|authorization|cookie|password|secret|token)\b\s*([=:])\s*([^\s,;]+)")
_IDENTIFIER_KEYS = frozenset(
    {
        "attempt",
        "call_id",
        "exchange_id",
        "error_type",
        "finish_reason",
        "hook",
        "kind",
        "mode",
        "lifecycle",
        "model",
        "name",
        "operation",
        "output_mode",
        "planner",
        "provider",
        "phase",
        "response_id",
        "response_model",
        "role",
        "run_id",
        "session_id",
        "source",
        "status",
        "strategy",
        "stream",
        "tool",
    }
)
_PREVIEW_CHARS = 200


def model_request_data(state: RuntimeState, exchange: RuntimeExchange) -> dict[str, Any]:
    """Return provider-neutral data needed to replay a prepared model request."""

    parameters = dict(state.request_parameters)
    overrides = exchange.context.get("request_parameters")
    if isinstance(overrides, Mapping):
        parameters.update(overrides)
    return {
        "exchange_id": exchange.exchange_id,
        "operation": exchange.operation,
        "provider": state.provider,
        "model": state.model,
        "output_mode": exchange.output_mode,
        "stream": exchange.stream,
        "request_parameters": parameters,
        "messages": [_message_to_record(message) for message in exchange.messages],
        "tools": [_tool_spec_to_dict(tool) for tool in exchange.allowed_tools],
    }


def model_response_data(state: RuntimeState, exchange: RuntimeExchange, response: PreparedResponse) -> dict[str, Any]:
    """Return the normalized response instead of provider-specific wire data."""

    return {
        "exchange_id": exchange.exchange_id,
        "provider": state.provider,
        "model": state.model,
        "response_id": response.response_id,
        "response_model": response.model,
        "finish_reason": response.finish_reason,
        "usage": response.usage,
        "message": _message_to_record(response.message),
    }


def model_error_data(state: RuntimeState, exchange: RuntimeExchange, error: Exception) -> dict[str, Any]:
    """Capture safe request diagnostics without retaining provider wire payloads."""

    diagnostics = getattr(error, "diagnostics", None)
    return {
        "exchange_id": exchange.exchange_id,
        "provider": state.provider,
        "model": state.model,
        "operation": exchange.operation,
        "error_type": error.__class__.__name__,
        "error": str(error),
        "diagnostics": dict(diagnostics) if isinstance(diagnostics, dict) else {},
    }


def persistent_event(event: RuntimeEvent, include_full_messages: bool) -> tuple[str, dict[str, Any]]:
    """Create the data representation shared by checkpoints, SQLite, and JSONL."""

    message = _redact_text(event.message)
    if not include_full_messages and message:
        message = _summary_label(message)
    return message, _persistent_value(event.data, include_full_messages)


def _tool_spec_to_dict(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _message_to_record(message: Any) -> dict[str, Any]:
    """Serialize the neutral message contract without provider wire extensions."""

    payload = message_to_dict(message)
    payload.pop("provider_options", None)
    tools = payload.get("tool_messages")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                tool.pop("provider_options", None)
    return payload


def _persistent_value(value: Any, include_full_messages: bool, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _persistent_value(item_value, include_full_messages, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_persistent_value(item, include_full_messages) for item in value]
    if isinstance(value, tuple):
        return [_persistent_value(item, include_full_messages) for item in value]
    if isinstance(value, str):
        redacted = _redact_text(value)
        if include_full_messages or key in _IDENTIFIER_KEYS:
            return redacted
        return _text_summary(redacted)
    return value


def _redact_text(value: str) -> str:
    return _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)


def _summary_label(value: str) -> str:
    return f"{len(value)} chars; sha256={hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _text_summary(value: str) -> dict[str, Any]:
    return {
        "preview": value[:_PREVIEW_CHARS],
        "chars": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }
