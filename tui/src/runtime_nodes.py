"""Client-side reducer for canonical RuntimeState lifecycle frames."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

NodeFrameType = Literal["turn.create", "turn.update"]
USAGE_FIELDS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens")


@dataclass
class RuntimeNodeView:
    thread_id: str
    parent_thread_id: str
    session_id: str
    parent_session_id: str
    id: str
    parent_id: str
    version: str
    firstKeptItemSize: int
    compactionId: str
    user: str
    provider_name: str
    model: dict[str, Any]
    permission_mode: str
    running_mode: str
    usage: dict[str, int | None]
    cwd: str
    timestamp: str
    status: str
    current_data_idx: int
    data: list[list[dict[str, Any]]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RuntimeNodeView:
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
        missing = required.difference(value)
        if missing:
            raise ValueError(f"Turn is missing required fields: {', '.join(sorted(missing))}")
        if value["version"] != "0.0.1":
            raise ValueError("Unsupported Turn version.")
        if value["permission_mode"] not in {"read_only", "workspace_write", "full_access"}:
            raise ValueError("Invalid permission_mode.")
        if value["running_mode"] not in {"agent", "plan"}:
            raise ValueError("Invalid running_mode.")
        if value["status"] not in {"running", "success", "paused", "failed"}:
            raise ValueError("Invalid Turn status.")
        model = value["model"]
        usage = value["usage"]
        data = value["data"]
        if not isinstance(model, dict) or not isinstance(usage, dict) or not isinstance(data, list) or not data:
            raise ValueError("Invalid Turn model, usage, or data.")
        if set(USAGE_FIELDS).difference(usage):
            raise ValueError("Turn usage is incomplete.")
        index = value["current_data_idx"]
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(data):
            raise ValueError("current_data_idx is out of range.")
        return cls(
            thread_id=str(value["thread_id"]),
            parent_thread_id=str(value["parent_thread_id"]),
            session_id=str(value["session_id"]),
            parent_session_id=str(value["parent_session_id"]),
            id=str(value["id"]),
            parent_id=str(value["parent_id"]),
            version=str(value["version"]),
            firstKeptItemSize=int(value["firstKeptItemSize"]),
            compactionId=str(value["compactionId"]),
            user=str(value["user"]),
            provider_name=str(value["provider_name"]),
            model=dict(model),
            permission_mode=str(value["permission_mode"]),
            running_mode=str(value["running_mode"]),
            usage=dict(usage),
            cwd=str(value["cwd"]),
            timestamp=str(value["timestamp"]),
            status=str(value["status"]),
            current_data_idx=index,
            data=list(data),
        )


class RuntimeNodeReducer:
    """Apply full replacement frames, never append partial stream chunks."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], RuntimeNodeView] = {}

    def apply(self, frame: dict[str, Any]) -> RuntimeNodeView | None:
        frame_type = frame.get("type")
        if frame_type not in {"turn.create", "turn.update"}:
            return None
        raw = frame.get("turn")
        if not isinstance(raw, dict):
            return None
        node = RuntimeNodeView.from_dict(raw)
        self.nodes[(node.session_id, node.id)] = node
        return node

    def leaves(self, session_id: str | None = None) -> list[RuntimeNodeView]:
        values = [node for node in self.nodes.values() if session_id is None or node.session_id == session_id]
        parents = {(node.parent_session_id, node.parent_id) for node in values if node.parent_id}
        return [node for node in values if (node.session_id, node.id) not in parents]
