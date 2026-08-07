"""Client-side reducer for canonical RuntimeState lifecycle frames."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

NodeFrameType = Literal["node.create", "node.update", "node.delete"]


@dataclass
class RuntimeNodeView:
    session_id: str
    parent_session_id: str
    id: str
    parent_id: str
    version: str
    firstKeptEntryId: str
    compactionIdx: str
    user: str
    provider: str
    cwd: str
    timestamp: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RuntimeNodeView:
        return cls(
            session_id=str(value.get("session_id", "")),
            parent_session_id=str(value.get("parent_session_id", "")),
            id=str(value.get("id", "")),
            parent_id=str(value.get("parent_id", "")),
            version=str(value.get("version", "")),
            firstKeptEntryId=str(value.get("firstKeptEntryId", "")),
            compactionIdx=str(value.get("compactionIdx", "")),
            user=str(value.get("user", "")),
            provider=str(value.get("provider", "")),
            cwd=str(value.get("cwd", "")),
            timestamp=str(value.get("timestamp", "")),
            status=str(value.get("status", "failed")),
            data=dict(value.get("data") or {}),
        )


class RuntimeNodeReducer:
    """Apply full replacement frames, never append partial stream chunks."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], RuntimeNodeView] = {}

    def apply(self, frame: dict[str, Any]) -> RuntimeNodeView | None:
        frame_type = frame.get("type")
        if frame_type not in {"node.create", "node.update", "node.delete"}:
            return None
        raw = frame.get("node")
        if not isinstance(raw, dict):
            return None
        node = RuntimeNodeView.from_dict(raw)
        self.nodes[(node.session_id, node.id)] = node
        return node

    def leaves(self, session_id: str | None = None) -> list[RuntimeNodeView]:
        values = [node for node in self.nodes.values() if session_id is None or node.session_id == session_id]
        parents = {(node.parent_session_id, node.parent_id) for node in values if node.parent_id}
        return [node for node in values if (node.session_id, node.id) not in parents]
