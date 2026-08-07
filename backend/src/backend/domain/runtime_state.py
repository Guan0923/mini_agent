"""Canonical RuntimeState message-tree protocol.

The runtime used to keep a conversation transcript, a transient exchange and
an event log as three different contracts.  This module is the small, domain
only contract shared by persistence, model adapters and clients.  A
``RuntimeState`` is one immutable-in-the-tree entry; streaming code may work
on a private copy and publish lifecycle frames through :class:`NodeWriter`.

The module deliberately contains no provider, database, HTTP or UI imports.
That makes the JSON shape useful as an interchange format and keeps recovery
tests deterministic.
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

APP_VERSION = "0.2.0"
DEFAULT_COMPACTION_RETENTION = 8

NodeStatus: TypeAlias = Literal["failed", "success", "abort"]
NodeDataType: TypeAlias = Literal["message", "thinking_level_change", "model_change", "compaction"]
MessageRole: TypeAlias = Literal["user", "assistant", "tool_result", "bash"]
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
]

NODE_STATUSES = frozenset({"failed", "success", "abort"})
NODE_DATA_TYPES = frozenset({"message", "thinking_level_change", "model_change", "compaction"})
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
    }
)
MESSAGE_ROLES = frozenset({"user", "assistant", "tool_result", "bash"})


class RuntimeStateValidationError(ValueError):
    """Raised when a node or its discriminated data payload is invalid."""


def utc_iso() -> str:
    """Return a timezone-aware, stable representation for a node timestamp."""

    return datetime.now(UTC).isoformat()


def new_node_id() -> str:
    return f"node_{uuid4().hex}"


def new_session_id() -> str:
    return f"session_{uuid4().hex}"


def _clone(value: Any) -> Any:
    """Clone JSON-shaped values without sharing mutable data with callers."""

    return copy.deepcopy(value)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeStateValidationError(f"{name} must be a JSON object.")
    return {str(key): _clone(item) for key, item in value.items()}


def _string_field(raw: Mapping[str, Any], name: str, default: str = "") -> str:
    value = raw.get(name, default)
    if not isinstance(value, str):
        raise RuntimeStateValidationError(f"{name} must be a string.")
    return value


def _json_safe(value: Any, name: str = "value") -> Any:
    """Validate and return a detached JSON value.

    Persisting arbitrary Python objects in ``data`` would make snapshots
    provider- or process-dependent.  Reject them at the domain boundary.
    """

    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeStateValidationError(f"{name} must contain JSON values only.") from exc
    return _clone(value)


def _text_block(value: str, *, block_type: ContentBlockType = "text") -> dict[str, Any]:
    if not isinstance(value, str):
        raise RuntimeStateValidationError(f"{block_type} block text must be a string.")
    return {"type": block_type, "text": value}


def normalize_content(content: str | Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize text or provider-neutral content blocks to ``content[]``."""

    if content is None:
        return []
    if isinstance(content, str):
        return [_text_block(content)]
    if isinstance(content, Mapping):
        content = [content]
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray)):
        raise RuntimeStateValidationError("message.content must be a string or an array of content blocks.")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(content):
        block = _mapping(raw, f"message.content[{index}]")
        block_type = block.get("type")
        if not isinstance(block_type, str) or block_type not in CONTENT_BLOCK_TYPES:
            raise RuntimeStateValidationError(f"Unsupported content block type: {block_type!r}.")
        if block_type in {"text", "reasoning", "bash"}:
            text = block.get("text")
            if not isinstance(text, str):
                raise RuntimeStateValidationError(f"{block_type} block requires string text.")
        elif block_type == "tool_call":
            if not isinstance(block.get("name"), str) or not block["name"]:
                raise RuntimeStateValidationError("tool_call block requires name.")
            if not isinstance(block.get("call_id"), str) or not block["call_id"]:
                raise RuntimeStateValidationError("tool_call block requires call_id.")
            if not isinstance(block.get("arguments", {}), Mapping):
                raise RuntimeStateValidationError("tool_call.arguments must be an object.")
        elif block_type == "tool_result":
            if not isinstance(block.get("call_id"), str) or not block["call_id"]:
                raise RuntimeStateValidationError("tool_result block requires call_id.")
        elif block_type in {"plan", "approval", "question", "subagent", "skill_snapshot"}:
            # These blocks intentionally allow a small evolving JSON payload;
            # ``type`` is the stable discriminator and all values remain JSON.
            pass
        result.append(_json_safe(block, f"message.content[{index}]"))
    return result


