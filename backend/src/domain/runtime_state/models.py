"""Canonical root and Turn node models."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeAlias

from .contract import (
    APP_VERSION,
    DEFAULT_FIRST_KEPT_ITEM_SIZE,
    DEFAULT_MODEL,
    NODE_STATUSES,
    PERMISSION_MODES,
    RUNNING_MODES,
    USAGE_FIELDS,
    NodeStatus,
    PermissionMode,
    RunningMode,
    RuntimeStateValidationError,
    _clone,
    _mapping,
    _normalize_cwd,
    _normalize_model,
    _normalize_usage,
    _string,
    message_payload,
    new_node_id,
    normalize_content,
    turn_payload,
    utc_iso,
    validate_data,
)


@dataclass(frozen=True)
class RuntimeRootState:
    """One synthetic Session root persisted with identifiers only."""

    session_id: str
    thread_id: str
    id: str = field(default_factory=new_node_id)

    def __post_init__(self) -> None:
        for name in ("session_id", "thread_id", "id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise RuntimeStateValidationError(f"root {name} must be a non-empty string.")
        if self.thread_id != self.session_id:
            raise RuntimeStateValidationError("A Session root must use thread_id == session_id.")

    @property
    def key(self) -> tuple[str, str]:
        return self.session_id, self.id

    def clone(self) -> RuntimeRootState:
        return RuntimeRootState.from_dict(self.to_dict())

    def to_dict(self) -> dict[str, str]:
        return {"session_id": self.session_id, "thread_id": self.thread_id, "id": self.id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeRootState:
        raw = _mapping(value, "root Turn")
        required = {"session_id", "thread_id", "id"}
        if set(raw) != required:
            raise RuntimeStateValidationError("A root Turn must contain only session_id, thread_id, and id.")
        return cls(
            session_id=_string(raw, "session_id"),
            thread_id=_string(raw, "thread_id"),
            id=_string(raw, "id"),
        )

    @classmethod
    def create(cls, session_id: str, *, id: str | None = None) -> RuntimeRootState:
        return cls(session_id=session_id, thread_id=session_id, id=id or new_node_id())


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
    project_cwd: str = ""
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
        object.__setattr__(self, "project_cwd", _normalize_cwd(self.project_cwd, name="project_cwd"))
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
            "project_cwd": self.project_cwd,
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
            "project_cwd",
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
            project_cwd=_string(raw, "project_cwd"),
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
        parent: RuntimeState | RuntimeRootState | None = None,
        id: str | None = None,
        user: str = "",
        provider_name: str = "",
        model: Mapping[str, Any] | None = None,
        permission_mode: PermissionMode = "read_only",
        running_mode: RunningMode = "agent",
        cwd: str = "",
        project_cwd: str = "",
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
            compaction_id=compaction_id or (parent.compaction_id if isinstance(parent, RuntimeState) else turn_id),
            user=user,
            provider_name=provider_name,
            model=dict(model or DEFAULT_MODEL),
            permission_mode=permission_mode,
            running_mode=running_mode,
            cwd=cwd,
            project_cwd=project_cwd,
            timestamp=timestamp or utc_iso(),
            status=status,
            data=data if data is not None else turn_payload(user_content),
        )


RuntimeNode: TypeAlias = RuntimeRootState | RuntimeState


def _runtime_node_sort_key(node: RuntimeNode) -> tuple[int, str, str]:
    if isinstance(node, RuntimeRootState):
        return (0, "", node.id)
    return (1, node.timestamp, node.id)


def runtime_node_from_dict(value: Mapping[str, Any]) -> RuntimeNode:
    """Parse the strict structural union used by storage and list APIs."""

    raw = _mapping(value, "runtime node")
    if set(raw) == {"session_id", "thread_id", "id"}:
        return RuntimeRootState.from_dict(raw)
    return RuntimeState.from_dict(raw)
