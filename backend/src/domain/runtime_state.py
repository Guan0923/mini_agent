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
from uuid import NAMESPACE_URL, uuid4, uuid5

APP_VERSION = "0.3.0"
DEFAULT_COMPACTION_RETENTION = 8

NodeStatus: TypeAlias = Literal["failed", "success", "abort"]
TerminalErrorCategory: TypeAlias = Literal["unknown", "tool", "agent", "user", "network"]
NodeDataType: TypeAlias = Literal["message", "compaction", "root"]
MessageRole: TypeAlias = Literal["user", "assistant", "tool_result", "bash"]
ReasoningEffort: TypeAlias = Literal["low", "medium", "high", "xhigh", "max"]
ThinkingMode: TypeAlias = Literal["enable", "disable"]
PermissionMode: TypeAlias = Literal["approval_for_me", "full_access"]
RunningMode: TypeAlias = Literal["agent", "plan"]
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
NODE_DATA_TYPES = frozenset({"message", "compaction", "root"})
REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
THINKING_MODES = frozenset({"enable", "disable"})
PERMISSION_MODES = frozenset({"approval_for_me", "full_access"})
RUNNING_MODES = frozenset({"agent", "plan"})
DEFAULT_MODEL: dict[str, Any] = {
    "reasoning_effort": "medium",
    "current_model": "unknown",
    "context_length": 128000,
    "output_length": 8192,
    "thinking": "enable",
    "temperature": 1.0,
}
ROOT_MODEL: dict[str, Any] = {
    **DEFAULT_MODEL,
    "reasoning_effort": "max",
    "temperature": 0.7,
}
USAGE_FIELDS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
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

FAILED_TERMINAL_MESSAGE = "An unknown error caused the system to encounter an exception."
ABORT_TERMINAL_MESSAGES: dict[TerminalErrorCategory, str] = {
    "unknown": "The run was aborted for an unknown reason.",
    "tool": "The run was aborted because an internal tool error prevented execution from continuing.",
    "agent": "The run was aborted because the agent encountered an internal error.",
    "user": "The run was aborted at the user's request.",
    "network": (
        "The run was aborted because a network error interrupted communication with the model or a required service."
    ),
}


class RuntimeStateValidationError(ValueError):
    """Raised when a node or its discriminated data payload is invalid."""


def utc_iso() -> str:
    """Return a timezone-aware, stable representation for a node timestamp."""

    return datetime.now(UTC).isoformat()


def new_node_id() -> str:
    return f"node_{uuid4().hex}"


def new_session_id() -> str:
    return f"session_{uuid4().hex}"


def session_root_id(session_id: str) -> str:
    """Return the deterministic root id for a session."""

    if not isinstance(session_id, str) or not session_id:
        raise RuntimeStateValidationError("session_id must not be empty.")
    return f"root_{uuid5(NAMESPACE_URL, f'mini-agent/session-root/{session_id}').hex}"


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


def terminal_error_payload(
    status: Literal["failed", "abort"],
    category: TerminalErrorCategory | None = None,
    *,
    code: str | None = None,
    detail: str | None = None,
) -> dict[str, str]:
    """Build the stable, provider-neutral reason attached to a terminal node.

    ``failed`` is deliberately the last-resort state and therefore never
    claims a cause that the runtime could not prove.  ``abort`` carries the
    best known source category so presentation and the next model turn share
    exactly the same explanation.
    """

    if status == "failed":
        resolved_category: TerminalErrorCategory = "unknown"
        message = FAILED_TERMINAL_MESSAGE
    elif status == "abort":
        resolved_category = category or "unknown"
        if resolved_category not in ABORT_TERMINAL_MESSAGES:
            raise RuntimeStateValidationError(f"Unsupported terminal error category: {resolved_category!r}.")
        message = (
            "The run was paused at the user's request."
            if resolved_category == "user" and code == "user_paused"
            else ABORT_TERMINAL_MESSAGES[resolved_category]
        )
    else:
        raise RuntimeStateValidationError(f"Unsupported terminal error status: {status!r}.")

    payload = {"category": resolved_category, "message": message}
    if code:
        payload["code"] = code
    normalized_detail = " ".join((detail or "").split())
    if status == "abort" and normalized_detail and normalized_detail.rstrip(".") != message.rstrip("."):
        payload["detail"] = normalized_detail
    return payload


