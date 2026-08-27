"""Canonical Session/Thread/Turn message-tree contract.

One persisted node is one Turn. A Turn owns every version of one interaction;
each version alternates ``user, assistant, user, assistant...``. A running
version may temporarily end in user; no legacy message-node representation is
accepted.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol, TypeAlias
from uuid import uuid4

APP_VERSION = "0.0.1"
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
    "temperature": 1.0,
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


def _normalize_cwd(value: str) -> str:
    if not isinstance(value, str):
        raise RuntimeStateValidationError("cwd must be a string.")
    if not value:
        return ""
    try:
        return os.path.normpath(str(Path(value).expanduser().resolve(strict=False)))
    except (OSError, RuntimeError) as exc:
        raise RuntimeStateValidationError("cwd must be a valid path.") from exc


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


def terminal_error_payload(category: str, message: str, *, retryable: bool) -> dict[str, Any]:
    return normalize_content(
        {
            "type": "error",
            "category": str(category),
            "message": str(message),
            "retryable": retryable,
            "status": "failed",
        }
    )[0]


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
        for message_index, raw_message in enumerate(raw_version):
            message = _mapping(raw_message, f"data[{version_index}][{message_index}]")
            expected_role = "user" if message_index % 2 == 0 else "assistant"
            if message.get("role") != expected_role:
                raise RuntimeStateValidationError(
                    f"data[{version_index}][{message_index}].role must be {expected_role}."
                )
            message["content"] = normalize_content(message.get("content"))
            if expected_role == "user" and len(message["content"]) != 1:
                raise RuntimeStateValidationError("Every user Message must contain exactly one Item.")
            if expected_role == "user":
                item = message["content"][0]
                if item.get("type") != "text" or not isinstance(item.get("text"), str):
                    raise RuntimeStateValidationError("Every user Message must contain one text Item.")
            messages.append(_json(message, "message"))
        versions.append(messages)
    return versions


@dataclass
class RuntimeState:
    """One canonical Turn node."""

    thread_id: str
    parent_thread_id: str
    session_id: str
    parent_session_id: str
    id: str = field(default_factory=new_node_id)
    parent_id: str = ""
    version: str = APP_VERSION
    first_kept_item_size: int = DEFAULT_FIRST_KEPT_ITEM_SIZE
    compaction_id: str = ""
    user: str = ""
    provider_name: str = ""
    model: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_MODEL))
    permission_mode: PermissionMode = "read_only"
    running_mode: RunningMode = "agent"
    usage: dict[str, int | None] = field(default_factory=lambda: {name: None for name in USAGE_FIELDS})
    cwd: str = ""
    timestamp: str = field(default_factory=utc_iso)
    status: NodeStatus = "running"
    current_data_idx: int = 0
    data: list[list[dict[str, Any]]] = field(default_factory=lambda: turn_payload(""))

    def __post_init__(self) -> None:
        for name in (
            "thread_id",
            "parent_thread_id",
            "session_id",
            "parent_session_id",
            "id",
            "parent_id",
            "version",
            "user",
            "provider_name",
        ):
            if not isinstance(getattr(self, name), str):
                raise RuntimeStateValidationError(f"{name} must be a string.")
        if not self.thread_id or not self.session_id or not self.id:
            raise RuntimeStateValidationError("thread_id, session_id, and id are required.")
        if self.version != APP_VERSION:
            raise RuntimeStateValidationError(f"version must be {APP_VERSION!r}.")
        if self.status not in NODE_STATUSES:
            raise RuntimeStateValidationError("status must be running, success, paused, or failed.")
        if self.permission_mode not in PERMISSION_MODES:
            raise RuntimeStateValidationError("permission_mode must be read_only, workspace_write, or full_access.")
        if self.running_mode not in RUNNING_MODES:
            raise RuntimeStateValidationError("running_mode must be agent or plan.")
        if bool(self.parent_id) != bool(self.parent_session_id):
            raise RuntimeStateValidationError("parent_id and parent_session_id must be set together.")
        if not self.parent_id and self.parent_session_id:
            raise RuntimeStateValidationError("A Turn without parent_id cannot have parent_session_id.")
        if not self.parent_id and self.thread_id == self.session_id and self.parent_thread_id:
            raise RuntimeStateValidationError("The first main-thread Turn must have empty parent fields.")
        if not self.parent_id and self.thread_id != self.session_id and not self.parent_thread_id:
            raise RuntimeStateValidationError("A root fork copy must record its source thread.")
        if isinstance(self.first_kept_item_size, bool) or not isinstance(self.first_kept_item_size, int):
            raise RuntimeStateValidationError("firstKeptItemSize must be an integer.")
        if self.first_kept_item_size < 0:
            raise RuntimeStateValidationError("firstKeptItemSize must be non-negative.")
        if not self.compaction_id:
            object.__setattr__(self, "compaction_id", self.id)
        if not isinstance(self.compaction_id, str):
            raise RuntimeStateValidationError("compactionId must be a string.")
        try:
            timestamp = datetime.fromisoformat(self.timestamp)
        except (TypeError, ValueError) as exc:
            raise RuntimeStateValidationError("timestamp must be an ISO 8601 datetime.") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            raise RuntimeStateValidationError("timestamp must be UTC and timezone-aware.")
        object.__setattr__(self, "cwd", _normalize_cwd(self.cwd))
        object.__setattr__(self, "model", _normalize_model(self.model))
        object.__setattr__(self, "usage", _normalize_usage(self.usage))
        object.__setattr__(self, "data", validate_data(self.data))
        if isinstance(self.current_data_idx, bool) or not isinstance(self.current_data_idx, int):
            raise RuntimeStateValidationError("current_data_idx must be an integer.")
        if not 0 <= self.current_data_idx < len(self.data):
            raise RuntimeStateValidationError("current_data_idx is out of range.")
        if self.status != "running" and any(version[-1]["role"] != "assistant" for version in self.data):
            raise RuntimeStateValidationError("A non-running Turn must end with an assistant Message.")
        if self.status != "running" and any(
            item.get("status") == "running"
            for version in self.data
            for message in version
            for item in message["content"]
        ):
            raise RuntimeStateValidationError("A non-running Turn cannot contain running Items.")

    @property
    def key(self) -> tuple[str, str]:
        return self.session_id, self.id

    @property
    def selected_messages(self) -> list[dict[str, Any]]:
        return _clone(self.data[self.current_data_idx])

    @property
    def user_message(self) -> dict[str, Any]:
        return _clone(self.data[self.current_data_idx][0])

    @property
    def assistant_message(self) -> dict[str, Any]:
        for message in reversed(self.data[self.current_data_idx]):
            if message["role"] == "assistant":
                return _clone(message)
        return message_payload("assistant")

    @property
    def assistant_items(self) -> list[dict[str, Any]]:
        return _clone(self.assistant_message["content"])

    @property
    def is_terminal(self) -> bool:
        return self.status != "running"

    def clone(self) -> RuntimeState:
        return RuntimeState.from_dict(self.to_dict())

    def with_assistant_items(self, items: Sequence[Mapping[str, Any]]) -> RuntimeState:
        result = self.clone()
        messages = result.data[result.current_data_idx]
        if messages[-1]["role"] != "assistant":
            messages.append(message_payload("assistant"))
        messages[-1]["content"] = normalize_content(items)
        result.data = validate_data(result.data)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "parent_thread_id": self.parent_thread_id,
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "id": self.id,
            "parent_id": self.parent_id,
            "version": self.version,
            "firstKeptItemSize": self.first_kept_item_size,
            "compactionId": self.compaction_id,
            "user": self.user,
            "provider_name": self.provider_name,
            "model": _clone(self.model),
            "permission_mode": self.permission_mode,
            "running_mode": self.running_mode,
            "usage": _clone(self.usage),
            "cwd": self.cwd,
            "timestamp": self.timestamp,
            "status": self.status,
            "current_data_idx": self.current_data_idx,
            "data": _clone(self.data),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeState:
        raw = _mapping(value, "turn")
        required = {
            "thread_id",
            "parent_thread_id",
            "session_id",
            "parent_session_id",
            "id",
            "parent_id",
            "version",
            "firstKeptItemSize",
            "compactionId",
            "user",
            "provider_name",
            "model",
            "permission_mode",
            "running_mode",
            "usage",
            "cwd",
            "timestamp",
            "status",
            "current_data_idx",
            "data",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise RuntimeStateValidationError(f"turn is missing required fields: {', '.join(missing)}")
        return cls(
            thread_id=_string(raw, "thread_id"),
            parent_thread_id=_string(raw, "parent_thread_id"),
            session_id=_string(raw, "session_id"),
            parent_session_id=_string(raw, "parent_session_id"),
            id=_string(raw, "id"),
            parent_id=_string(raw, "parent_id"),
            version=_string(raw, "version"),
            first_kept_item_size=raw["firstKeptItemSize"],
            compaction_id=_string(raw, "compactionId"),
            user=_string(raw, "user"),
            provider_name=_string(raw, "provider_name"),
            model=_mapping(raw["model"], "model"),
            permission_mode=raw["permission_mode"],
            running_mode=raw["running_mode"],
            usage=_mapping(raw["usage"], "usage"),
            cwd=_string(raw, "cwd"),
            timestamp=_string(raw, "timestamp"),
            status=raw["status"],
            current_data_idx=raw["current_data_idx"],
            data=raw["data"],
        )

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        thread_id: str,
        user_content: Any,
        parent: RuntimeState | None = None,
        id: str | None = None,
        user: str = "",
        provider_name: str = "",
        model: Mapping[str, Any] | None = None,
        permission_mode: PermissionMode = "read_only",
        running_mode: RunningMode = "agent",
        cwd: str = "",
        first_kept_item_size: int = DEFAULT_FIRST_KEPT_ITEM_SIZE,
        compaction_id: str | None = None,
        data: Any | None = None,
        status: NodeStatus = "running",
        timestamp: str | None = None,
    ) -> RuntimeState:
        turn_id = id or new_node_id()
        return cls(
            thread_id=thread_id,
            parent_thread_id=parent.thread_id if parent else "",
            session_id=session_id,
            parent_session_id=parent.session_id if parent else "",
            id=turn_id,
            parent_id=parent.id if parent else "",
            first_kept_item_size=first_kept_item_size,
            compaction_id=compaction_id or (parent.compaction_id if parent else turn_id),
            user=user,
            provider_name=provider_name,
            model=dict(model or DEFAULT_MODEL),
            permission_mode=permission_mode,
            running_mode=running_mode,
            cwd=cwd,
            timestamp=timestamp or utc_iso(),
            status=status,
            data=data if data is not None else turn_payload(user_content),
        )


class RuntimeNodeStore(Protocol):
    def create_node(self, node: RuntimeState) -> None: ...
    def update_node(self, node: RuntimeState) -> None: ...
    def get_node(self, session_id: str, node_id: str) -> RuntimeState | None: ...
    def find_node(self, node_id: str) -> RuntimeState | None: ...
    def load_nodes(self, session_id: str) -> list[RuntimeState]: ...


class InMemoryNodeStore:
    def __init__(self, nodes: Iterable[RuntimeState] = ()) -> None:
        self._nodes = {node.key: node.clone() for node in nodes}
        self._lock = RLock()

    def create_node(self, node: RuntimeState) -> None:
        with self._lock:
            if node.key in self._nodes:
                raise ValueError(f"Turn already exists: {node.id}")
            if node.parent_id:
                parent = self._nodes.get((node.parent_session_id, node.parent_id))
                if parent is None:
                    raise ValueError("Turn parent does not exist.")
                if node.parent_session_id != node.session_id:
                    raise ValueError("A Turn cannot continue across Sessions.")
                if node.parent_thread_id != parent.thread_id:
                    raise ValueError("parent_thread_id does not match the parent Turn.")
            if any(item.thread_id == node.thread_id and item.status == "running" for item in self._nodes.values()):
                raise ValueError("A thread may have only one running Turn.")
            self._nodes[node.key] = node.clone()

    def update_node(self, node: RuntimeState) -> None:
        with self._lock:
            if node.key not in self._nodes:
                raise KeyError(node.id)
            self._nodes[node.key] = node.clone()

    def get_node(self, session_id: str, node_id: str) -> RuntimeState | None:
        with self._lock:
            node = self._nodes.get((session_id, node_id))
            return node.clone() if node else None

    def find_node(self, node_id: str) -> RuntimeState | None:
        with self._lock:
            matches = [node for node in self._nodes.values() if node.id == node_id]
            if len(matches) > 1:
                raise RuntimeStateValidationError("Turn id is not globally unique.")
            return matches[0].clone() if matches else None

    def load_nodes(self, session_id: str) -> list[RuntimeState]:
        with self._lock:
            return sorted(
                (node.clone() for node in self._nodes.values() if node.session_id == session_id),
                key=lambda node: (node.timestamp, node.id),
            )


class RuntimeStateTree:
    def __init__(self, nodes: Iterable[RuntimeState] = ()) -> None:
        self._nodes = {node.key: node.clone() for node in nodes}

    def get(self, session_id: str, node_id: str) -> RuntimeState:
        try:
            return self._nodes[(session_id, node_id)].clone()
        except KeyError as exc:
            raise KeyError(f"Unknown Turn: {node_id}") from exc

    def ancestors(self, source: RuntimeState | tuple[str, str]) -> list[RuntimeState]:
        current = source.clone() if isinstance(source, RuntimeState) else self.get(*source)
        path: list[RuntimeState] = []
        seen: set[tuple[str, str]] = set()
        while True:
            if current.key in seen:
                raise RuntimeStateValidationError("Turn parent chain contains a cycle.")
            seen.add(current.key)
            path.append(current)
            if not current.parent_id:
                break
            try:
                parent = self.get(current.parent_session_id, current.parent_id)
            except KeyError as exc:
                raise RuntimeStateValidationError("Turn parent is missing.") from exc
            if current.parent_session_id != current.session_id:
                raise RuntimeStateValidationError("A Turn cannot continue across Sessions.")
            if current.parent_thread_id != parent.thread_id:
                raise RuntimeStateValidationError("parent_thread_id does not match the parent Turn.")
            current = parent
        path.reverse()
        return path

    @staticmethod
    def _items(turns: Sequence[RuntimeState]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for turn in turns:
            for message in turn.selected_messages:
                result.extend(_clone(message["content"]))
        return result

    def model_input(self, source: RuntimeState | tuple[str, str]) -> list[RuntimeState]:
        path = self.ancestors(source)
        current = path[-1]
        matches = [index for index, turn in enumerate(path) if turn.id == current.compaction_id]
        if not matches:
            raise RuntimeStateValidationError("compactionId is not an ancestor of the Turn.")
        return [turn.clone() for turn in path[matches[-1] :]]

    def compact(self, source: RuntimeState, summary: str, *, id: str | None = None) -> RuntimeState:
        path = self.ancestors(source)
        starts = [index for index, turn in enumerate(path) if turn.id == source.compaction_id]
        if not starts:
            raise RuntimeStateValidationError("compactionId is not an ancestor of the source Turn.")
        items = self._items(path[starts[-1] :])
        kept = items[-source.first_kept_item_size :] if source.first_kept_item_size else []
        data = [
            [source.user_message, message_payload("assistant", [compaction_payload(summary, kept_items=kept), *kept])]
        ]
        result = RuntimeState.create(
            session_id=source.session_id,
            thread_id=source.thread_id,
            user_content=source.user_message["content"],
            parent=source,
            id=id,
            user=source.user,
            provider_name=source.provider_name,
            model=source.model,
            permission_mode=source.permission_mode,
            running_mode=source.running_mode,
            cwd=source.cwd,
            first_kept_item_size=source.first_kept_item_size,
            data=data,
        )
        result.compaction_id = result.id
        return result

    def fork(self, source: RuntimeState, *, id: str | None = None, thread_id: str | None = None) -> RuntimeState:
        parent = self.get(source.parent_session_id, source.parent_id) if source.parent_id else None
        result = source.clone()
        result.id = id or new_node_id()
        result.thread_id = thread_id or new_thread_id()
        result.parent_thread_id = source.thread_id
        result.parent_id = parent.id if parent else ""
        result.parent_session_id = parent.session_id if parent else ""
        result.timestamp = utc_iso()
        if result.compaction_id == source.id:
            result.compaction_id = result.id
        return RuntimeState.from_dict(result.to_dict())

    def all_nodes(self, session_id: str | None = None) -> list[RuntimeState]:
        return sorted(
            (node.clone() for node in self._nodes.values() if session_id is None or node.session_id == session_id),
            key=lambda node: (node.timestamp, node.id),
        )


NodeFrameType: TypeAlias = Literal["turn.snapshot", "turn.delta"]
TurnDeltaOperation: TypeAlias = dict[str, Any]


_TURN_IDENTITY_FIELDS = frozenset(
    {"session_id", "id", "thread_id", "parent_session_id", "parent_id", "parent_thread_id"}
)
_TURN_CONFIG_FIELDS = frozenset(
    {
        "version",
        "first_kept_item_size",
        "compaction_id",
        "user",
        "provider_name",
        "model",
        "permission_mode",
        "running_mode",
        "usage",
        "cwd",
        "timestamp",
        "status",
        "current_data_idx",
    }
)


def _data_delta_operations(before: list[Any], after: list[Any]) -> list[TurnDeltaOperation] | None:
    """Describe append-only Message/Item changes, or reject a mutation."""

    if len(before) != len(after):
        return None
    operations: list[TurnDeltaOperation] = []
    for data_idx, (before_version, after_version) in enumerate(zip(before, after, strict=True)):
        if before_version == after_version:
            continue
        if not isinstance(before_version, list) or not isinstance(after_version, list):
            return None
        if len(after_version) > len(before_version) and after_version[: len(before_version)] == before_version:
            for message_idx, message in enumerate(after_version[len(before_version) :], start=len(before_version)):
                if not isinstance(message, Mapping):
                    return None
                operations.append(
                    {
                        "op": "append_message",
                        "data_idx": data_idx,
                        "message_idx": message_idx,
                        "message": _clone(message),
                    }
                )
            continue
        if len(before_version) != len(after_version):
            return None
        changed_messages = [index for index, message in enumerate(before_version) if message != after_version[index]]
        if len(changed_messages) != 1:
            return None
        message_idx = changed_messages[0]
        if not isinstance(before_version[message_idx], Mapping) or not isinstance(after_version[message_idx], Mapping):
            return None
        before_assistant = dict(before_version[message_idx])
        after_assistant = dict(after_version[message_idx])
        if before_assistant.get("role") != "assistant" or after_assistant.get("role") != "assistant":
            return None
        before_items = before_assistant.pop("content", None)
        after_items = after_assistant.pop("content", None)
        if (
            before_assistant != after_assistant
            or not isinstance(before_items, list)
            or not isinstance(after_items, list)
        ):
            return None
        if after_items[: len(before_items)] == before_items:
            operations.extend(
                {
                    "op": "append_item",
                    "data_idx": data_idx,
                    "message_idx": message_idx,
                    "item_idx": item_idx,
                    "item": _clone(item),
                }
                for item_idx, item in enumerate(after_items[len(before_items) :], start=len(before_items))
            )
            continue
        if len(before_items) != len(after_items):
            return None
        changed = [index for index, item in enumerate(before_items) if item != after_items[index]]
        if len(changed) != 1:
            return None
        item_idx = changed[0]
        before_item, after_item = before_items[item_idx], after_items[item_idx]
        if not isinstance(before_item, Mapping) or not isinstance(after_item, Mapping):
            return None
        before_fields, after_fields = dict(before_item), dict(after_item)
        before_status = before_fields.pop("status", None)
        after_status = after_fields.pop("status", None)
        if before_fields == after_fields and before_status != after_status and after_status in ITEM_STATUSES:
            operations.append(
                {
                    "op": "set_item_status",
                    "data_idx": data_idx,
                    "message_idx": message_idx,
                    "item_idx": item_idx,
                    "status": after_status,
                }
            )
            continue
        before_text = before_fields.pop("text", None)
        after_text = after_fields.pop("text", None)
        if (
            before_fields != after_fields
            or before_fields.get("type") not in {"text", "reasoning"}
            or not isinstance(before_text, str)
            or not isinstance(after_text, str)
            or not after_text.startswith(before_text)
        ):
            return None
        operations.append(
            {
                "op": "append_text",
                "data_idx": data_idx,
                "message_idx": message_idx,
                "item_idx": item_idx,
                "delta": after_text[len(before_text) :],
            }
        )
    return operations


@dataclass(frozen=True)
class NodeFrame:
    type: NodeFrameType
    session_id: str
    turn_id: str
    revision: int
    turn: RuntimeState | None = None
    patch: dict[str, Any] = field(default_factory=dict)
    operations: tuple[TurnDeltaOperation, ...] = ()

    @classmethod
    def snapshot(cls, node: RuntimeState) -> NodeFrame:
        return cls("turn.snapshot", node.session_id, node.id, 0, turn=node.clone())

    @classmethod
    def delta(cls, before: RuntimeState, after: RuntimeState, *, revision: int) -> NodeFrame | None:
        before_payload, after_payload = before.to_dict(), after.to_dict()
        if any(before_payload[name] != after_payload[name] for name in _TURN_IDENTITY_FIELDS):
            raise RuntimeStateValidationError("Turn identity cannot change in a delta.")
        patch = {
            name: _clone(value)
            for name, value in after_payload.items()
            if name not in {*_TURN_IDENTITY_FIELDS, "data"} and before_payload.get(name) != value
        }
        operations = _data_delta_operations(before_payload["data"], after_payload["data"])
        if operations is None:
            raise RuntimeStateValidationError("Turn streaming mutations must be append-only.")
        if not patch and not operations:
            return None
        return cls(
            "turn.delta",
            after.session_id,
            after.id,
            revision,
            patch=patch,
            operations=tuple(operations),
        )

    def to_dict(self) -> dict[str, Any]:
        if self.type == "turn.snapshot":
            if self.turn is None:
                raise RuntimeStateValidationError("A Turn snapshot requires a complete Turn.")
            return {"type": self.type, "revision": self.revision, "turn": self.turn.to_dict()}
        payload: dict[str, Any] = {
            "type": self.type,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "revision": self.revision,
        }
        if self.patch:
            payload["patch"] = _clone(self.patch)
        if self.operations:
            payload["operations"] = _clone(list(self.operations))
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    def as_sse(self) -> str:
        return f"data: {self.to_json()}\n\n"


class NodeWriter:
    """Persist complete Turns while emitting one baseline and incremental updates."""

    def __init__(
        self,
        store: RuntimeNodeStore,
        *,
        emit: Callable[[NodeFrame], None] | None = None,
        id_factory: Callable[[], str] = new_node_id,
    ) -> None:
        self.store = store
        self.emit = emit or (lambda _frame: None)
        self._emits_frames = emit is not None
        self.id_factory = id_factory
        self._dynamic: dict[tuple[str, str], RuntimeState] = {}
        self._revisions: dict[tuple[str, str], int] = {}
        self._lock = RLock()

    def _emit_snapshot(self, node: RuntimeState) -> None:
        if not self._emits_frames:
            return
        self._revisions[node.key] = 0
        self.emit(NodeFrame.snapshot(node))

    def _emit_delta(
        self,
        node: RuntimeState,
        *,
        patch: Mapping[str, Any] | None = None,
        operations: Sequence[TurnDeltaOperation] = (),
    ) -> None:
        if not self._emits_frames or (not patch and not operations):
            return
        previous = self._revisions.get(node.key)
        if previous is None:
            raise RuntimeStateValidationError("A Turn delta requires a baseline snapshot.")
        revision = previous + 1
        self.emit(
            NodeFrame(
                "turn.delta",
                node.session_id,
                node.id,
                revision,
                patch={str(key): _clone(value) for key, value in (patch or {}).items()},
                operations=tuple(_clone(list(operations))),
            )
        )
        self._revisions[node.key] = revision

    def _store_dynamic(self, node: RuntimeState, *, persist: bool) -> RuntimeState:
        value = RuntimeState.from_dict(node.to_dict())
        self._dynamic[value.key] = value.clone()
        if persist:
            self.store.update_node(value)
        return value

    def create(self, node: RuntimeState | None = None, **kwargs: Any) -> RuntimeState:
        with self._lock:
            if node is None:
                kwargs.setdefault("id", self.id_factory())
                node = RuntimeState.create(**kwargs)
            self.store.create_node(node)
            self._dynamic[node.key] = node.clone()
            self._emit_snapshot(node)
            return node.clone()

    def snapshot(self, node: RuntimeState) -> RuntimeState:
        """Seed an existing Turn as this stream's baseline."""

        with self._lock:
            value = RuntimeState.from_dict(node.to_dict())
            self._dynamic[value.key] = value.clone()
            self._emit_snapshot(value)
            return value.clone()

    def current(self, session_id: str, node_id: str) -> RuntimeState:
        with self._lock:
            value = self._dynamic.get((session_id, node_id)) or self.store.get_node(session_id, node_id)
            if value is None:
                raise KeyError(node_id)
            return value.clone()

    def update(self, node: RuntimeState, *, persist: bool = False) -> RuntimeState:
        with self._lock:
            previous = self.current(node.session_id, node.id)
            value = RuntimeState.from_dict(node.to_dict())
            previous_revision = self._revisions.get(value.key)
            if self._emits_frames and previous_revision is None:
                raise RuntimeStateValidationError("A Turn delta requires a baseline snapshot.")
            revision = (previous_revision or 0) + 1
            frame = NodeFrame.delta(previous, value, revision=revision) if self._emits_frames else None
            value = self._store_dynamic(value, persist=persist)
            if frame is not None:
                self.emit(frame)
                self._revisions[value.key] = revision
            return value.clone()

    def update_data(self, node: RuntimeState, data: Any, *, persist: bool = False) -> RuntimeState:
        current = self.current(node.session_id, node.id)
        current.data = validate_data(data)
        return self.update(current, persist=persist)

    def update_config(self, node: RuntimeState, **changes: Any) -> RuntimeState:
        with self._lock:
            current = self.current(node.session_id, node.id)
            before = current.to_dict()
            for name, value in changes.items():
                if name == "firstKeptItemSize":
                    name = "first_kept_item_size"
                elif name == "compactionId":
                    name = "compaction_id"
                if name not in _TURN_CONFIG_FIELDS:
                    raise RuntimeStateValidationError(f"Unsupported Turn field: {name}")
                setattr(current, name, _clone(value))
            value = self._store_dynamic(current, persist=True)
            after = value.to_dict()
            patch = {
                name: _clone(item)
                for name, item in after.items()
                if name not in {*_TURN_IDENTITY_FIELDS, "data"} and before.get(name) != item
            }
            self._emit_delta(value, patch=patch)
            return value.clone()

    def append_item(self, node: RuntimeState, item: Mapping[str, Any], *, persist: bool = True) -> RuntimeState:
        return self.append_items(node, [item], persist=persist)

    def append_message(
        self,
        node: RuntimeState,
        message: Mapping[str, Any],
        *,
        persist: bool = True,
    ) -> RuntimeState:
        with self._lock:
            current = self.current(node.session_id, node.id)
            messages = current.data[current.current_data_idx]
            message_idx = len(messages)
            messages.append(_json(message, "Message"))
            current.data = validate_data(current.data)
            value = self._store_dynamic(current, persist=persist)
            self._emit_delta(
                value,
                operations=(
                    {
                        "op": "append_message",
                        "data_idx": current.current_data_idx,
                        "message_idx": message_idx,
                        "message": _clone(messages[message_idx]),
                    },
                ),
            )
            return value.clone()

    def append_items(
        self,
        node: RuntimeState,
        items: Sequence[Mapping[str, Any]],
        *,
        message_idx: int | None = None,
        persist: bool = True,
    ) -> RuntimeState:
        with self._lock:
            current = self.current(node.session_id, node.id)
            messages = current.data[current.current_data_idx]
            target_idx = len(messages) - 1 if message_idx is None else message_idx
            try:
                target = messages[target_idx]
            except IndexError as exc:
                raise RuntimeStateValidationError("Turn Item target Message is out of range.") from exc
            if target.get("role") != "assistant":
                raise RuntimeStateValidationError("Turn Items can only be appended to an assistant Message.")
            content = target["content"]
            operations: list[TurnDeltaOperation] = []
            for item in items:
                normalized = normalize_content([item])[0]
                operations.append(
                    {
                        "op": "append_item",
                        "data_idx": current.current_data_idx,
                        "message_idx": target_idx,
                        "item_idx": len(content),
                        "item": _clone(normalized),
                    }
                )
                content.append(normalized)
            current.data = validate_data(current.data)
            value = self._store_dynamic(current, persist=persist)
            self._emit_delta(value, operations=operations)
            return value.clone()

    def set_item_status(
        self,
        node: RuntimeState,
        *,
        data_idx: int,
        message_idx: int,
        item_idx: int,
        status: ItemStatus,
        persist: bool = True,
    ) -> RuntimeState:
        if status not in ITEM_STATUSES:
            raise RuntimeStateValidationError("Item status must be running, failed, or success.")
        with self._lock:
            current = self.current(node.session_id, node.id)
            try:
                item = current.data[data_idx][message_idx]["content"][item_idx]
            except (IndexError, KeyError) as exc:
                raise RuntimeStateValidationError("Turn Item status target is out of range.") from exc
            item["status"] = status
            current.data = validate_data(current.data)
            value = self._store_dynamic(current, persist=persist)
            self._emit_delta(
                value,
                operations=(
                    {
                        "op": "set_item_status",
                        "data_idx": data_idx,
                        "message_idx": message_idx,
                        "item_idx": item_idx,
                        "status": status,
                    },
                ),
            )
            return value.clone()

    def append_text(
        self,
        node: RuntimeState,
        *,
        data_idx: int,
        message_idx: int | None = None,
        item_idx: int,
        delta: str,
        persist: bool = False,
    ) -> RuntimeState:
        if not delta:
            return self.current(node.session_id, node.id)
        with self._lock:
            current = self.current(node.session_id, node.id)
            try:
                messages = current.data[data_idx]
                target_idx = len(messages) - 1 if message_idx is None else message_idx
                if messages[target_idx].get("role") != "assistant":
                    raise RuntimeStateValidationError("Turn text delta must target an assistant Message.")
                item = messages[target_idx]["content"][item_idx]
            except IndexError as exc:
                raise RuntimeStateValidationError("Turn text delta target is out of range.") from exc
            if item.get("type") not in {"text", "reasoning"} or not isinstance(item.get("text"), str):
                raise RuntimeStateValidationError("Turn text delta must target text or reasoning.")
            item["text"] += delta
            value = self._store_dynamic(current, persist=persist)
            self._emit_delta(
                value,
                operations=(
                    {
                        "op": "append_text",
                        "data_idx": data_idx,
                        "message_idx": target_idx,
                        "item_idx": item_idx,
                        "delta": delta,
                    },
                ),
            )
            return value.clone()

    def persist(self, node: RuntimeState) -> RuntimeState:
        """Persist the current dynamic Turn without publishing another delta."""

        with self._lock:
            value = self._store_dynamic(node, persist=True)
            return value.clone()

    def finalize(self, node: RuntimeState, status: NodeStatus) -> RuntimeState:
        if status == "running":
            raise RuntimeStateValidationError("A finalized Turn cannot remain running.")
        with self._lock:
            current = self.current(node.session_id, node.id)
            current.status = status
            result = self._store_dynamic(current, persist=True)
            self._emit_delta(result, patch={"status": status})
            self._dynamic.pop(result.key, None)
            return result.clone()

    def fail(self, session_id: str, node_id: str, message: str = "Execution failed.") -> RuntimeState:
        node = self.current(session_id, node_id)
        node = self.append_item(node, terminal_error_payload("agent", message, retryable=False))
        return self.finalize(node, "failed")


