"""Persistence-aware writer for streaming Turn mutations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from threading import RLock
from typing import Any

from .contract import (
    ITEM_STATUSES,
    ItemStatus,
    NodeStatus,
    RuntimeStateValidationError,
    _clone,
    _json,
    new_node_id,
    normalize_content,
    terminal_error_payload,
    validate_data,
)
from .frames import _TURN_CONFIG_FIELDS, _TURN_IDENTITY_FIELDS, NodeFrame, TurnDeltaOperation
from .models import RuntimeRootState, RuntimeState
from .tree import RuntimeNodeStore


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

    def _emit_snapshot(self, node: RuntimeState, *, persist: bool = False) -> None:
        if not self._emits_frames:
            if persist:
                self.store.update_node(node)
            return
        frame = NodeFrame.snapshot(node)
        if persist:
            persist_frame = getattr(self.store, "update_node_with_frame", None)
            if callable(persist_frame):
                persist_frame(node, frame)
            else:
                self.store.update_node(node)
        self._revisions[node.key] = 0
        self.emit(frame)

    def _emit_delta(
        self,
        node: RuntimeState,
        *,
        patch: Mapping[str, Any] | None = None,
        operations: Sequence[TurnDeltaOperation] = (),
        persist: bool = False,
    ) -> None:
        if not self._emits_frames:
            if persist:
                self.store.update_node(node)
            return
        if not patch and not operations:
            if persist:
                self.store.update_node(node)
            return
        previous = self._revisions.get(node.key)
        if previous is None:
            raise RuntimeStateValidationError("A Turn delta requires a baseline snapshot.")
        revision = previous + 1
        frame = NodeFrame(
            "turn.delta",
            node.session_id,
            node.id,
            revision,
            patch={str(key): _clone(value) for key, value in (patch or {}).items()},
            operations=tuple(_clone(list(operations))),
        )
        if persist:
            persist_frame = getattr(self.store, "update_node_with_frame", None)
            if callable(persist_frame):
                persist_frame(node, frame)
            else:
                self.store.update_node(node)
        self.emit(frame)
        self._revisions[node.key] = revision

    def _store_dynamic(self, node: RuntimeState, *, persist: bool) -> RuntimeState:
        del persist
        value = RuntimeState.from_dict(node.to_dict())
        self._dynamic[value.key] = value.clone()
        return value

    def create(self, node: RuntimeState | None = None, **kwargs: Any) -> RuntimeState:
        with self._lock:
            if node is None:
                kwargs.setdefault("id", self.id_factory())
                node = RuntimeState.create(**kwargs)
            frame = NodeFrame.snapshot(node)
            persist_frame = getattr(self.store, "create_node_with_frame", None)
            if callable(persist_frame):
                persist_frame(node, frame)
            else:
                self.store.create_node(node)
            self._dynamic[node.key] = node.clone()
            if self._emits_frames:
                self._revisions[node.key] = 0
                self.emit(frame)
            return node.clone()

    def snapshot(self, node: RuntimeState) -> RuntimeState:
        """Seed an existing Turn as this stream's baseline."""

        with self._lock:
            value = RuntimeState.from_dict(node.to_dict())
            self._dynamic[value.key] = value.clone()
            self._emit_snapshot(value, persist=True)
            return value.clone()

    def current(self, session_id: str, node_id: str) -> RuntimeState:
        with self._lock:
            value = self._dynamic.get((session_id, node_id)) or self.store.get_node(session_id, node_id)
            if value is None:
                raise KeyError(node_id)
            if isinstance(value, RuntimeRootState):
                raise RuntimeStateValidationError("A root Turn cannot be updated.")
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
                if persist:
                    persist_frame = getattr(self.store, "update_node_with_frame", None)
                    if callable(persist_frame):
                        persist_frame(value, frame)
                    else:
                        self.store.update_node(value)
                self.emit(frame)
                self._revisions[value.key] = revision
            elif persist:
                self.store.update_node(value)
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
            self._emit_delta(value, patch=patch, persist=True)
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
                persist=persist,
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
            self._emit_delta(value, operations=operations, persist=persist)
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
                persist=persist,
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
                persist=persist,
            )
            return value.clone()

    def persist(self, node: RuntimeState) -> RuntimeState:
        """Persist the current dynamic Turn without publishing another delta."""

        with self._lock:
            value = self._store_dynamic(node, persist=True)
            self.store.update_node(value)
            return value.clone()

    def finalize(self, node: RuntimeState, status: NodeStatus) -> RuntimeState:
        if status == "running":
            raise RuntimeStateValidationError("A finalized Turn cannot remain running.")
        with self._lock:
            current = self.current(node.session_id, node.id)
            current.status = status
            result = self._store_dynamic(current, persist=True)
            self._emit_delta(result, patch={"status": status}, persist=True)
            self._dynamic.pop(result.key, None)
            return result.clone()

    def fail(self, session_id: str, node_id: str, message: str = "Execution failed.") -> RuntimeState:
        node = self.current(session_id, node_id)
        node = self.append_item(node, terminal_error_payload("agent", message, retryable=False))
        return self.finalize(node, "failed")


def recoverable(node: RuntimeState) -> bool:
    return node.status == "paused"