def terminal_error_text(error: Mapping[str, Any]) -> str:
    """Render a structured terminal reason for people and model context."""

    message = str(error.get("message") or FAILED_TERMINAL_MESSAGE)
    detail = str(error.get("detail") or "").strip()
    return f"{message}\n\nDetails: {detail}" if detail else message


def compaction_payload(summary: str, *, source_ids: Sequence[str] = ()) -> dict[str, Any]:
    if not isinstance(summary, str):
        raise RuntimeStateValidationError("compaction summary must be a string.")
    if any(not isinstance(item, str) or not item for item in source_ids):
        raise RuntimeStateValidationError("compaction source_ids must contain non-empty strings.")
    return {"type": "compaction", "summary": summary, "source_ids": list(source_ids)}


def root_payload() -> dict[str, str]:
    """Build the provider-neutral payload for a session root node."""

    return {"type": "root"}


def change_payload(kind: str, **values: Any) -> dict[str, Any]:
    """Reject the removed configuration-node protocol explicitly.

    Runtime configuration is now represented by top-level node fields.  The
    name remains importable for older integrations so they receive a clear
    protocol error instead of an import failure.
    """

    raise RuntimeStateValidationError(f"{kind!r} is no longer a RuntimeState data type; use top-level configuration fields.")


def validate_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a node's discriminated union and return a detached copy.

    ``{}`` is accepted only for a create placeholder.  A writer fills it on
    the dynamic copy before the terminal replacement; completed entries use
    one of the two explicit types.
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
    elif data_type == "compaction":
        if "summary" not in payload:
            payload["summary"] = ""
        if not isinstance(payload.get("summary"), str):
            raise RuntimeStateValidationError("compaction summary must be a string.")
        source_ids = payload.get("source_ids", [])
        if not isinstance(source_ids, list) or any(not isinstance(item, str) for item in source_ids):
            raise RuntimeStateValidationError("compaction.source_ids must be an array of ids.")
    elif data_type == "root":
        if set(payload) != {"type"}:
            raise RuntimeStateValidationError("root data may only contain the type discriminator.")
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


