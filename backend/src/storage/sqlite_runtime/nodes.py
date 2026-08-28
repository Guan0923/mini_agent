"""Canonical Turn-node persistence and tree mutations."""

from __future__ import annotations

from collections.abc import Mapping

from backend.domain.runtime_state import (
    RuntimeNode,
    RuntimeRootState,
    RuntimeStateTree,
    RuntimeStateValidationError,
    message_payload,
    new_node_id,
    new_thread_id,
    runtime_node_from_dict,
    utc_iso,
)
from backend.domain.runtime_state import RuntimeState as TreeRuntimeState
from backend.domain.state import utc_now


def _require_runtime_turn(node: RuntimeNode | None, turn_id: str) -> TreeRuntimeState:
    if node is None:
        raise KeyError(turn_id)
    if isinstance(node, RuntimeRootState):
        raise ValueError("A root Turn is only an ancestry anchor.")
    return node


class SQLiteNodeMixin:
    def ensure_root_node(self, session_id: str, *, id: str | None = None) -> RuntimeRootState:
        """Persist and return the sole synthetic root for an otherwise empty Session."""

        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, session_id)
            nodes = self._objects(connection, session_id, "runtime_node")
            roots = [node for node in nodes if isinstance(node, RuntimeRootState)]
            if len(roots) > 1:
                raise RuntimeStateValidationError("A Session may contain only one root Turn.")
            if roots:
                return roots[0]
            if nodes:
                raise RuntimeStateValidationError("A Session with Turns must already contain its root Turn.")
            root = RuntimeRootState.create(session_id, id=id)
            timestamp = utc_iso()
            self._put_json_object(connection, session_id, "runtime_node", root.id, root.to_dict(), timestamp)
            self._touch_session(connection, session_id, timestamp)
            return root

    def create_node(self, node: TreeRuntimeState) -> None:
        if node.status != "running":
            raise ValueError("A Turn must be created with status='running'.")
        if not node.parent_id:
            raise ValueError("A non-root Turn must have a parent Turn.")
        parent = self.get_node(node.parent_session_id, node.parent_id)
        if parent is None:
            raise ValueError("A runtime node parent must be present in the store.")
        if node.parent_session_id != node.session_id:
            raise ValueError("A Turn cannot continue across Sessions.")
        if node.parent_thread_id != parent.thread_id:
            raise ValueError("parent_thread_id does not match the parent Turn.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, node.session_id)
            nodes = self._objects(connection, node.session_id, "runtime_node")
            if any(
                isinstance(item, TreeRuntimeState) and item.status == "running" and item.thread_id == node.thread_id
                for item in nodes
            ):
                raise ValueError("A thread may have only one running Turn.")
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            self._touch_session(connection, node.session_id, node.timestamp)

    def update_node(self, node: TreeRuntimeState) -> None:
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            existing = self._json_object(connection, node.session_id, "runtime_node", node.id)
            if existing is None:
                raise KeyError(node.id)
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            self._touch_session(connection, node.session_id, utc_now())

    def create_finalized_nodes(self, nodes: list[TreeRuntimeState] | tuple[TreeRuntimeState, ...]) -> None:
        """Atomically append an ordered batch of terminal canonical nodes."""

        if not nodes:
            return
        session_id = nodes[0].session_id
        if any(node.session_id != session_id for node in nodes):
            raise ValueError("A finalized node batch must belong to one session.")
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, session_id)
            existing = self._objects(connection, session_id, "runtime_node")
            by_key = {(node.session_id, node.id): node for node in existing}
            staged = dict(by_key)
            for node in nodes:
                if node.status not in {"success", "paused", "failed"}:
                    raise ValueError("A finalized node batch must contain terminal nodes.")
                if node.key in staged:
                    raise ValueError(f"Runtime node already exists: {node.session_id}/{node.id}")
                if not node.parent_id:
                    raise ValueError("A non-root Turn must have a parent Turn.")
                parent = staged.get((node.parent_session_id, node.parent_id))
                if parent is None:
                    raise ValueError("A finalized node parent must be present in the store.")
                if node.parent_session_id != session_id:
                    raise ValueError("A Turn cannot continue across Sessions.")
                if node.parent_thread_id != parent.thread_id:
                    raise ValueError("parent_thread_id does not match the parent Turn.")
                staged[node.key] = node
            timestamp = nodes[-1].timestamp
            for node in nodes:
                self._put_json_object(connection, session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            self._touch_session(connection, session_id, timestamp)

    def get_node(self, session_id: str, node_id: str) -> RuntimeNode | None:
        if not self.paths.session_db(session_id).exists():
            return None
        with self._connection(session_id) as connection:
            value = self._json_object(connection, session_id, "runtime_node", node_id)
        return runtime_node_from_dict(value) if value is not None else None

    def find_node(self, node_id: str) -> RuntimeNode | None:
        matches: list[RuntimeNode] = []
        for summary in self.list_sessions(state="all"):
            node = self.get_node(summary.session_id, node_id)
            if node is not None:
                matches.append(node)
        if len(matches) > 1:
            raise ValueError("Turn id is not globally unique.")
        return matches[0] if matches else None

    def list_children(self, parent_session_id: str, parent_id: str) -> list[TreeRuntimeState]:
        result: list[TreeRuntimeState] = []
        for summary in self.list_sessions(state="all"):
            with self._connection(summary.session_id) as connection:
                for value in self._objects(connection, summary.session_id, "runtime_node"):
                    if isinstance(value, RuntimeRootState):
                        continue
                    if value.parent_session_id == parent_session_id and value.parent_id == parent_id:
                        result.append(value)
        return sorted(result, key=lambda item: (item.timestamp, item.id))

    def load_nodes(self, session_id: str) -> list[RuntimeNode]:
        if not self.paths.session_db(session_id).exists():
            return []
        with self._connection(session_id) as connection:
            nodes = self._objects(connection, session_id, "runtime_node")
        return sorted(
            nodes,
            key=lambda item: (0, "", item.id) if isinstance(item, RuntimeRootState) else (1, item.timestamp, item.id),
        )

    def finalize_node(self, node: TreeRuntimeState) -> None:
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            existing = self._json_object(connection, node.session_id, "runtime_node", node.id)
            if existing is None:
                raise ValueError(f"Unknown runtime node: {node.session_id}/{node.id}")
            if str(existing.get("status")) != "running":
                raise ValueError("Sealed runtime nodes are read-only.")
            if node.status not in {"success", "paused", "failed"}:
                raise ValueError("A Turn can only be finalized as success, paused, or failed.")
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            self._touch_session(connection, node.session_id, node.timestamp)

    def append_turn_version(self, turn_id: str, user_item: Mapping[str, object]) -> TreeRuntimeState:
        """Atomically rewind one Turn by appending a new selected version."""

        node = _require_runtime_turn(self.find_node(turn_id), turn_id)
        if node.status == "running":
            raise ValueError("A running Turn cannot be rewound.")
        user = message_payload("user", [dict(user_item)])
        assistant = message_payload("assistant", [])
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            current = self._json_object(connection, node.session_id, "runtime_node", node.id)
            if current is None:
                raise KeyError(turn_id)
            stored = TreeRuntimeState.from_dict(current)
            running = [
                item
                for item in self._objects(connection, node.session_id, "runtime_node")
                if isinstance(item, TreeRuntimeState)
                and item.thread_id == stored.thread_id
                and item.status == "running"
                and item.id != stored.id
            ]
            if running:
                raise ValueError("A thread may have only one running Turn.")
            stored.data.append([user, assistant])
            stored.current_data_idx = len(stored.data) - 1
            stored.status = "running"
            stored.timestamp = utc_iso()
            stored = TreeRuntimeState.from_dict(stored.to_dict())
            self._put_json_object(
                connection, stored.session_id, "runtime_node", stored.id, stored.to_dict(), stored.timestamp
            )
            self._touch_session(connection, stored.session_id, stored.timestamp)
        return stored

    def set_turn_current_data(self, turn_id: str, current_data_idx: int) -> TreeRuntimeState:
        node = _require_runtime_turn(self.find_node(turn_id), turn_id)
        if isinstance(current_data_idx, bool) or not isinstance(current_data_idx, int):
            raise RuntimeStateValidationError("current_data_idx must be an integer.")
        if not 0 <= current_data_idx < len(node.data):
            raise RuntimeStateValidationError("current_data_idx is out of range.")
        node.current_data_idx = current_data_idx
        node = TreeRuntimeState.from_dict(node.to_dict())
        self.update_node(node)
        return node

    def pause_turn(self, turn_id: str, message: str = "Paused by user.") -> TreeRuntimeState:
        node = _require_runtime_turn(self.find_node(turn_id), turn_id)
        if node.status != "running":
            raise ValueError("Only a running Turn can be paused.")
        del message
        for version in node.data:
            for turn_message in version:
                for item in turn_message["content"]:
                    if item.get("status") == "running":
                        item["status"] = "failed"
        node.status = "paused"
        node = TreeRuntimeState.from_dict(node.to_dict())
        self.finalize_node(node)
        return node

    def resume_turn_node(self, turn_id: str) -> TreeRuntimeState:
        """Re-open a paused Turn in place and continue its selected version."""

        node = _require_runtime_turn(self.find_node(turn_id), turn_id)
        if node.status != "paused":
            raise ValueError("Only a paused Turn can be resumed.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            if any(
                isinstance(item, TreeRuntimeState)
                and item.thread_id == node.thread_id
                and item.status == "running"
                and item.id != node.id
                for item in self._objects(connection, node.session_id, "runtime_node")
            ):
                raise ValueError("A thread may have only one running Turn.")
            content = node.data[node.current_data_idx][1]["content"]
            if content and content[-1].get("type") == "error" and bool(content[-1].get("retryable")):
                content.pop()
            node.status = "running"
            node = TreeRuntimeState.from_dict(node.to_dict())
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), utc_iso())
            self._touch_session(connection, node.session_id, utc_iso())
        return node

    def fork_turn_node(
        self, turn_id: str, *, new_turn_id: str | None = None, thread_id: str | None = None
    ) -> TreeRuntimeState:
        source = _require_runtime_turn(self.find_node(turn_id), turn_id)
        if source.status == "running":
            raise ValueError("A running Turn cannot be forked.")
        nodes = self.load_nodes(source.session_id)
        forked = RuntimeStateTree(nodes).fork(
            source, id=new_turn_id or new_node_id(), thread_id=thread_id or new_thread_id()
        )
        self.create_finalized_nodes([forked])
        return forked

    def create_compact_turn(self, turn_id: str, summary: str, *, new_turn_id: str | None = None) -> TreeRuntimeState:
        source = _require_runtime_turn(self.find_node(turn_id), turn_id)
        if source.status != "success":
            raise ValueError("Only a successful Turn can be compacted.")
        compacted = RuntimeStateTree(self.load_nodes(source.session_id)).compact(
            source, summary, id=new_turn_id or new_node_id()
        )
        self.create_node(compacted)
        return compacted
