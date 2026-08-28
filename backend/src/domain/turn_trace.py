"""Turn-version audit context and completed Item snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TurnTraceContext:
    """The immutable context captured immediately before the first decision."""

    system_message: str
    active_skills: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    initialized_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_message": self.system_message,
            "active_skills": self.active_skills,
            "tools": self.tools,
            "initialized_at": self.initialized_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TurnTraceContext:
        return cls(
            system_message=str(value.get("system_message") or ""),
            active_skills=[dict(item) for item in value.get("active_skills", []) if isinstance(item, Mapping)],
            tools=[dict(item) for item in value.get("tools", []) if isinstance(item, Mapping)],
            initialized_at=str(value.get("initialized_at") or ""),
        )


@dataclass(frozen=True)
class TurnTraceItem:
    """One terminal canonical Turn Item, addressed by its stable coordinates."""

    sequence: int
    message_idx: int
    item_idx: int
    role: str
    item: dict[str, Any]
    completed_at: str

    @property
    def coordinate(self) -> tuple[int, int]:
        return self.message_idx, self.item_idx

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "message_idx": self.message_idx,
            "item_idx": self.item_idx,
            "role": self.role,
            "item": self.item,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TurnTraceItem:
        item = value.get("item")
        return cls(
            sequence=int(value["sequence"]),
            message_idx=int(value["message_idx"]),
            item_idx=int(value["item_idx"]),
            role=str(value.get("role") or "assistant"),
            item=dict(item) if isinstance(item, Mapping) else {},
            completed_at=str(value.get("completed_at") or ""),
        )


@dataclass(frozen=True)
class TurnTrace:
    """The single audit record for one ``turn_id + data_idx`` pair."""

    turn_id: str
    thread_id: str
    data_idx: int
    context: TurnTraceContext
    items: list[TurnTraceItem]
    last_sequence: int
    updated_at: str

    @property
    def object_id(self) -> str:
        return f"{self.turn_id}:{self.data_idx}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "turn_id": self.turn_id,
            "thread_id": self.thread_id,
            "data_idx": self.data_idx,
            "context": self.context.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "last_sequence": self.last_sequence,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TurnTrace:
        if value.get("schema_version") != 2:
            raise ValueError("Unsupported Turn Trace schema.")
        context = value.get("context")
        if not isinstance(context, Mapping):
            raise ValueError("Turn Trace context is missing.")
        items = [TurnTraceItem.from_dict(item) for item in value.get("items", []) if isinstance(item, Mapping)]
        return cls(
            turn_id=str(value["turn_id"]),
            thread_id=str(value["thread_id"]),
            data_idx=int(value["data_idx"]),
            context=TurnTraceContext.from_dict(context),
            items=items,
            last_sequence=int(value.get("last_sequence") or 0),
            updated_at=str(value.get("updated_at") or ""),
        )


__all__ = ["TurnTrace", "TurnTraceContext", "TurnTraceItem"]
