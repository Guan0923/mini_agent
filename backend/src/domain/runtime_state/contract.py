"""Runtime Turn payload types, constants, normalization, and validation.

One persisted node is one Turn. A Turn owns every version of one interaction.
Each version starts with one user Message, rejects consecutive user Messages,
and may contain consecutive assistant Messages for durable Agent reports. A
running version may temporarily end in user; no legacy message-node
representation is accepted.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any, Literal, TypeAlias
from uuid import uuid4

APP_VERSION = "0.0.2"
DEFAULT_FIRST_KEPT_ITEM_SIZE = 8
DEFAULT_COMPACTION_RETENTION = DEFAULT_FIRST_KEPT_ITEM_SIZE
FAILED_TERMINAL_MESSAGE = "An unknown error caused the system to encounter an exception."

NodeStatus: TypeAlias = Literal["running", "success", "paused", "failed"]
ItemStatus: TypeAlias = Literal["running", "failed", "success"]
PermissionMode: TypeAlias = Literal["read_only", "workspace_write", "full_access"]
RunningMode: TypeAlias = Literal["agent", "plan"]
ReasoningEffort: TypeAlias = Literal["low", "medium", "high", "xhigh", "max"]
ThinkingMode: TypeAlias = Literal["enable", "disable"]
MessageRole: TypeAlias = Literal["user", "assistant"]
ContentBlockType: TypeAlias = Literal[
    "text",
    "reasoning",
    "tool_call",
    "tool_result",
    "bash",
    "plan",
    "approval",
    "question",
    "subagent",
    "skill_snapshot",
    "compaction",
    "retry",
    "error",
]
TerminalErrorCategory: TypeAlias = Literal["user", "network", "tool", "provider", "server", "billing", "agent"]

NODE_STATUSES = frozenset({"running", "success", "paused", "failed"})
ITEM_STATUSES = frozenset({"running", "failed", "success"})
PERMISSION_MODES = frozenset({"read_only", "workspace_write", "full_access"})
RUNNING_MODES = frozenset({"agent", "plan"})
REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
THINKING_MODES = frozenset({"enable", "disable"})
MESSAGE_ROLES = frozenset({"user", "assistant"})
CONTENT_BLOCK_TYPES = frozenset(
    {
        "text",
        "reasoning",
        "tool_call",
        "tool_result",
        "bash",
        "plan",
        "approval",
        "question",
        "subagent",
        "skill_snapshot",
        "compaction",
        "retry",
        "error",
    }
)
USAGE_FIELDS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
DEFAULT_MODEL: dict[str, Any] = {
    "reasoning_effort": "medium",
    "current_model": "unknown",
    "context_length": 128000,
    "output_length": 8192,
    "thinking": "enable",
    "temperature": 0.0,
}


class RuntimeStateValidationError(ValueError):
    """Raised when a Turn or one of its selected messages is invalid."""


def utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_node_id() -> str:
    return f"turn_{uuid4().hex}"


def new_thread_id() -> str:
    return f"thread_{uuid4().hex}"


def new_session_id() -> str:
    return f"session_{uuid4().hex}"


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _json(value: Any, name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeStateValidationError(f"{name} must contain JSON values only.") from exc
    return _clone(value)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeStateValidationError(f"{name} must be a JSON object.")
    return {str(key): _clone(item) for key, item in value.items()}


def _string(raw: Mapping[str, Any], name: str, default: str = "") -> str:
    value = raw.get(name, default)
    if not isinstance(value, str):
        raise RuntimeStateValidationError(f"{name} must be a string.")
    return value


def _normalize_cwd(value: str, *, name: str = "cwd") -> str:
    if not isinstance(value, str):
        raise RuntimeStateValidationError(f"{name} must be a string.")
    if not value:
        return ""
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise RuntimeStateValidationError(f"{name} must be an absolute path.")
        return os.path.normpath(str(candidate.resolve(strict=False)))
    except (OSError, RuntimeError) as exc:
        raise RuntimeStateValidationError(f"{name} must be a valid path.") from exc


def _normalize_model(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(DEFAULT_MODEL)
    if value is not None:
        supplied = _mapping(value, "model")
        unknown = set(supplied) - set(DEFAULT_MODEL)
        if unknown:
            raise RuntimeStateValidationError(f"Unsupported model fields: {', '.join(sorted(unknown))}.")
        result.update(supplied)
    if result["reasoning_effort"] not in REASONING_EFFORTS:
        raise RuntimeStateValidationError("model.reasoning_effort is invalid.")
    if result["thinking"] not in THINKING_MODES:
        raise RuntimeStateValidationError("model.thinking must be enable or disable.")
    if not isinstance(result["current_model"], str) or not result["current_model"]:
        raise RuntimeStateValidationError("model.current_model must be a non-empty string.")
    for name in ("context_length", "output_length"):
        number = result[name]
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise RuntimeStateValidationError(f"model.{name} must be a positive integer.")
    temperature = result["temperature"]
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise RuntimeStateValidationError("model.temperature must be numeric.")
    return _json(result, "model")


def _normalize_usage(value: Mapping[str, Any] | None) -> dict[str, int | None]:
    raw = {name: None for name in USAGE_FIELDS}
    if value is not None:
        supplied = _mapping(value, "usage")
        if set(supplied) - set(USAGE_FIELDS):
            raise RuntimeStateValidationError("usage contains unsupported fields.")
        raw.update(supplied)
    for name, item in raw.items():
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            raise RuntimeStateValidationError(f"usage.{name} must be a non-negative integer or null.")
    return {name: raw[name] for name in USAGE_FIELDS}


def normalize_content(content: str | Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        content = [{"type": "text", "text": content, "status": "success"}]
    elif isinstance(content, Mapping):
        content = [content]
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        raise RuntimeStateValidationError("message.content must be an Item array.")
    result: list[dict[str, Any]] = []
    for index, value in enumerate(content):
        item = _mapping(value, f"message.content[{index}]")
        kind = item.get("type")
        if kind not in CONTENT_BLOCK_TYPES:
            raise RuntimeStateValidationError(f"Unsupported Item type: {kind!r}.")
        status = item.get("status")
        if status not in ITEM_STATUSES:
            raise RuntimeStateValidationError(f"{kind} Item status must be running, failed, or success.")
        if kind in {"text", "reasoning", "bash"} and not isinstance(item.get("text"), str):
            raise RuntimeStateValidationError(f"{kind} Item requires string text.")
        if kind == "tool_call":
            if not isinstance(item.get("call_id"), str) or not item["call_id"]:
                raise RuntimeStateValidationError("tool_call requires call_id.")
            if not isinstance(item.get("name"), str) or not item["name"]:
                raise RuntimeStateValidationError("tool_call requires name.")
            if not isinstance(item.get("arguments", {}), Mapping):
                raise RuntimeStateValidationError("tool_call.arguments must be an object.")
            if not isinstance(item.get("replay_safe", True), bool):
                raise RuntimeStateValidationError("tool_call.replay_safe must be boolean.")
        if kind == "tool_result":
            if not isinstance(item.get("call_id"), str) or not item["call_id"]:
                raise RuntimeStateValidationError("tool_result requires call_id.")
        if kind == "compaction":
            if not isinstance(item.get("summary"), str):
                raise RuntimeStateValidationError("compaction.summary must be a string.")
            count = item.get("kept_item_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise RuntimeStateValidationError("compaction.kept_item_count must be non-negative.")
        if kind == "retry":
            if item.get("event") != "model_retry":
                raise RuntimeStateValidationError("retry.event must be model_retry.")
            if item.get("category") != "network":
                raise RuntimeStateValidationError("retry.category must be network.")
            if not isinstance(item.get("message"), str) or not item["message"]:
                raise RuntimeStateValidationError("retry.message must be a non-empty string.")
            attempt = item.get("attempt")
            max_retries = item.get("max_retries")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise RuntimeStateValidationError("retry.attempt must be a positive integer.")
            if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < attempt:
                raise RuntimeStateValidationError("retry.max_retries must include the current attempt.")
            delay_seconds = item.get("delay_seconds")
            if (
                isinstance(delay_seconds, bool)
                or not isinstance(delay_seconds, (int, float))
                or not isfinite(delay_seconds)
                or delay_seconds < 0
            ):
                raise RuntimeStateValidationError("retry.delay_seconds must be finite and non-negative.")
        if kind == "error":
            if not isinstance(item.get("category"), str) or not item["category"]:
                raise RuntimeStateValidationError("error.category must be a non-empty string.")
            if not isinstance(item.get("message"), str) or not item["message"]:
                raise RuntimeStateValidationError("error.message must be a non-empty string.")
            if not isinstance(item.get("retryable"), bool):
                raise RuntimeStateValidationError("error.retryable must be boolean.")
        result.append(_json(item, f"message.content[{index}]"))
    return result


def message_payload(role: MessageRole, content: Any = None, **metadata: Any) -> dict[str, Any]:
    if role not in MESSAGE_ROLES:
        raise RuntimeStateValidationError("message.role must be user or assistant.")
    if role == "user" and isinstance(content, Mapping):
        content = [{**content, "status": "success"}]
    elif role == "user" and isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        content = [{**item, "status": "success"} for item in content]
    message: dict[str, Any] = {"role": role, "content": normalize_content(content)}
    if metadata:
        message.update(_json(metadata, "message metadata"))
    return message


def turn_payload(user: Any, assistant: Any = None, **user_metadata: Any) -> list[list[dict[str, Any]]]:
    return [[message_payload("user", user, **user_metadata), message_payload("assistant", assistant)]]


def compaction_payload(summary: str, *, kept_items: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    if not isinstance(summary, str):
        raise RuntimeStateValidationError("compaction summary must be a string.")
    return {
        "type": "compaction",
        "summary": summary,
        "kept_item_count": len(kept_items),
        "status": "success",
    }


def terminal_error_payload(
    category: str,
    message: str,
    *,
    retryable: bool,
    code: str = "",
) -> dict[str, Any]:
    payload = {
        "type": "error",
        "category": str(category),
        "message": str(message),
        "retryable": retryable,
        "status": "failed",
    }
    if code:
        payload["code"] = str(code)
    return normalize_content(payload)[0]


def terminal_error_text(error: Mapping[str, Any]) -> str:
    return str(error.get("message") or "Execution failed.")


def validate_data(value: Any) -> list[list[dict[str, Any]]]:
    if not isinstance(value, list) or not value:
        raise RuntimeStateValidationError("data must be a non-empty Message[][] array.")
    versions: list[list[dict[str, Any]]] = []
    for version_index, raw_version in enumerate(value):
        if not isinstance(raw_version, list) or not raw_version:
            raise RuntimeStateValidationError(f"data[{version_index}] must contain at least one Message.")
        messages: list[dict[str, Any]] = []
        previous_role: str | None = None
        for message_index, raw_message in enumerate(raw_version):
            message = _mapping(raw_message, f"data[{version_index}][{message_index}]")
            role = message.get("role")
            if message_index == 0 and role != "user":
                raise RuntimeStateValidationError(f"data[{version_index}][{message_index}].role must be user.")
            if role not in MESSAGE_ROLES:
                raise RuntimeStateValidationError(f"data[{version_index}][{message_index}].role is invalid.")
            if role == "user" and previous_role == "user":
                raise RuntimeStateValidationError("Consecutive user Messages are not allowed.")
            message["content"] = normalize_content(message.get("content"))
            if role == "user" and len(message["content"]) != 1:
                raise RuntimeStateValidationError("Every user Message must contain exactly one Item.")
            if role == "user":
                item = message["content"][0]
                if item.get("type") != "text" or not isinstance(item.get("text"), str):
                    raise RuntimeStateValidationError("Every user Message must contain one text Item.")
            messages.append(_json(message, "message"))
            previous_role = str(role)
        versions.append(messages)
    return versions
