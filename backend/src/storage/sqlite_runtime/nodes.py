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
            self._ensure_runtime_thread_record(
                connection,
                session_id=node.session_id,
                thread_id=node.thread_id,
                origin_kind="main" if node.thread_id == node.session_id else "fork",
                timestamp=node.timestamp,
            )
            self._claim_thread_turn(
                connection,
                session_id=node.session_id,
                thread_id=node.thread_id,
                turn_id=node.id,
                timestamp=node.timestamp,
            )
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            if node.thread_id == node.session_id:
                connection.execute(
                    "UPDATE thread_nodes SET thread_task=?,updated_at=? WHERE thread_id=? AND thread_task=''",
                    (str(node.user_message["content"][0].get("text") or ""), node.timestamp, node.thread_id),
                )
            self._touch_session(connection, node.session_id, node.timestamp)

    def update_node(self, node: TreeRuntimeState) -> None:
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            existing = self._json_object(connection, node.session_id, "runtime_node", node.id)
            if existing is None:
                raise KeyError(node.id)
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            self._set_thread_head(
                connection,
                session_id=node.session_id,
                thread_id=node.thread_id,
                turn_id=node.id,
                timestamp=node.timestamp,
                clear_running=node.status in {"success", "paused", "failed"},
            )
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
                self._ensure_runtime_thread_record(
                    connection,
                    session_id=node.session_id,
                    thread_id=node.thread_id,
                    origin_kind="main" if node.thread_id == node.session_id else "fork",
                    timestamp=node.timestamp,
                )
                self._put_json_object(connection, session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
                self._set_thread_head(
                    connection,
                    session_id=node.session_id,
                    thread_id=node.thread_id,
                    turn_id=node.id,
                    timestamp=node.timestamp,
                    clear_running=True,
                )
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
            self._set_thread_head(
                connection,
                session_id=node.session_id,
                thread_id=node.thread_id,
                turn_id=node.id,
                timestamp=node.timestamp,
                clear_running=True,
            )
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
            self._claim_thread_turn(
                connection,
                session_id=stored.session_id,
                thread_id=stored.thread_id,
                turn_id=stored.id,
                timestamp=utc_iso(),
            )
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
            self._claim_thread_turn(
                connection,
                session_id=node.session_id,
                thread_id=node.thread_id,
                turn_id=node.id,
                timestamp=utc_iso(),
            )
            content = node.data[node.current_data_idx][1]["content"]
            if content and content[-1].get("type") == "error" and bool(content[-1].get("retryable")):
                content.pop()
            node.status = "running"
            node = TreeRuntimeState.from_dict(node.to_dict())
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), utc_iso())
            self._touch_session(connection, node.session_id, utc_iso())
        return node

    def settle_indeterminate_tool_calls(self, turn_id: str) -> TreeRuntimeState:
        """Seal running tool calls whose side effects cannot be determined after process loss."""

        node = _require_runtime_turn(self.find_node(turn_id), turn_id)
        data_idx = node.current_data_idx
        messages = node.data[data_idx]
        terminal_results = {
            str(item.get("call_id"))
            for message in messages
            for item in message.get("content", [])
            if item.get("type") == "tool_result" and item.get("status") in {"success", "failed"} and item.get("call_id")
        }
        uncertain: list[tuple[str, str]] = []
        for message in messages:
            for item in message.get("content", []):
                if item.get("type") != "tool_call" or item.get("status") != "running":
                    continue
                call_id = str(item.get("call_id") or "")
                if not call_id or call_id in terminal_results:
                    continue
                item["status"] = "failed"
                item["replay_safe"] = False
                uncertain.append((call_id, str(item.get("name") or "unknown")))
        if not uncertain:
            return node
        if messages[-1]["role"] != "assistant":
            messages.append({"role": "assistant", "content": []})
        for call_id, tool in uncertain:
            messages[-1]["content"].append(
                {
                    "type": "tool_result",
                    "call_id": call_id,
                    "tool": tool,
                    "content": (
                        "Outcome is indeterminate because the process stopped after the tool call began. "
                        "This call will not be replayed automatically."
                    ),
                    "status": "failed",
                    "replay_safe": False,
                    "retryable": False,
                    "failure_code": "indeterminate",
                }
            )
        node = TreeRuntimeState.from_dict(node.to_dict())
        self.update_node(node)
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