def _normalize_model(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the provider-neutral model snapshot stored on every node."""

    raw = dict(DEFAULT_MODEL)
    if value is not None:
        supplied = _mapping(value, "model")
        unknown = set(supplied) - set(DEFAULT_MODEL)
        if unknown:
            raise RuntimeStateValidationError(f"Unsupported model fields: {', '.join(sorted(unknown))}.")
        raw.update(supplied)
    else:
        unknown = set()
    effort = raw.get("reasoning_effort")
    if effort not in REASONING_EFFORTS:
        raise RuntimeStateValidationError("model.reasoning_effort must be low, medium, high, xhigh, or max.")
    thinking = raw.get("thinking")
    if thinking not in THINKING_MODES:
        raise RuntimeStateValidationError("model.thinking must be enable or disable.")
    if thinking == "disable":
        # Keep the canonical field in the persisted snapshot for round-trip
        # stability, but callers must omit reasoning_effort when constructing
        # the provider request (the adapter enforces that boundary).
        raw["reasoning_effort"] = effort
    current_model = raw.get("current_model")
    if not isinstance(current_model, str) or not current_model:
        raise RuntimeStateValidationError("model.current_model must be a non-empty string.")
    context_length = raw.get("context_length")
    output_length = raw.get("output_length")
    if isinstance(context_length, bool) or not isinstance(context_length, int) or context_length < 1:
        raise RuntimeStateValidationError("model.context_length must be a positive integer.")
    if isinstance(output_length, bool) or not isinstance(output_length, int) or output_length < 1:
        raise RuntimeStateValidationError("model.output_length must be a positive integer.")
    if context_length <= output_length:
        raise RuntimeStateValidationError("model.context_length must be greater than model.output_length.")
    temperature = raw.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
        raise RuntimeStateValidationError("model.temperature must be between 0 and 2.")
    result = {
        "reasoning_effort": effort,
        "current_model": current_model,
        "context_length": context_length,
        "output_length": output_length,
        "thinking": thinking,
        "temperature": float(temperature),
    }
    return _json_safe(result, "model")


def _normalize_usage(value: Mapping[str, Any] | None) -> dict[str, int | None]:
    raw = {name: None for name in USAGE_FIELDS}
    if value is not None:
        supplied = _mapping(value, "usage")
        unknown = set(supplied) - set(USAGE_FIELDS)
        if unknown:
            raise RuntimeStateValidationError(f"Unsupported usage fields: {', '.join(sorted(unknown))}.")
        raw.update(supplied)
    for name in USAGE_FIELDS:
        item = raw[name]
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            raise RuntimeStateValidationError(f"usage.{name} must be a non-negative integer or null.")
    return {name: raw[name] for name in USAGE_FIELDS}


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
    provider_name: str = ""
    model: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_MODEL))
    permission_mode: PermissionMode = "approval_for_me"
    running_mode: RunningMode = "agent"
    usage: dict[str, int | None] = field(default_factory=lambda: {name: None for name in USAGE_FIELDS})
    cwd: str = ""
    timestamp: str = field(default_factory=utc_iso)
    status: NodeStatus = "failed"
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("session_id", "parent_session_id", "id", "parent_id", "version", "user", "provider_name"):
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
        if not isinstance(self.permission_mode, str) or self.permission_mode not in PERMISSION_MODES:
            raise RuntimeStateValidationError("permission_mode must be approval_for_me or full_access.")
        if not isinstance(self.running_mode, str) or self.running_mode not in RUNNING_MODES:
            raise RuntimeStateValidationError("running_mode must be agent or plan.")
        object.__setattr__(self, "model", _normalize_model(self.model))
        object.__setattr__(self, "usage", _normalize_usage(self.usage))
        object.__setattr__(self, "data", validate_data(self.data))
        if not self.data and self.status != "failed":
            raise RuntimeStateValidationError("A complete runtime node must contain message or compaction data.")
        if self.first_kept_entry_id in {None, ""}:
            object.__setattr__(self, "first_kept_entry_id", self.id)
        if self.compaction_idx in {None, ""}:
            object.__setattr__(self, "compaction_idx", self.id)
        for name in ("first_kept_entry_id", "compaction_idx"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise RuntimeStateValidationError(f"{name} must be a non-empty node id.")
        if self.data_type == "root":
            if self.status != "success":
                raise RuntimeStateValidationError("Root nodes must have status='success'.")
            if self.model != _normalize_model(ROOT_MODEL):
                raise RuntimeStateValidationError("Root nodes must use the fixed neutral model snapshot.")
            if self.user or self.provider_name or self.cwd:
                raise RuntimeStateValidationError("Root nodes must not carry user, provider, or cwd state.")
            if self.permission_mode != "approval_for_me" or self.running_mode != "agent":
                raise RuntimeStateValidationError("Root nodes must use the default permission and agent modes.")
            if any(value is not None for value in self.usage.values()):
                raise RuntimeStateValidationError("Root nodes must have empty usage fields.")
            if self.firstKeptEntryId != self.id or self.compactionIdx != self.id:
                raise RuntimeStateValidationError("Root ancestry pointers must refer to the root itself.")

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
    def provider(self) -> str:
        """Compatibility accessor for internal adapters; the wire key is provider_name."""

        return self.provider_name

    @provider.setter
    def provider(self, value: str) -> None:
        self.provider_name = value

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

        if self.data_type == "root" and dict(data) != root_payload():
            raise RuntimeStateValidationError("Root nodes are immutable.")
        result = self.clone()
        result.data = validate_data(data)
        return result

    def with_status(self, status: NodeStatus) -> RuntimeState:
        if not isinstance(status, str) or status not in NODE_STATUSES:
            raise RuntimeStateValidationError(f"Unsupported node status: {status!r}.")
        if self.data_type == "root" and status != "success":
            raise RuntimeStateValidationError("Root nodes are immutable.")
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
            "provider_name": self.provider_name,
            "model": _clone(self.model),
            "permission_mode": self.permission_mode,
            "running_mode": self.running_mode,
            "usage": _clone(self.usage),
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
            "provider_name",
            "model",
            "permission_mode",
            "running_mode",
            "usage",
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
            provider_name=_string_field(raw, "provider_name"),
            model=_mapping(raw.get("model"), "model"),
            permission_mode=raw.get("permission_mode", "approval_for_me"),  # type: ignore[arg-type]
            running_mode=raw.get("running_mode", "agent"),  # type: ignore[arg-type]
            usage=_mapping(raw.get("usage"), "usage"),
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
        provider_name: str | None = None,
        provider: str | None = None,
        model: Mapping[str, Any] | None = None,
        permission_mode: PermissionMode | None = None,
        running_mode: RunningMode | None = None,
        usage: Mapping[str, Any] | None = None,
        cwd: str = "",
        data: Mapping[str, Any] | None = None,
        first_kept_entry_id: str | None = None,
        compaction_idx: str | None = None,
        id: str | None = None,
        timestamp: str | None = None,
        status: NodeStatus = "failed",
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
            # ``provider_name`` is the public, persisted identity.  Keep the
            # legacy ``provider`` keyword as a fallback for older adapters,
            # but never let it overwrite an explicitly selected name (even
            # when that explicit value is the empty placeholder identity).
            provider_name=provider_name if provider_name is not None else (provider or ""),
            model=dict(model or DEFAULT_MODEL),
            permission_mode=permission_mode or "approval_for_me",
            running_mode=running_mode or "agent",
            usage=dict(usage or {name: None for name in USAGE_FIELDS}),
            cwd=cwd,
            timestamp=timestamp or utc_iso(),
            status=status,
            data=dict(data or {}),
        )


def create_root_node(
    session_id: str,
    *,
    parent: tuple[str, str] | None = None,
    timestamp: str | None = None,
) -> RuntimeState:
    """Create the immutable session anchor used by the persisted tree."""

    return RuntimeState.create(
        session_id=session_id,
        parent=parent,
        id=session_root_id(session_id),
        model=ROOT_MODEL,
        timestamp=timestamp,
        status="success",
        data=root_payload(),
    )


def reparent_node(node: RuntimeState, parent: RuntimeState) -> RuntimeState:
    """Return a validated copy with a new parent reference."""

    raw = node.to_dict()
    raw["parent_session_id"] = parent.session_id
    raw["parent_id"] = parent.id
    return RuntimeState.from_dict(raw)


def ensure_session_root(
    nodes: Sequence[RuntimeState],
    session_id: str,
    *,
    timestamp: str | None = None,
) -> list[RuntimeState]:
    """Add a deterministic root and attach legacy local roots to it.

    ``nodes`` may include cross-session ancestors.  Only nodes owned by
    ``session_id`` are reparented; the first local root supplies the new
    root's external parent when a branch already has one.
    """

    values = list(nodes)
    root_id = session_root_id(session_id)
    existing = [node for node in values if node.session_id == session_id and node.id == root_id]
    if existing:
        if existing[0].data_type != "root":
            raise RuntimeStateValidationError(f"Reserved root id is already used by a non-root node: {root_id}.")
        if len(existing) > 1:
            raise RuntimeStateValidationError(f"Session contains duplicate root nodes: {root_id}.")
        return values

    legacy_roots = [node for node in values if node.session_id == session_id and node.data_type == "root"]
    legacy_root_keys = {node.key for node in legacy_roots}

    local_roots = [
        node
        for node in values
        if node.session_id == session_id
        and node.key not in legacy_root_keys
        and (not node.parent_id or node.parent_session_id != session_id)
    ]
    parent = None
    parent_candidates = [*legacy_roots, *local_roots]
    parent_candidate = next((node for node in parent_candidates if node.parent_id), None)
    if parent_candidate is not None:
        parent = (parent_candidate.parent_session_id, parent_candidate.parent_id)
    root = create_root_node(session_id, parent=parent, timestamp=timestamp)

    # A v4 snapshot has no formal root.  A malformed/intermediate v5
    # snapshot may contain a root whose id was generated before the session
    # id was rewritten during import.  In both cases, attach the old local
    # starting entries to the deterministic root.  Drop a stale root payload
    # itself; it carries no conversation content and must not survive as a
    # second anchor.
    direct_children_of_legacy_root = {
        node.key
        for node in values
        if node.session_id == session_id
        and node.parent_id
        and (node.parent_session_id, node.parent_id) in legacy_root_keys
    }
    local_root_keys = {node.key for node in local_roots} | direct_children_of_legacy_root
    retained = [node for node in values if node.key not in legacy_root_keys]
    return [
        root,
        *[
            reparent_node(node, root) if node.key in local_root_keys else node
            for node in retained
        ],
    ]


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
        provider_name: str | None = None,
        provider: str | None = None,
        model: Mapping[str, Any] | None = None,
        permission_mode: PermissionMode | None = None,
        running_mode: RunningMode | None = None,
        usage: Mapping[str, Any] | None = None,
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
            provider_name=(provider_name if provider_name is not None else provider)
            if (provider_name is not None or provider is not None)
            else (parent_node.provider_name if parent_node else ""),
            model=model if model is not None else (parent_node.model if parent_node else None),
            permission_mode=(
                permission_mode
                if permission_mode is not None
                else (parent_node.permission_mode if parent_node else "approval_for_me")
            ),
            running_mode=(
                running_mode if running_mode is not None else (parent_node.running_mode if parent_node else "agent")
            ),
            # Usage belongs to the node's own model turn; it is never copied
            # from a parent message.
            usage=usage,
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
            provider_name=kwargs.pop("provider_name", kwargs.pop("provider", source_node.provider_name)),
            model=kwargs.pop("model", source_node.model),
            permission_mode=kwargs.pop("permission_mode", source_node.permission_mode),
            running_mode=kwargs.pop("running_mode", source_node.running_mode),
            usage=kwargs.pop("usage", None),
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
        # ``source`` may be the in-memory dynamic clone while the tree holds
        # only its failed placeholder.  Build the ancestry from durable
        # parent links and replace that identity before creating the summary;
        # calling ``ancestors`` first would fail because the dynamic node is
        # intentionally not inserted into the durable tree.
        if isinstance(source, RuntimeState) and self.try_get(source.session_id, source.id) is None:
            path: list[RuntimeState] = []
            cursor = source.clone()
            seen: set[tuple[str, str]] = set()
            while True:
                if cursor.key in seen:
                    raise RuntimeStateValidationError("RuntimeState parent chain contains a cycle.")
                seen.add(cursor.key)
                path.append(cursor)
                if not cursor.parent_id:
                    break
                parent = self.try_get(cursor.parent_session_id, cursor.parent_id)
                if parent is None:
                    break
                cursor = parent
            path.reverse()
        else:
            path = self.ancestors(current.session_id, current.id)
            if isinstance(source, RuntimeState) and path and path[-1].key == source.key:
                path[-1] = source.clone()
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
            provider_name=kwargs.pop("provider_name", kwargs.pop("provider", current.provider_name)),
            model=kwargs.pop("model", current.model),
            permission_mode=kwargs.pop("permission_mode", current.permission_mode),
            running_mode=kwargs.pop("running_mode", current.running_mode),
            usage=kwargs.pop("usage", None),
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

        # ``source`` is normally the dynamic sidecar created by ``NodeWriter``.
        # The durable tree still contains a failed, empty placeholder with the
        # same identity until the run commits.  Build the ancestry from the
        # durable tree, then replace that one identity with the supplied
        # dynamic copy before doing any compaction/window processing.
        current = source if isinstance(source, RuntimeState) else self.get(*source)
        # The dynamic leaf is authoritative during a run.  A failed
        # persistence placeholder with the same identity is never sent to a
        # provider; callers may pass the dynamic copy directly.
        if isinstance(source, RuntimeState) and self.try_get(source.session_id, source.id) is None:
            # A caller may hold a dynamic sidecar before it has ever been
            # inserted into a transient tree.  Walk its durable parent links
            # from the tree and append the sidecar as the authoritative leaf.
            path = []
            cursor = source.clone()
            seen: set[tuple[str, str]] = set()
            while True:
                if cursor.key in seen:
                    raise RuntimeStateValidationError("RuntimeState parent chain contains a cycle.")
                seen.add(cursor.key)
                path.append(cursor)
                if not cursor.parent_id:
                    break
                parent = self.try_get(cursor.parent_session_id, cursor.parent_id)
                if parent is None:
                    break
                cursor = parent
            path.reverse()
        else:
            path = self.ancestors(current.session_id, current.id)
            if isinstance(source, RuntimeState) and path and path[-1].key == source.key:
                path[-1] = source.clone()

        # A fork can make a path cross sessions and malformed imports can
        # contain the same identity more than once.  Provider context is a
        # sequence, not a database dump: keep one entry per identity and let
        # the dynamic source win over its persisted placeholder.
        unique: dict[tuple[str, str], RuntimeState] = {}
        for item in path:
            unique[item.key] = item
        path = list(unique.values())
        # An unresolved failed placeholder has no message to send.  It is a
        # recovery marker only; never expose its empty ``data`` object to a
        # provider.  Standalone adapter calls retain their historical error
        # rendering, while this model-context boundary drops the marker.
        # The root is a durable tree anchor only.  It deliberately carries a
        # complete neutral node shape for persistence, but must never become a
        # provider message or a compaction/token input.
        path = [item for item in path if item.data and item.data.get("type") != "root"]
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
        self._last_timestamp: datetime | None = None
        self._dynamic: dict[tuple[str, str], RuntimeState] = {}
        self._lock = RLock()

    def _next_timestamp(self, parent: RuntimeState | None) -> str:
        """Return a timestamp that preserves creation order within a writer.

        Windows can return the same wall-clock value for several adjacent
        nodes.  Runtime stores use ``(timestamp, id)`` for stable projections,
        so equal values would let a random UUID reorder a parent and child.
        Keep protocol timestamps in UTC ISO 8601 while advancing ties by one
        microsecond.
        """

        raw = self.clock()
        try:
            current = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            # RuntimeState remains the single source of timestamp validation.
            return raw
        floor = self._last_timestamp
        if parent is not None:
            parent_timestamp = datetime.fromisoformat(parent.timestamp)
            floor = parent_timestamp if floor is None or parent_timestamp > floor else floor
        if floor is not None and current <= floor:
            current = floor + timedelta(microseconds=1)
        self._last_timestamp = current
        return current.isoformat()

    def create(
        self,
        *,
        session_id: str,
        parent: RuntimeState | tuple[str, str] | None = None,
        parent_session_id: str = "",
        parent_id: str = "",
        data: Mapping[str, Any] | None = None,
        user: str = "",
        provider_name: str | None = None,
        provider: str | None = None,
        model: Mapping[str, Any] | None = None,
        permission_mode: PermissionMode | None = None,
        running_mode: RunningMode | None = None,
        usage: Mapping[str, Any] | None = None,
        cwd: str = "",
        first_kept_entry_id: str | None = None,
        compaction_idx: str | None = None,
    ) -> RuntimeState:
        with self._lock:
            if isinstance(data, Mapping) and data.get("type") == "root":
                raise RuntimeStateValidationError("Root nodes must be created by the session store.")
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
                if provider_name is None:
                    provider_name = provider if provider is not None else parent_node.provider_name
                provider = None
                cwd = cwd or parent_node.cwd
            node = RuntimeState.create(
                session_id=session_id,
                parent_session_id=parent_session_id,
                parent_id=parent_id,
                id=self.id_factory(),
                timestamp=self._next_timestamp(parent_node),
                first_kept_entry_id=first_kept_entry_id,
                compaction_idx=compaction_idx,
                data=data,
                user=user,
                # The compatibility ``provider`` keyword is only a fallback;
                # a node's explicit provider_name remains authoritative.
                provider_name=provider_name if provider_name is not None else (provider or ""),
                model=model if model is not None else (parent_node.model if parent_node else None),
                permission_mode=(
                    permission_mode
                    if permission_mode is not None
                    else (parent_node.permission_mode if parent_node else "approval_for_me")
                ),
                running_mode=(
                    running_mode if running_mode is not None else (parent_node.running_mode if parent_node else "agent")
                ),
                usage=usage,
                cwd=cwd,
            )
            # Persistence receives only an empty failed placeholder.  The
            # fully populated copy is kept in the writer's dynamic sidecar
            # until the terminal delete atomically seals the leaf.
            placeholder = node.with_data({})
            # A persisted placeholder is a recovery marker, not a partial
            # provider accounting record.  It keeps the complete top-level
            # runtime configuration so a crash can be resumed with the same
            # provider/model/policy, while usage remains empty until the
            # dynamic copy is atomically finalized.
            placeholder.usage = {name: None for name in USAGE_FIELDS}
            self.store.create_node(placeholder)
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
        provider_name: str | None = None,
        model: Mapping[str, Any] | None = None,
        permission_mode: PermissionMode | None = None,
        running_mode: RunningMode | None = None,
        usage: Mapping[str, Any] | None = None,
        status: NodeStatus | None = None,
    ) -> RuntimeState:
        with self._lock:
            node = self.current(session_id, node_id)
            if self.store.list_children(session_id, node_id):
                raise RuntimeStateValidationError("Only a leaf dynamic node can be updated.")
            if data is not None:
                if isinstance(data, Mapping) and data.get("type") == "root":
                    raise RuntimeStateValidationError("Root nodes cannot be created or updated by NodeWriter.")
                node.data = validate_data(data)
            if provider_name is not None:
                if not isinstance(provider_name, str):
                    raise RuntimeStateValidationError("provider_name must be a string.")
                node.provider_name = provider_name
            if model is not None:
                # Runtime-config PATCH requests are partial.  Merge them with
                # the dynamic node snapshot before validating so changing one
                # field cannot reset the provider/model fields selected by the
                # user earlier in the run.
                node.model = _normalize_model({**node.model, **dict(model)})
            if permission_mode is not None:
                if permission_mode not in PERMISSION_MODES:
                    raise RuntimeStateValidationError("permission_mode must be approval_for_me or full_access.")
                node.permission_mode = permission_mode
            if running_mode is not None:
                if running_mode not in RUNNING_MODES:
                    raise RuntimeStateValidationError("running_mode must be agent or plan.")
                node.running_mode = running_mode
            if usage is not None:
                node.usage = _normalize_usage(usage)
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

    def update_config(
        self,
        node: RuntimeState,
        *,
        provider_name: str | None = None,
        model: Mapping[str, Any] | None = None,
        permission_mode: PermissionMode | None = None,
        running_mode: RunningMode | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> RuntimeState:
        """Apply a running configuration change to the dynamic leaf."""

        return self.update(
            node.session_id,
            node.id,
            provider_name=provider_name,
            model=model,
            permission_mode=permission_mode,
            running_mode=running_mode,
            usage=usage,
        )

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
            if node.data_type == "root":
                raise RuntimeStateValidationError("Root nodes are immutable.")
            if status == "success" and not node.data:
                raise RuntimeStateValidationError("A successful runtime node must contain typed data.")
            node.status = status
            # finalize_node performs the leaf check and replaces the static
            # placeholder in one store transaction.
            self.store.finalize_node(node)
            self._dynamic.pop(node.key, None)
            self.emit(NodeFrame("node.delete", node.clone()))
            return node.clone()

    def _finish_with_error(
        self,
        session_id: str,
        node_id: str,
        *,
        status: Literal["failed", "abort"],
        category: TerminalErrorCategory | None = None,
        code: str | None = None,
        detail: str | None = None,
    ) -> RuntimeState:
        error = terminal_error_payload(status, category, code=code, detail=detail)
        rendered = terminal_error_text(error)
        node = self.current(session_id, node_id)
        if not node.data:
            self.update_data(node, message_payload("assistant", rendered, error=error))
        elif node.data.get("type") == "message" and isinstance(node.data.get("message"), Mapping):
            message = dict(node.data["message"])
            blocks = [dict(item) for item in message.get("content", []) if isinstance(item, Mapping)]
            if not any(item.get("type") == "text" and item.get("text") == rendered for item in blocks):
                blocks.append(_text_block(rendered))
            role = str(message.pop("role", "assistant"))
            message.pop("content", None)
            message["error"] = error
            self.update_data(node, message_payload(role, blocks, **message))  # type: ignore[arg-type]
        return self.delete(session_id, node_id, status=status)

    def fail(self, session_id: str, node_id: str) -> RuntimeState:
        return self._finish_with_error(session_id, node_id, status="failed")

    def abort(
        self,
        session_id: str,
        node_id: str,
        *,
        category: TerminalErrorCategory = "user",
        code: str | None = None,
        detail: str | None = None,
    ) -> RuntimeState:
        return self._finish_with_error(
            session_id,
            node_id,
            status="abort",
            category=category,
            code=code,
            detail=detail,
        )

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
    "DEFAULT_MODEL",
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
    "ROOT_MODEL",
    "PERMISSION_MODES",
    "PermissionMode",
    "REASONING_EFFORTS",
    "ReasoningEffort",
    "RUNNING_MODES",
    "RunningMode",
    "THINKING_MODES",
    "ThinkingMode",
    "USAGE_FIELDS",
    "change_payload",
    "compaction_payload",
    "create_root_node",
    "ensure_session_root",
    "message_payload",
    "new_node_id",
    "new_session_id",
    "normalize_content",
    "parent_reference",
    "recoverable",
    "reparent_node",
    "root_payload",
    "session_root_id",
    "utc_iso",
    "validate_data",
]
