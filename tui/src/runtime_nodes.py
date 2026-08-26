"""Legacy TUI reducer for Turn baseline snapshots and incremental deltas."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

NodeFrameType = Literal["turn.snapshot", "turn.delta"]
USAGE_FIELDS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
PATCH_FIELDS = {
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
}


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
            data=deepcopy(data),
        )


class RuntimeNodeReducer:
    """Apply one baseline per Turn followed by consecutive append-only deltas."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], RuntimeNodeView] = {}
        self.revisions: dict[tuple[str, str], int] = {}

    def apply(self, frame: dict[str, Any]) -> RuntimeNodeView | None:
        frame_type = frame.get("type")
        if frame_type not in {"turn.snapshot", "turn.delta"}:
            return None
        if frame_type == "turn.snapshot":
            raw = frame.get("turn")
            if not isinstance(raw, dict):
                return None
            if frame.get("revision") != 0:
                raise ValueError("Turn snapshot revision must be zero.")
            node = RuntimeNodeView.from_dict(raw)
            key = (node.session_id, node.id)
            if key in self.revisions:
                raise ValueError("Turn received more than one baseline snapshot.")
            self.nodes[key] = node
            self.revisions[key] = 0
            return node

        session_id, turn_id = frame.get("session_id"), frame.get("turn_id")
        if not isinstance(session_id, str) or not isinstance(turn_id, str):
            raise ValueError("Turn delta identity is invalid.")
        key = (session_id, turn_id)
        previous = self.nodes.get(key)
        revision = frame.get("revision")
        if previous is None:
            raise ValueError("Turn delta arrived before its baseline snapshot.")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision != self.revisions[key] + 1:
            raise ValueError("Turn delta revision is not consecutive.")

        payload = asdict(previous)
        patch = frame.get("patch", {})
        if not isinstance(patch, dict) or not set(patch).issubset(PATCH_FIELDS):
            raise ValueError("Turn delta patch is invalid.")
        payload.update(deepcopy(patch))
        data = payload["data"]
        operations = frame.get("operations", [])
        if not isinstance(operations, list):
            raise ValueError("Turn delta operations are invalid.")
        for operation in operations:
            if not isinstance(operation, dict):
                raise ValueError("Turn delta operation is invalid.")
            data_idx, item_idx = operation.get("data_idx"), operation.get("item_idx")
            if (
                isinstance(data_idx, bool)
                or not isinstance(data_idx, int)
                or data_idx < 0
                or isinstance(item_idx, bool)
                or not isinstance(item_idx, int)
                or item_idx < 0
            ):
                raise ValueError("Turn delta indexes are invalid.")
            try:
                items = data[data_idx][1]["content"]
            except (IndexError, KeyError, TypeError) as exc:
                raise ValueError("Turn delta target is invalid.") from exc
            if operation.get("op") == "append_item":
                if item_idx != len(items) or not isinstance(operation.get("item"), dict):
                    raise ValueError("Turn item delta is out of order.")
                items.append(deepcopy(operation["item"]))
            elif operation.get("op") == "append_text":
                delta = operation.get("delta")
                if not isinstance(delta, str) or not delta:
                    raise ValueError("Turn text delta is invalid.")
                try:
                    item = items[item_idx]
                except IndexError as exc:
                    raise ValueError("Turn text delta target is out of range.") from exc
                if item.get("type") not in {"text", "reasoning"} or not isinstance(item.get("text"), str):
                    raise ValueError("Turn text delta target is invalid.")
                item["text"] += delta
            else:
                raise ValueError("Unsupported Turn delta operation.")

        node = RuntimeNodeView.from_dict(payload)
        self.nodes[key] = node
        self.revisions[key] = revision
        return node

    def leaves(self, session_id: str | None = None) -> list[RuntimeNodeView]:
        values = [node for node in self.nodes.values() if session_id is None or node.session_id == session_id]
        parents = {(node.parent_session_id, node.parent_id) for node in values if node.parent_id}
        return [node for node in values if (node.session_id, node.id) not in parents]
