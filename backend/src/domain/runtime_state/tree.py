"""Runtime node storage protocol and ancestry/compaction operations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from threading import RLock
from typing import Any, Protocol

from .contract import (
    RuntimeStateValidationError,
    _clone,
    compaction_payload,
    message_payload,
    new_node_id,
    new_thread_id,
    utc_iso,
)
from .models import RuntimeNode, RuntimeRootState, RuntimeState, _runtime_node_sort_key


class RuntimeNodeStore(Protocol):
    def ensure_root_node(self, session_id: str, *, id: str | None = None) -> RuntimeRootState: ...
    def create_node(self, node: RuntimeState) -> None: ...
    def update_node(self, node: RuntimeState) -> None: ...
    def get_node(self, session_id: str, node_id: str) -> RuntimeNode | None: ...
    def find_node(self, node_id: str) -> RuntimeNode | None: ...
    def load_nodes(self, session_id: str) -> list[RuntimeNode]: ...


class InMemoryNodeStore:
    def __init__(self, nodes: Iterable[RuntimeNode] = ()) -> None:
        self._nodes = {node.key: node.clone() for node in nodes}
        self._validate_root_counts()
        self._lock = RLock()

    def _validate_root_counts(self) -> None:
        counts: dict[str, int] = {}
        for node in self._nodes.values():
            if isinstance(node, RuntimeRootState):
                counts[node.session_id] = counts.get(node.session_id, 0) + 1
        if any(count > 1 for count in counts.values()):
            raise RuntimeStateValidationError("A Session may contain only one root Turn.")

    def ensure_root_node(self, session_id: str, *, id: str | None = None) -> RuntimeRootState:
        with self._lock:
            roots = [
                node
                for node in self._nodes.values()
                if node.session_id == session_id and isinstance(node, RuntimeRootState)
            ]
            if len(roots) > 1:
                raise RuntimeStateValidationError("A Session may contain only one root Turn.")
            if roots:
                return roots[0].clone()
            if any(node.session_id == session_id for node in self._nodes.values()):
                raise RuntimeStateValidationError("A Session with Turns must already contain its root Turn.")
            root = RuntimeRootState.create(session_id, id=id)
            self._nodes[root.key] = root
            return root.clone()

    def create_node(self, node: RuntimeState) -> None:
        with self._lock:
            if node.key in self._nodes:
                raise ValueError(f"Turn already exists: {node.id}")
            if not node.parent_id:
                raise ValueError("A non-root Turn must have a parent Turn.")
            parent = self._nodes.get((node.parent_session_id, node.parent_id))
            if parent is None:
                raise ValueError("Turn parent does not exist.")
            if node.parent_session_id != node.session_id:
                raise ValueError("A Turn cannot continue across Sessions.")
            if node.parent_thread_id != parent.thread_id:
                raise ValueError("parent_thread_id does not match the parent Turn.")
            if any(
                isinstance(item, RuntimeState) and item.thread_id == node.thread_id and item.status == "running"
                for item in self._nodes.values()
            ):
                raise ValueError("A thread may have only one running Turn.")
            self._nodes[node.key] = node.clone()

    def update_node(self, node: RuntimeState) -> None:
        with self._lock:
            if node.key not in self._nodes:
                raise KeyError(node.id)
            self._nodes[node.key] = node.clone()

    def get_node(self, session_id: str, node_id: str) -> RuntimeNode | None:
        with self._lock:
            node = self._nodes.get((session_id, node_id))
            return node.clone() if node else None

    def find_node(self, node_id: str) -> RuntimeNode | None:
        with self._lock:
            matches = [node for node in self._nodes.values() if node.id == node_id]
            if len(matches) > 1:
                raise RuntimeStateValidationError("Turn id is not globally unique.")
            return matches[0].clone() if matches else None

    def load_nodes(self, session_id: str) -> list[RuntimeNode]:
        with self._lock:
            return sorted(
                (node.clone() for node in self._nodes.values() if node.session_id == session_id),
                key=_runtime_node_sort_key,
            )


class RuntimeStateTree:
    def __init__(self, nodes: Iterable[RuntimeNode] = ()) -> None:
        self._nodes = {node.key: node.clone() for node in nodes}
        root_counts: dict[str, int] = {}
        for node in self._nodes.values():
            if isinstance(node, RuntimeRootState):
                root_counts[node.session_id] = root_counts.get(node.session_id, 0) + 1
        if any(count > 1 for count in root_counts.values()):
            raise RuntimeStateValidationError("A Session may contain only one root Turn.")

    def get(self, session_id: str, node_id: str) -> RuntimeNode:
        try:
            return self._nodes[(session_id, node_id)].clone()
        except KeyError as exc:
            raise KeyError(f"Unknown Turn: {node_id}") from exc

    def ancestors(self, source: RuntimeNode | tuple[str, str]) -> list[RuntimeNode]:
        current = source.clone() if isinstance(source, (RuntimeState, RuntimeRootState)) else self.get(*source)
        path: list[RuntimeNode] = []
        seen: set[tuple[str, str]] = set()
        while True:
            if current.key in seen:
                raise RuntimeStateValidationError("Turn parent chain contains a cycle.")
            seen.add(current.key)
            path.append(current)
            if isinstance(current, RuntimeRootState):
                break
            if not current.parent_id:
                raise RuntimeStateValidationError("A non-root Turn must have a parent Turn.")
            try:
                parent = self.get(current.parent_session_id, current.parent_id)
            except KeyError as exc:
                raise RuntimeStateValidationError("Turn parent is missing.") from exc
            if current.parent_session_id != current.session_id:
                raise RuntimeStateValidationError("A Turn cannot continue across Sessions.")
            if current.parent_thread_id != parent.thread_id:
                raise RuntimeStateValidationError("parent_thread_id does not match the parent Turn.")
            current = parent
        path.reverse()
        return path

    @staticmethod
    def _items(turns: Sequence[RuntimeNode]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for turn in turns:
            if isinstance(turn, RuntimeRootState):
                continue
            for message in turn.selected_messages:
                result.extend(_clone(message["content"]))
        return result

    def model_input(self, source: RuntimeState | tuple[str, str]) -> list[RuntimeState]:
        path = self.ancestors(source)
        current = path[-1]
        if not isinstance(current, RuntimeState):
            raise RuntimeStateValidationError("A root Turn has no model input.")
        matches = [index for index, turn in enumerate(path) if turn.id == current.compaction_id]
        if not matches:
            raise RuntimeStateValidationError("compactionId is not an ancestor of the Turn.")
        return [turn.clone() for turn in path[matches[-1] :] if isinstance(turn, RuntimeState)]

    def compact(self, source: RuntimeState, summary: str, *, id: str | None = None) -> RuntimeState:
        path = self.ancestors(source)
        starts = [index for index, turn in enumerate(path) if turn.id == source.compaction_id]
        if not starts:
            raise RuntimeStateValidationError("compactionId is not an ancestor of the source Turn.")
        items = self._items(path[starts[-1] :])
        kept = items[-source.first_kept_item_size :] if source.first_kept_item_size else []
        data = [
            [source.user_message, message_payload("assistant", [compaction_payload(summary, kept_items=kept), *kept])]
        ]
        result = RuntimeState.create(
            session_id=source.session_id,
            thread_id=source.thread_id,
            user_content=source.user_message["content"],
            parent=source,
            id=id,
            user=source.user,
            provider_name=source.provider_name,
            model=source.model,
            permission_mode=source.permission_mode,
            running_mode=source.running_mode,
            cwd=source.cwd,
            first_kept_item_size=source.first_kept_item_size,
            data=data,
        )
        result.compaction_id = result.id
        return result

    def fork(self, source: RuntimeState, *, id: str | None = None, thread_id: str | None = None) -> RuntimeState:
        parent = self.get(source.parent_session_id, source.parent_id) if source.parent_id else None
        result = source.clone()
        result.id = id or new_node_id()
        result.thread_id = thread_id or new_thread_id()
        result.parent_thread_id = parent.thread_id if parent else ""
        result.parent_id = parent.id if parent else ""
        result.parent_session_id = parent.session_id if parent else ""
        result.timestamp = utc_iso()
        if result.compaction_id == source.id:
            result.compaction_id = result.id
        return RuntimeState.from_dict(result.to_dict())

    def all_nodes(self, session_id: str | None = None) -> list[RuntimeNode]:
        return sorted(
            (node.clone() for node in self._nodes.values() if session_id is None or node.session_id == session_id),
            key=_runtime_node_sort_key,
        )
