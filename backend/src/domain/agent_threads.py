"""Persistent logical Thread and Agent-tree contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ThreadOrigin = Literal["main", "fork", "subagent"]
ThreadStatus = Literal["opening", "closed"]
ContextStrategy = Literal["share", "compaction_share", "independent"]


@dataclass(frozen=True, slots=True)
class RuntimeThread:
    session_id: str
    thread_id: str
    origin_kind: ThreadOrigin
    current_turn_id: str | None
    running_turn_id: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "origin_kind": self.origin_kind,
            "current_turn_id": self.current_turn_id,
            "running_turn_id": self.running_turn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ThreadNode:
    session_id: str
    thread_id: str
    parent_thread_id: str | None
    thread_path: str
    thread_task: str
    thread_status: ThreadStatus
    depth: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not self.session_id or not self.thread_id:
            raise ValueError("ThreadNode identifiers are required.")
        if not self.thread_path.startswith("/root"):
            raise ValueError("thread_path must start with /root.")
        if self.thread_status not in {"opening", "closed"}:
            raise ValueError("thread_status must be opening or closed.")
        if isinstance(self.depth, bool) or self.depth < 0:
            raise ValueError("ThreadNode depth must be non-negative.")

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "parent_thread_id": self.parent_thread_id,
            "thread_path": self.thread_path,
            "thread_task": self.thread_task,
            "thread_status": self.thread_status,
            "depth": self.depth,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ThreadContext:
    thread_id: str
    requested_strategy: ContextStrategy
    effective_strategy: ContextStrategy
    source_turn_id: str
    source_data_idx: int
    snapshot: list[dict[str, Any]] | None = None
    summary: str | None = None

    def __post_init__(self) -> None:
        allowed = {"share", "compaction_share", "independent"}
        if self.requested_strategy not in allowed or self.effective_strategy not in allowed:
            raise ValueError("Unsupported context strategy.")
        if isinstance(self.source_data_idx, bool) or self.source_data_idx < 0:
            raise ValueError("source_data_idx must be non-negative.")

    def to_dict(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "requested_strategy": self.requested_strategy,
            "effective_strategy": self.effective_strategy,
            "source_turn_id": self.source_turn_id,
            "source_data_idx": self.source_data_idx,
            "snapshot": self.snapshot,
            "summary": self.summary,
        }


__all__ = [
    "ContextStrategy",
    "RuntimeThread",
    "ThreadContext",
    "ThreadNode",
    "ThreadOrigin",
    "ThreadStatus",
]
