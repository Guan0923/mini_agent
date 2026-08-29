"""Append-only Turn delta calculation and SSE frame serialization."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from .contract import ITEM_STATUSES, RuntimeStateValidationError, _clone
from .models import RuntimeState

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
        "project_cwd",
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