def recoverable(node: RuntimeState) -> bool:
    return node.status == "paused"


__all__ = [
    "APP_VERSION",
    "CONTENT_BLOCK_TYPES",
    "DEFAULT_COMPACTION_RETENTION",
    "FAILED_TERMINAL_MESSAGE",
    "DEFAULT_FIRST_KEPT_ITEM_SIZE",
    "DEFAULT_MODEL",
    "MESSAGE_ROLES",
    "ITEM_STATUSES",
    "NODE_STATUSES",
    "USAGE_FIELDS",
    "ContentBlockType",
    "InMemoryNodeStore",
    "ItemStatus",
    "MessageRole",
    "NodeFrame",
    "NodeFrameType",
    "NodeStatus",
    "NodeWriter",
    "PermissionMode",
    "ReasoningEffort",
    "RunningMode",
    "RuntimeNodeStore",
    "RuntimeState",
    "RuntimeStateTree",
    "RuntimeStateValidationError",
    "TerminalErrorCategory",
    "ThinkingMode",
    "TurnDeltaOperation",
    "compaction_payload",
    "message_payload",
    "new_node_id",
    "new_session_id",
    "new_thread_id",
    "normalize_content",
    "recoverable",
    "terminal_error_payload",
    "terminal_error_text",
    "turn_payload",
    "utc_iso",
    "validate_data",
]