def message_payload(
    role: MessageRole,
    content: str | Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a canonical ``data`` payload for a message node."""

    if not isinstance(role, str) or role not in MESSAGE_ROLES:
        raise RuntimeStateValidationError(f"Unsupported message role: {role!r}.")
    payload: dict[str, Any] = {"type": "message", "message": {"role": role, "content": normalize_content(content)}}
    if extra:
        payload["message"].update(_json_safe(extra, "message metadata"))
    return payload


def change_payload(kind: Literal["thinking_level_change", "model_change"], **values: Any) -> dict[str, Any]:
    """Build a validated model/thinking configuration node payload."""

    if not values:
        raise RuntimeStateValidationError(f"{kind} requires at least one value.")
    return {"type": kind, **_json_safe(values, kind)}


def compaction_payload(summary: str, *, source_ids: Sequence[str] = ()) -> dict[str, Any]:
    if not isinstance(summary, str):
        raise RuntimeStateValidationError("compaction summary must be a string.")
    if any(not isinstance(item, str) or not item for item in source_ids):
        raise RuntimeStateValidationError("compaction source_ids must contain non-empty strings.")
    return {"type": "compaction", "summary": summary, "source_ids": list(source_ids)}


def validate_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a node's discriminated union and return a detached copy.

    ``{}`` is accepted for a create placeholder.  A writer can fill it before
    the final delete frame; completed domain entries should use one of the
    four explicit types.
    """

    payload = _mapping(data, "data")
    if not payload:
        return payload
    data_type = payload.get("type")
    if not isinstance(data_type, str) or data_type not in NODE_DATA_TYPES:
        raise RuntimeStateValidationError(f"Unsupported data.type: {data_type!r}.")
    if data_type == "message":
        if "message" not in payload and payload.get("role") in MESSAGE_ROLES:
            # Accept the compact first-draft shape while serializing the
            # canonical nested ``message`` object.
            metadata = payload.pop("metadata", {})
            if not isinstance(metadata, Mapping):
                raise RuntimeStateValidationError("message.metadata must be an object.")
            message = {
                "role": payload.pop("role"),
                "content": payload.pop("content", []),
                **metadata,
            }
        else:
            message = _mapping(payload.get("message"), "data.message")
        role = message.get("role")
        if role not in MESSAGE_ROLES:
            raise RuntimeStateValidationError(f"Unsupported message role: {role!r}.")
        message["content"] = normalize_content(message.get("content"))
        # Metadata is deliberately JSON-only, but never accept a second
        # provider wire payload under a message node.
        payload["message"] = message
    elif data_type == "thinking_level_change":
        if "level" not in payload and isinstance(payload.get("thinking_level"), str):
            payload["level"] = payload["thinking_level"]
        if not isinstance(payload.get("level"), str) or not payload["level"]:
            raise RuntimeStateValidationError("thinking_level_change requires a non-empty level.")
    elif data_type == "model_change":
        if not isinstance(payload.get("model"), str) or not payload["model"]:
            raise RuntimeStateValidationError("model_change requires a non-empty model.")
        if "provider" in payload and not isinstance(payload["provider"], str):
            raise RuntimeStateValidationError("model_change.provider must be a string.")
    elif data_type == "compaction":
        if "summary" not in payload:
            payload["summary"] = ""
        if not isinstance(payload.get("summary"), str):
            raise RuntimeStateValidationError("compaction summary must be a string.")
        source_ids = payload.get("source_ids", [])
        if not isinstance(source_ids, list) or any(not isinstance(item, str) for item in source_ids):
            raise RuntimeStateValidationError("compaction.source_ids must be an array of ids.")
    return _json_safe(payload, "data")


def _normalize_cwd(cwd: str) -> str:
    if not isinstance(cwd, str):
        raise RuntimeStateValidationError("cwd must be a string.")
    if not cwd:
        return ""
    # ``Path.resolve`` is non-strict and therefore safe for a workspace that
    # has not been created yet.  Preserve a caller's empty cwd for protocol
    # compatibility with non-filesystem clients.
    try:
        return os.path.normpath(str(Path(cwd).expanduser().resolve(strict=False)))
    except (OSError, RuntimeError) as exc:
        raise RuntimeStateValidationError("cwd must be a valid path.") from exc


@dataclass
class RuntimeState:
    """One serializable node in the RuntimeState message tree.

    The dataclass is mutable only for constructing a dynamic streaming copy.
    Persisted nodes must be changed through :class:`NodeWriter`, which enforces
    leaf and terminal-state rules.  ``timestamp`` and identity fields are
    never changed by the writer.
    """

    session_id: str
    parent_session_id: str = ""
    id: str = field(default_factory=new_node_id)
    parent_id: str = ""
    version: str = APP_VERSION
    first_kept_entry_id: str | None = None
    compaction_idx: str | None = None
    user: str = ""
    provider: str = ""
    cwd: str = ""
    timestamp: str = field(default_factory=utc_iso)
    status: NodeStatus = "failed"
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("session_id", "parent_session_id", "id", "parent_id", "version", "user", "provider"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise RuntimeStateValidationError(f"{name} must be a string.")
        if not self.session_id:
            raise RuntimeStateValidationError("session_id must not be empty.")
        if not self.id:
            raise RuntimeStateValidationError("id must not be empty.")
        if self.version != APP_VERSION:
            raise RuntimeStateValidationError(f"RuntimeState.version must be {APP_VERSION!r}.")
        if not isinstance(self.status, str) or self.status not in NODE_STATUSES:
            raise RuntimeStateValidationError(f"Unsupported node status: {self.status!r}.")
        if bool(self.parent_id) != bool(self.parent_session_id):
            raise RuntimeStateValidationError("parent_id and parent_session_id must be set together.")
        try:
            parsed_timestamp = datetime.fromisoformat(self.timestamp)
        except (TypeError, ValueError) as exc:
            raise RuntimeStateValidationError("timestamp must be an ISO 8601 datetime.") from exc
        if parsed_timestamp.tzinfo is None:
            raise RuntimeStateValidationError("timestamp must include a timezone.")
        if parsed_timestamp.utcoffset() != timedelta(0):
            raise RuntimeStateValidationError("timestamp must be expressed in UTC.")
        object.__setattr__(self, "cwd", _normalize_cwd(self.cwd))
        object.__setattr__(self, "data", validate_data(self.data))
        if self.first_kept_entry_id in {None, ""}:
            object.__setattr__(self, "first_kept_entry_id", self.id)
        if self.compaction_idx in {None, ""}:
            object.__setattr__(self, "compaction_idx", self.id)
        for name in ("first_kept_entry_id", "compaction_idx"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise RuntimeStateValidationError(f"{name} must be a non-empty node id.")

    @property
    def firstKeptEntryId(self) -> str:  # noqa: N802 - protocol key spelling
        return self.first_kept_entry_id or self.id

    @firstKeptEntryId.setter
    def firstKeptEntryId(self, value: str) -> None:  # noqa: N802
        self.first_kept_entry_id = value

    @property
    def compactionIdx(self) -> str:  # noqa: N802 - protocol key spelling
        return self.compaction_idx or self.id

    @compactionIdx.setter
    def compactionIdx(self, value: str) -> None:  # noqa: N802
        self.compaction_idx = value

    @property
    def key(self) -> tuple[str, str]:
        return self.session_id, self.id

    @property
    def is_terminal(self) -> bool:
        return self.status in NODE_STATUSES

    @property
    def data_type(self) -> str | None:
        value = self.data.get("type")
        return value if isinstance(value, str) else None

    @property
    def message(self) -> Mapping[str, Any] | None:
        value = self.data.get("message") if self.data_type == "message" else None
        return value if isinstance(value, Mapping) else None

    @property
    def role(self) -> str | None:
        value = self.message.get("role") if self.message is not None else None
        return value if isinstance(value, str) else None

    @property
    def content(self) -> list[dict[str, Any]]:
        message = self.message
        value = message.get("content", []) if message is not None else []
        return [dict(item) for item in value if isinstance(item, Mapping)]

    def clone(self) -> RuntimeState:
        return RuntimeState.from_dict(self.to_dict())

    def with_data(self, data: Mapping[str, Any]) -> RuntimeState:
        """Return a dynamic copy with unchanged identity/timestamp/pointers."""

        result = self.clone()
        result.data = validate_data(data)
        return result

    def with_status(self, status: NodeStatus) -> RuntimeState:
        if not isinstance(status, str) or status not in NODE_STATUSES:
            raise RuntimeStateValidationError(f"Unsupported node status: {status!r}.")
        result = self.clone()
        result.status = status
        return result

    def to_dict(self) -> dict[str, Any]:
        """Serialize using the exact public JSON key names."""

        return {
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "id": self.id,
            "parent_id": self.parent_id,
            "version": self.version,
            "firstKeptEntryId": self.firstKeptEntryId,
            "compactionIdx": self.compactionIdx,
            "user": self.user,
            "provider": self.provider,
            "cwd": self.cwd,
            "timestamp": self.timestamp,
            "status": self.status,
            "data": _clone(self.data),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RuntimeState:
        raw = _mapping(value, "node")
        if "children_id" in raw:
            raise RuntimeStateValidationError("children_id is not part of RuntimeState; query children by parent keys.")
        required = {
            "session_id",
            "parent_session_id",
            "id",
            "parent_id",
            "version",
            "firstKeptEntryId",
            "compactionIdx",
            "user",
            "provider",
            "cwd",
            "timestamp",
            "status",
            "data",
        }
        missing = sorted(required.difference(raw))
        if missing:
            raise RuntimeStateValidationError(f"node is missing required fields: {', '.join(missing)}")
        first_kept = raw.get("firstKeptEntryId", raw.get("first_kept_entry_id"))
        compaction = raw.get("compactionIdx", raw.get("compaction_idx"))
        if first_kept is not None and not isinstance(first_kept, str):
            raise RuntimeStateValidationError("firstKeptEntryId must be a string.")
        if compaction is not None and not isinstance(compaction, str):
            raise RuntimeStateValidationError("compactionIdx must be a string.")
        timestamp = raw.get("timestamp")
        if not isinstance(timestamp, str):
            raise RuntimeStateValidationError("timestamp must be a string.")
        return cls(
            session_id=_string_field(raw, "session_id"),
            parent_session_id=_string_field(raw, "parent_session_id"),
            id=_string_field(raw, "id"),
            parent_id=_string_field(raw, "parent_id"),
            version=_string_field(raw, "version", APP_VERSION),
            first_kept_entry_id=first_kept,
            compaction_idx=compaction,
            user=_string_field(raw, "user"),
            provider=_string_field(raw, "provider"),
            cwd=_string_field(raw, "cwd"),
            timestamp=timestamp,
            status=raw.get("status", "failed"),  # type: ignore[arg-type]
            data=_mapping(raw.get("data", {}), "data"),
        )

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        parent: RuntimeState | tuple[str, str] | None = None,
        parent_session_id: str | None = None,
        parent_id: str | None = None,
        user: str = "",
        provider: str = "",
        cwd: str = "",
        data: Mapping[str, Any] | None = None,
        first_kept_entry_id: str | None = None,
        compaction_idx: str | None = None,
        id: str | None = None,
        timestamp: str | None = None,
    ) -> RuntimeState:
        """Create a node and derive ancestry pointers for normal children."""

        if parent is not None:
            if isinstance(parent, RuntimeState):
                parent_session_id, parent_id = parent.session_id, parent.id
                if first_kept_entry_id is None:
                    first_kept_entry_id = parent.firstKeptEntryId
                if compaction_idx is None:
                    compaction_idx = parent.compactionIdx
            else:
                parent_session_id, parent_id = parent
        return cls(
            session_id=session_id,
            parent_session_id=parent_session_id or "",
            id=id or new_node_id(),
            parent_id=parent_id or "",
            first_kept_entry_id=first_kept_entry_id,
            compaction_idx=compaction_idx,
            user=user,
            provider=provider,
            cwd=cwd,
            timestamp=timestamp or utc_iso(),
            data=dict(data or {}),
        )


class RuntimeNodeStore(Protocol):
    """Minimal store port consumed by :class:`NodeWriter`."""

    def create_node(self, node: RuntimeState) -> None: ...

    def get_node(self, session_id: str, node_id: str) -> RuntimeState | None: ...

    def list_children(self, parent_session_id: str, parent_id: str) -> list[RuntimeState]: ...

    def load_nodes(self, session_id: str) -> list[RuntimeState]: ...

    def finalize_node(self, node: RuntimeState) -> None: ...


class InMemoryNodeStore:
    """Small reference store used by tests, embedded runtimes and adapters."""

    def __init__(self, nodes: Iterable[RuntimeState] = ()) -> None:
        self._nodes: dict[tuple[str, str], RuntimeState] = {}
        self._lock = RLock()
        for node in nodes:
            self.create_node(node)

    def create_node(self, node: RuntimeState) -> None:
        with self._lock:
            if node.key in self._nodes:
                raise RuntimeStateValidationError(f"Node already exists: {node.session_id}/{node.id}.")
            if node.parent_id and (node.parent_session_id, node.parent_id) not in self._nodes:
                raise RuntimeStateValidationError("A node parent must be present in the store.")
            self._nodes[node.key] = node.clone()

    def get_node(self, session_id: str, node_id: str) -> RuntimeState | None:
        with self._lock:
            node = self._nodes.get((session_id, node_id))
            return node.clone() if node is not None else None

    def list_children(self, parent_session_id: str, parent_id: str) -> list[RuntimeState]:
        with self._lock:
            return sorted(
                (
                    node.clone()
                    for node in self._nodes.values()
                    if node.parent_session_id == parent_session_id and node.parent_id == parent_id
                ),
                key=lambda item: (item.timestamp, item.id),
            )

    def finalize_node(self, node: RuntimeState) -> None:
        with self._lock:
            existing = self._nodes.get(node.key)
            if existing is None:
                raise KeyError(f"Unknown node: {node.session_id}/{node.id}.")
            if existing.status != "failed":
                raise RuntimeStateValidationError("Sealed runtime nodes are read-only.")
            if self.list_children(node.session_id, node.id):
                raise RuntimeStateValidationError("Only a leaf node can be finalized.")
            self._nodes[node.key] = node.clone()

    def all_nodes(self, session_id: str | None = None) -> list[RuntimeState]:
        with self._lock:
            values = [node for node in self._nodes.values() if session_id is None or node.session_id == session_id]
            return sorted((node.clone() for node in values), key=lambda item: (item.timestamp, item.id))

    def load_nodes(self, session_id: str) -> list[RuntimeState]:
        """Load a session's nodes plus any cross-session ancestors they need.

        Fork roots deliberately point at a node in another session.  Returning
        that ancestor chain here keeps model-context and recovery callers from
        having to know how sessions are physically partitioned.
        """

        with self._lock:
            result: dict[tuple[str, str], RuntimeState] = {
                node.key: node.clone() for node in self._nodes.values() if node.session_id == session_id
            }
            pending = list(result.values())
            while pending:
                node = pending.pop()
                if not node.parent_id:
                    continue
                key = (node.parent_session_id, node.parent_id)
                parent = self._nodes.get(key)
                if parent is not None and key not in result:
                    result[key] = parent.clone()
                    pending.append(parent)
            return sorted(result.values(), key=lambda item: (item.timestamp, item.id))

    def snapshot(self) -> list[dict[str, Any]]:
        return [node.to_dict() for node in self.all_nodes()]


class RuntimeStateTree:
    """In-memory path and branch operations over canonical nodes."""

    def __init__(self, nodes: Iterable[RuntimeState] = ()) -> None:
        self._nodes: dict[tuple[str, str], RuntimeState] = {}
        for node in nodes:
            if node.key in self._nodes:
                raise RuntimeStateValidationError(f"Duplicate node: {node.session_id}/{node.id}.")
            self._nodes[node.key] = node.clone()

    def add(self, node: RuntimeState) -> RuntimeState:
        if node.key in self._nodes:
            raise RuntimeStateValidationError(f"Node already exists: {node.session_id}/{node.id}.")
        if node.parent_id and (node.parent_session_id, node.parent_id) not in self._nodes:
            raise RuntimeStateValidationError("A node parent must be present in the tree.")
        self._nodes[node.key] = node.clone()
        return node.clone()

    def get(self, session_id: str, node_id: str) -> RuntimeState:
        try:
            return self._nodes[(session_id, node_id)].clone()
        except KeyError as exc:
            raise KeyError(f"Unknown node: {session_id}/{node_id}.") from exc

    def try_get(self, session_id: str, node_id: str) -> RuntimeState | None:
        node = self._nodes.get((session_id, node_id))
        return node.clone() if node is not None else None

    def children(self, parent_session_id: str, parent_id: str) -> list[RuntimeState]:
        return sorted(
            (
                node.clone()
                for node in self._nodes.values()
                if node.parent_session_id == parent_session_id and node.parent_id == parent_id
            ),
            key=lambda item: (item.timestamp, item.id),
        )

    def is_leaf(self, session_id: str, node_id: str) -> bool:
        return not self.children(session_id, node_id)

    def ancestors(self, session_id: str, node_id: str, *, include_self: bool = True) -> list[RuntimeState]:
        current = self.get(session_id, node_id)
        result: list[RuntimeState] = [current] if include_self else []
        seen: set[tuple[str, str]] = {current.key}
        while current.parent_id:
            key = (current.parent_session_id, current.parent_id)
            if key in seen:
                raise RuntimeStateValidationError("RuntimeState parent chain contains a cycle.")
            seen.add(key)
            current = self.get(*key)
            result.append(current)
        result.reverse()
        return result

    def roots(self, session_id: str | None = None) -> list[RuntimeState]:
        return sorted(
            (
                node.clone()
                for node in self._nodes.values()
                if (session_id is None or node.session_id == session_id)
                and (not node.parent_id or node.parent_session_id != node.session_id)
            ),
            key=lambda item: (item.timestamp, item.id),
        )

    def create_child(
        self,
        *,
        session_id: str,
        parent: RuntimeState | tuple[str, str] | None = None,
        data: Mapping[str, Any] | None = None,
        user: str | None = None,
        provider: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> RuntimeState:
        parent_node: RuntimeState | None = None
        if parent is not None:
            parent_key = parent.key if isinstance(parent, RuntimeState) else parent
            parent_node = self.get(*parent_key)
        node = RuntimeState.create(
            session_id=session_id,
            parent=parent_node,
            data=data,
            user=user if user is not None else (parent_node.user if parent_node else ""),
            provider=provider if provider is not None else (parent_node.provider if parent_node else ""),
            cwd=cwd if cwd is not None else (parent_node.cwd if parent_node else ""),
            **kwargs,
        )
        return self.add(node)

    def fork(
        self, source: RuntimeState | tuple[str, str], *, session_id: str | None = None, **kwargs: Any
    ) -> RuntimeState:
        source_node = source if isinstance(source, RuntimeState) else self.get(*source)
        if not self.is_leaf(source_node.session_id, source_node.id):
            raise RuntimeStateValidationError("Fork source must be a leaf node.")
        return self.create_child(
            session_id=session_id or new_session_id(),
            parent=source_node,
            first_kept_entry_id=source_node.firstKeptEntryId,
            compaction_idx=source_node.compactionIdx,
            user=kwargs.pop("user", source_node.user),
            provider=kwargs.pop("provider", source_node.provider),
            cwd=kwargs.pop("cwd", source_node.cwd),
            **kwargs,
        )

    def resume(
        self,
        source: RuntimeState | tuple[str, str],
        *,
        data: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> RuntimeState:
        """Create a continuation from a failed or user-paused leaf."""

        source_node = source if isinstance(source, RuntimeState) else self.get(*source)
        if source_node.status not in {"abort", "failed"}:
            raise RuntimeStateValidationError("Only failed or abort nodes can be resumed.")
        if not self.is_leaf(source_node.session_id, source_node.id):
            raise RuntimeStateValidationError("Only a leaf node can be resumed.")
        return self.create_child(session_id=source_node.session_id, parent=source_node, data=data, **kwargs)

    def compact(
        self,
        source: RuntimeState | tuple[str, str],
        summary: str,
        *,
        retention: int = DEFAULT_COMPACTION_RETENTION,
        source_ids: Sequence[str] | None = None,
        **kwargs: Any,
    ) -> RuntimeState:
        if retention < 1:
            raise ValueError("retention must be positive")
        current = source if isinstance(source, RuntimeState) else self.get(*source)
        if not self.is_leaf(current.session_id, current.id):
            raise RuntimeStateValidationError("Compaction must start from a leaf node.")
        path = self.ancestors(current.session_id, current.id)
        kept = path[max(0, len(path) - retention)]
        old_compaction_index = next(
            (index for index, item in enumerate(path) if item.id == current.compactionIdx),
            0,
        )
        payload = compaction_payload(
            summary,
            source_ids=source_ids or [item.id for item in path[old_compaction_index:]],
        )
        node = self.create_child(
            session_id=current.session_id,
            parent=current,
            data=payload,
            user=kwargs.pop("user", current.user),
            provider=kwargs.pop("provider", current.provider),
            cwd=kwargs.pop("cwd", current.cwd),
            first_kept_entry_id=kept.id,
            compaction_idx=None,  # set to this node's id below
            **kwargs,
        )
        node.compaction_idx = node.id
        node.status = "success"
        self._nodes[node.key] = node.clone()
        return node

    def model_input(self, source: RuntimeState | tuple[str, str]) -> list[RuntimeState]:
        """Return the deterministic model context for a leaf/path.

        A compaction summary is placed first, followed by the retained raw
        window and any nodes created after that summary.  Original ancestors
        are never deleted from the tree.
        """

        current = source if isinstance(source, RuntimeState) else self.get(*source)
        path = self.ancestors(current.session_id, current.id)
        compaction_positions = [i for i, item in enumerate(path) if item.data.get("type") == "compaction"]
        if not compaction_positions:
            return path
        summary_index = compaction_positions[-1]
        summary = path[summary_index]
        first_id = summary.firstKeptEntryId
        first_index = next((i for i, item in enumerate(path) if item.id == first_id), 0)
        # The raw window is intentionally limited to the entries before the
        # summary node; a descendant path continues after it.  A later
        # compaction supersedes older summary nodes, so do not feed duplicate
        # summaries back to the provider even though the originals remain in
        # the durable tree.
        raw_window = [item for item in path[first_index:summary_index] if item.data.get("type") != "compaction"]
        return [summary, *raw_window, *path[summary_index + 1 :]]

    def all_nodes(self, session_id: str | None = None) -> list[RuntimeState]:
        return sorted(
            (node.clone() for node in self._nodes.values() if session_id is None or node.session_id == session_id),
            key=lambda item: (item.timestamp, item.id),
        )


NodeFrameType: TypeAlias = Literal["node.create", "node.update", "node.delete"]


@dataclass(frozen=True)
class NodeFrame:
    """Transport-neutral lifecycle frame shared by SSE, TUI and Web."""

    type: NodeFrameType
    node: RuntimeState

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "node": self.node.to_dict()}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    def as_sse(self) -> str:
        return f"data: {self.to_json()}\n\n"


class NodeWriter:
    """Create dynamic nodes and publish complete replacement updates.

    The store receives only the failed placeholder and the final delete.  An
    interrupted process therefore leaves a recoverable failed node, while
    high-volume streaming updates remain ephemeral.
    """

    def __init__(
        self,
        store: RuntimeNodeStore,
        *,
        emit: Callable[[NodeFrame], None] | None = None,
        id_factory: Callable[[], str] = new_node_id,
        clock: Callable[[], str] = utc_iso,
    ) -> None:
        self.store = store
        self.emit = emit or (lambda _frame: None)
        self.id_factory = id_factory
        self.clock = clock
        self._dynamic: dict[tuple[str, str], RuntimeState] = {}
        self._lock = RLock()

    def create(
        self,
        *,
        session_id: str,
        parent: RuntimeState | tuple[str, str] | None = None,
        parent_session_id: str = "",
        parent_id: str = "",
        data: Mapping[str, Any] | None = None,
        user: str = "",
        provider: str = "",
        cwd: str = "",
        first_kept_entry_id: str | None = None,
        compaction_idx: str | None = None,
    ) -> RuntimeState:
        with self._lock:
            parent_node: RuntimeState | None = parent if isinstance(parent, RuntimeState) else None
            if isinstance(parent, tuple):
                parent_node = self.store.get_node(*parent)
            if parent is not None and parent_node is None:
                raise KeyError("NodeWriter parent does not exist.")
            if parent_node is not None:
                parent_session_id, parent_id = parent_node.session_id, parent_node.id
                if first_kept_entry_id is None:
                    first_kept_entry_id = parent_node.firstKeptEntryId
                if compaction_idx is None:
                    compaction_idx = parent_node.compactionIdx
                user = user or parent_node.user
                provider = provider or parent_node.provider
                cwd = cwd or parent_node.cwd
            node = RuntimeState.create(
                session_id=session_id,
                parent_session_id=parent_session_id,
                parent_id=parent_id,
                id=self.id_factory(),
                timestamp=self.clock(),
                first_kept_entry_id=first_kept_entry_id,
                compaction_idx=compaction_idx,
                data=data,
                user=user,
                provider=provider,
                cwd=cwd,
            )
            # Persistence receives only an empty failed placeholder.  The
            # fully populated copy is kept in the writer's dynamic sidecar
            # until the terminal delete atomically seals the leaf.
            self.store.create_node(node.with_data({}))
            self._dynamic[node.key] = node.clone()
            self.emit(NodeFrame("node.create", node.clone()))
            return node.clone()

    def current(self, session_id: str, node_id: str) -> RuntimeState:
        with self._lock:
            try:
                return self._dynamic[(session_id, node_id)].clone()
            except KeyError as exc:
                raise KeyError(f"Node is not a dynamic leaf: {session_id}/{node_id}.") from exc

    def update(
        self,
        session_id: str,
        node_id: str,
        *,
        data: Mapping[str, Any] | None = None,
        status: NodeStatus | None = None,
    ) -> RuntimeState:
        with self._lock:
            node = self.current(session_id, node_id)
            if self.store.list_children(session_id, node_id):
                raise RuntimeStateValidationError("Only a leaf dynamic node can be updated.")
            if data is not None:
                node.data = validate_data(data)
            if status is not None:
                if not isinstance(status, str) or status != "failed":
                    raise RuntimeStateValidationError(
                        "node.update keeps status='failed'; terminal status belongs to node.delete."
                    )
                node.status = "failed"
            self._dynamic[node.key] = node.clone()
            self.emit(NodeFrame("node.update", node.clone()))
            return node.clone()

    def update_data(self, node: RuntimeState, data: Mapping[str, Any]) -> RuntimeState:
        return self.update(node.session_id, node.id, data=data)

    def append_content(self, node: RuntimeState, block: Mapping[str, Any]) -> RuntimeState:
        """Append one canonical content block and emit a full replacement."""

        current = self.current(node.session_id, node.id)
        if current.data.get("type") != "message":
            raise RuntimeStateValidationError("append_content requires a message node.")
        message = current.data.get("message")
        if not isinstance(message, Mapping):
            raise RuntimeStateValidationError("Message node is missing message object.")
        content = list(message.get("content", []))
        content.append(dict(block))
        return self.update_data(node, message_payload(str(message.get("role")), content))  # type: ignore[arg-type]

    def delete(self, session_id: str, node_id: str, *, status: NodeStatus = "success") -> RuntimeState:
        with self._lock:
            if not isinstance(status, str) or status not in NODE_STATUSES:
                raise RuntimeStateValidationError(f"Unsupported node status: {status!r}.")
            node = self.current(session_id, node_id)
            node.status = status
            # finalize_node performs the leaf check and replaces the static
            # placeholder in one store transaction.
            self.store.finalize_node(node)
            self._dynamic.pop(node.key, None)
            self.emit(NodeFrame("node.delete", node.clone()))
            return node.clone()

    def fail(self, session_id: str, node_id: str) -> RuntimeState:
        return self.delete(session_id, node_id, status="failed")

    def abort(self, session_id: str, node_id: str) -> RuntimeState:
        return self.delete(session_id, node_id, status="abort")

    def resume(
        self,
        source: RuntimeState,
        *,
        data: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> RuntimeState:
        """Start a new dynamic child from a failed/aborted source."""

        if source.status not in {"abort", "failed"}:
            raise RuntimeStateValidationError("Only failed or abort nodes can be resumed.")
        if self.store.list_children(source.session_id, source.id):
            raise RuntimeStateValidationError("Only a leaf node can be resumed.")
        return self.create(parent=source, session_id=source.session_id, data=data, **kwargs)

    def active_nodes(self) -> list[RuntimeState]:
        with self._lock:
            return [node.clone() for node in self._dynamic.values()]


def recoverable(node: RuntimeState) -> bool:
    """Whether a failed node can be safely retried without replaying effects."""

    if node.status != "failed":
        return False
    if node.data.get("type") != "message":
        return True
    message = node.data.get("message")
    if not isinstance(message, Mapping):
        return True
    if message.get("role") == "bash":
        return False
    for block in message.get("content", []):
        if isinstance(block, Mapping) and block.get("type") == "bash":
            return False
        if isinstance(block, Mapping) and block.get("type") == "tool_call":
            if block.get("replay_safe") is False or block.get("side_effect") is True:
                return False
            name = str(block.get("tool") or block.get("name") or "").lower()
            if any(token in name for token in ("bash", "write", "mcp")):
                return False
        if isinstance(block, Mapping) and block.get("type") == "tool_result":
            if block.get("replay_safe") is False or block.get("side_effect") is True:
                return False
            name = str(block.get("tool") or block.get("name") or "").lower()
            if any(token in name for token in ("bash", "write", "mcp")):
                return False
    return True


def parent_reference(node: RuntimeState) -> tuple[str, str] | None:
    return (node.parent_session_id, node.parent_id) if node.parent_id else None


__all__ = [
    "APP_VERSION",
    "CONTENT_BLOCK_TYPES",
    "ContentBlockType",
    "DEFAULT_COMPACTION_RETENTION",
    "InMemoryNodeStore",
    "MESSAGE_ROLES",
    "MessageRole",
    "NODE_DATA_TYPES",
    "NODE_STATUSES",
    "NodeDataType",
    "NodeFrame",
    "NodeFrameType",
    "NodeStatus",
    "NodeWriter",
    "RuntimeNodeStore",
    "RuntimeState",
    "RuntimeStateTree",
    "RuntimeStateValidationError",
    "change_payload",
    "compaction_payload",
    "message_payload",
    "new_node_id",
    "new_session_id",
    "normalize_content",
    "parent_reference",
    "recoverable",
    "utc_iso",
    "validate_data",
]
