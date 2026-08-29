"""Runtime state, message and node persistence using JSON objects."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace

from backend.domain import RunProvenance, RunStatus, RuntimeMessage
from backend.domain.runtime_state import (
    RuntimeState as TreeRuntimeState,
)
from backend.domain.runtime_state import (
    RuntimeStateTree,
    RuntimeStateValidationError,
    message_payload,
    new_node_id,
    new_thread_id,
    terminal_error_payload,
    utc_iso,
)
from backend.domain.sidebar_thread import SidebarThread
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState

from .codec import assistant_content, decode_runtime_state, normalize_session_title


class SQLiteRuntimeMixin:
    """Persist all runtime business values as JSON objects and events."""

    def create_node(self, node: TreeRuntimeState) -> None:
        if node.status != "running":
            raise ValueError("A Turn must be created with status='running'.")
        if node.parent_id:
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
            if any(item.status == "running" and item.thread_id == node.thread_id for item in nodes):
                raise ValueError("A thread may have only one running Turn.")
            self._auto_title_sidebar_thread(connection, node, nodes)
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            self._touch_session(connection, node.session_id, node.timestamp)
            self._append_event(connection, node.session_id, kind="turn_upserted", payload={"turn": node.to_dict()})

    def _auto_title_sidebar_thread(
        self,
        connection: sqlite3.Connection,
        node: TreeRuntimeState,
        existing_nodes: list[TreeRuntimeState],
    ) -> None:
        """Name a new main Thread from its first persisted user text."""

        if node.thread_id != node.session_id or any(item.thread_id == node.thread_id for item in existing_nodes):
            return
        payload = self._json_object(connection, node.session_id, "sidebar_thread", node.thread_id)
        if payload is None:
            return
        sidebar = SidebarThread.from_dict(payload)
        if sidebar.title_is_custom:
            return
        user_content = node.user_message.get("content", [])
        if not user_content or user_content[0].get("type") != "text":
            return
        raw_title = user_content[0].get("text")
        if not isinstance(raw_title, str) or not raw_title.strip():
            return
        updated = replace(sidebar, title=normalize_session_title(raw_title), updated_at=node.timestamp)
        self._put_json_object(
            connection,
            node.session_id,
            "sidebar_thread",
            node.thread_id,
            updated.to_dict(),
            updated.updated_at,
        )
        self._append_event(
            connection,
            node.session_id,
            kind="sidebar_thread_upserted",
            payload={"sidebar_thread": updated.to_dict()},
        )

    def update_node(self, node: TreeRuntimeState) -> None:
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            existing = self._json_object(connection, node.session_id, "runtime_node", node.id)
            if existing is None:
                raise KeyError(node.id)
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            self._touch_session(connection, node.session_id, utc_now())
            self._append_event(connection, node.session_id, kind="turn_upserted", payload={"turn": node.to_dict()})

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
                if node.parent_id:
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
                self._append_event(connection, session_id, kind="turn_upserted", payload={"turn": node.to_dict()})
            self._touch_session(connection, session_id, timestamp)

    def get_node(self, session_id: str, node_id: str) -> TreeRuntimeState | None:
        if not self.paths.session_db(session_id).exists():
            return None
        with self._connection(session_id) as connection:
            value = self._json_object(connection, session_id, "runtime_node", node_id)
        return TreeRuntimeState.from_dict(value) if value is not None else None

    def find_node(self, node_id: str) -> TreeRuntimeState | None:
        matches: list[TreeRuntimeState] = []
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
                    if value.parent_session_id == parent_session_id and value.parent_id == parent_id:
                        result.append(value)
        return sorted(result, key=lambda item: (item.timestamp, item.id))

    def load_nodes(self, session_id: str) -> list[TreeRuntimeState]:
        if not self.paths.session_db(session_id).exists():
            return []
        with self._connection(session_id) as connection:
            nodes = self._objects(connection, session_id, "runtime_node")
        return sorted(nodes, key=lambda item: (item.timestamp, item.id))

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
            self._append_event(connection, node.session_id, kind="turn_upserted", payload={"turn": node.to_dict()})

    def append_turn_version(self, turn_id: str, user_item: Mapping[str, object]) -> TreeRuntimeState:
        """Atomically rewind one Turn by appending a new selected version."""

        node = self.find_node(turn_id)
        if node is None:
            raise KeyError(turn_id)
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
                if item.thread_id == stored.thread_id and item.status == "running" and item.id != stored.id
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
            self._append_event(connection, stored.session_id, kind="turn_upserted", payload={"turn": stored.to_dict()})
        return stored

    def set_turn_current_data(self, turn_id: str, current_data_idx: int) -> TreeRuntimeState:
        node = self.find_node(turn_id)
        if node is None:
            raise KeyError(turn_id)
        if isinstance(current_data_idx, bool) or not isinstance(current_data_idx, int):
            raise RuntimeStateValidationError("current_data_idx must be an integer.")
        if not 0 <= current_data_idx < len(node.data):
            raise RuntimeStateValidationError("current_data_idx is out of range.")
        node.current_data_idx = current_data_idx
        node = TreeRuntimeState.from_dict(node.to_dict())
        self.update_node(node)
        return node

    def pause_turn(self, turn_id: str, message: str = "Paused by user.") -> TreeRuntimeState:
        node = self.find_node(turn_id)
        if node is None:
            raise KeyError(turn_id)
        if node.status != "running":
            raise ValueError("Only a running Turn can be paused.")
        node.data[node.current_data_idx][1]["content"].append(terminal_error_payload("user", message, retryable=True))
        node.status = "paused"
        node = TreeRuntimeState.from_dict(node.to_dict())
        self.finalize_node(node)
        return node

    def resume_turn_node(self, turn_id: str) -> TreeRuntimeState:
        """Re-open a paused Turn in place and continue its selected version."""

        node = self.find_node(turn_id)
        if node is None:
            raise KeyError(turn_id)
        if node.status != "paused":
            raise ValueError("Only a paused Turn can be resumed.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            if any(
                item.thread_id == node.thread_id and item.status == "running" and item.id != node.id
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
            self._append_event(connection, node.session_id, kind="turn_upserted", payload={"turn": node.to_dict()})
        return node

    def fork_turn_node(
        self, turn_id: str, *, new_turn_id: str | None = None, thread_id: str | None = None
    ) -> TreeRuntimeState:
        source = self.find_node(turn_id)
        if source is None:
            raise KeyError(turn_id)
        if source.status == "running":
            raise ValueError("A running Turn cannot be forked.")
        nodes = self.load_nodes(source.session_id)
        forked = RuntimeStateTree(nodes).fork(
            source, id=new_turn_id or new_node_id(), thread_id=thread_id or new_thread_id()
        )
        self.create_finalized_nodes([forked])
        return forked

    def create_compact_turn(self, turn_id: str, summary: str, *, new_turn_id: str | None = None) -> TreeRuntimeState:
        source = self.find_node(turn_id)
        if source is None:
            raise KeyError(turn_id)
        if source.status != "success":
            raise ValueError("Only a successful Turn can be compacted.")
        compacted = RuntimeStateTree(self.load_nodes(source.session_id)).compact(
            source, summary, id=new_turn_id or new_node_id()
        )
        self.create_node(compacted)
        return compacted

    def runtime_state_document(self, session_id: str) -> dict[str, object]:
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        if session.local_only:
            raise ValueError("Local-only sessions are excluded from cloud sync.")
        return self._build_baseline_for_runtime(session_id)

    def start_turn(
        self,
        session_id: str,
        run_id: str,
        task: str,
        provenance: RunProvenance | None = None,
        *,
        append_user_message: bool = True,
    ) -> None:
        timestamp = utc_now()
        origin = provenance or RunProvenance(workflow_id=run_id, trigger="legacy")
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            document = self._session_document(connection, session_id)
            if not bool(document.get("title_is_custom")) and not self._session_has_turn(connection):
                document["title"] = normalize_session_title(task)
            run = {
                "run_id": run_id,
                "task": task,
                "status": "running",
                "workflow_id": origin.workflow_id,
                "attempt": origin.attempt,
                "origin_kind": origin.trigger,
                "source_session_id": origin.source_session_id,
                "source_run_id": origin.source_run_id,
                "started_at": timestamp,
                "updated_at": timestamp,
            }
            self._put_json_object(connection, session_id, "run", run_id, run, timestamp)
            self._append_event(connection, session_id, kind="run_upserted", payload={"run": run})
            if append_user_message:
                self._append_turn_message(connection, session_id, run_id, "user", task, timestamp)
            self._write_session_document(connection, session_id, document)
            self._append_event(
                connection,
                session_id,
                kind="turn_started",
                payload={
                    "run_id": run_id,
                    "task": task,
                    "provenance": {
                        "workflow_id": origin.workflow_id,
                        "attempt": origin.attempt,
                        "trigger": origin.trigger,
                        "source_session_id": origin.source_session_id,
                        "source_run_id": origin.source_run_id,
                    },
                    "append_user_message": append_user_message,
                },
            )

    @staticmethod
    def _session_has_turn(connection: sqlite3.Connection) -> bool:
        """Return whether the Session already contains a real Turn."""

        return (
            connection.execute(
                "SELECT 1 FROM json_objects "
                "WHERE namespace='runtime_node' "
                "AND json_extract(payload_json,'$.data[0][0].role')='user' "
                "LIMIT 1"
            ).fetchone()
            is not None
        )

    def append_turn_input(self, session_id: str, run_id: str, content: str) -> None:
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            if self._json_object(connection, session_id, "run", run_id) is None:
                raise ValueError(f"Unknown session run: {run_id}")
            timestamp = utc_now()
            self._append_turn_message(connection, session_id, run_id, "user", content, timestamp)
            self._touch_session(connection, session_id, timestamp)
            self._append_event(
                connection,
                session_id,
                kind="turn_input_appended",
                payload={"run_id": run_id, "role": "user", "content": content, "created_at": timestamp},
            )

    def finish_turn(self, session_id: str, run_id: str, status: RunStatus, answer: str | None) -> None:
        timestamp = utc_now()
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            run = self._json_object(connection, session_id, "run", run_id)
            if run is None:
                raise ValueError(f"Unknown session run: {run_id}")
            content = assistant_content(status, answer)
            run.update({"status": str(status), "updated_at": timestamp})
            self._put_json_object(connection, session_id, "run", run_id, run, timestamp)
            self._append_event(connection, session_id, kind="run_upserted", payload={"run": run})
            self._append_turn_message(
                connection, session_id, run_id, "assistant", content, timestamp, data={"status": str(status)}
            )
            self._touch_session(connection, session_id, timestamp)
            self._append_event(
                connection,
                session_id,
                kind="turn_finished",
                payload={"run_id": run_id, "status": str(status), "content": content, "created_at": timestamp},
            )

    def save(self, runtime, reason: str) -> None:
        self._save_state(runtime.state, reason)
        if self._sync_listener is not None and not self._is_local_only(runtime.state.session_id):
            self._sync_listener()

    def save_runtime(self, state: RuntimeState) -> None:
        self._save_state(state, "runtime")

    def _save_state(self, state: RuntimeState, reason: str) -> None:
        timestamp = utc_now()
        full_payload = state.to_dict(include_runtime_messages=True)
        previous_payload: dict[str, object] = {}
        reduced_payload = state.to_dict(include_runtime_messages=False)
        # Messages and runtime messages have independent append-only objects;
        # the state event is deliberately not an ever-growing transcript.
        reduced_payload.pop("messages", None)
        reduced_payload.pop("run_history", None)
        if reduced_payload.get("current_run"):
            for key in ("history", "actions", "events", "runtime_messages", "subagent_batches"):
                reduced_payload["current_run"].pop(key, None)
        with self._connection(state.session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, state.session_id)
            previous_payload = self._json_object(connection, state.session_id, "runtime_state", state.session_id) or {}
            self._put_json_object(
                connection, state.session_id, "runtime_state", state.session_id, full_payload, timestamp
            )
            run = state.current_run
            if run is not None:
                run_payload = run.to_dict(include_runtime_messages=False)
                run_payload.update(
                    {"run_id": run.run_id, "task": run.task, "status": run.status, "updated_at": timestamp}
                )
                self._put_json_object(connection, state.session_id, "run", run.run_id, run_payload, timestamp)
                previous_run = previous_payload.get("current_run")
                previous_messages = (
                    previous_run.get("runtime_messages", [])
                    if isinstance(previous_run, dict) and isinstance(previous_run.get("runtime_messages"), list)
                    else []
                )
                previous_sequences = {
                    int(item.get("sequence", 0)) for item in previous_messages if isinstance(item, dict)
                }
                for message in run.runtime_messages:
                    if message.sequence in previous_sequences:
                        continue
                    self._put_runtime_message(connection, state.session_id, run.run_id, message)
            self._touch_session(connection, state.session_id, timestamp)
            self._append_event(
                connection,
                state.session_id,
                kind="runtime_state_saved",
                payload={"reason": reason, "state": reduced_payload, "run_id": run.run_id if run else None},
            )
            delta = self._runtime_state_delta(previous_payload, full_payload, run.run_id if run else None)
            if delta:
                self._append_event(connection, state.session_id, kind="runtime_state_delta", payload=delta)
            if run is not None:
                checkpoint = {"run_id": run.run_id, "reason": reason, "state": reduced_payload, "created_at": timestamp}
                self._put_json_object(
                    connection,
                    state.session_id,
                    "checkpoint",
                    f"{run.run_id}:{timestamp}:{reason}",
                    checkpoint,
                    timestamp,
                )
                self._append_event(
                    connection,
                    state.session_id,
                    kind="checkpoint_recorded",
                    payload=checkpoint,
                )

    @staticmethod
    def _runtime_state_delta(
        previous: dict[str, object], current: dict[str, object], run_id: str | None
    ) -> dict[str, object] | None:
        """Return only newly appended runtime history for cloud replay."""

        delta: dict[str, object] = {"run_id": run_id}
        old_history = previous.get("run_history") if isinstance(previous.get("run_history"), list) else []
        new_history = current.get("run_history") if isinstance(current.get("run_history"), list) else []
        if len(new_history) > len(old_history):
            delta["run_history_append"] = new_history[len(old_history) :]
        old_run = previous.get("current_run") if isinstance(previous.get("current_run"), dict) else {}
        new_run = current.get("current_run") if isinstance(current.get("current_run"), dict) else {}
        for field in ("history", "actions", "events"):
            old_values = old_run.get(field) if isinstance(old_run.get(field), list) else []
            new_values = new_run.get(field) if isinstance(new_run.get(field), list) else []
            common = 0
            while common < len(old_values) and common < len(new_values) and old_values[common] == new_values[common]:
                common += 1
            if common < len(new_values):
                delta[f"{field}_from"] = common
                delta[f"{field}_values"] = new_values[common:]
        old_batches = old_run.get("subagent_batches") if isinstance(old_run.get("subagent_batches"), dict) else {}
        new_batches = new_run.get("subagent_batches") if isinstance(new_run.get("subagent_batches"), dict) else {}
        changed_batches = {str(key): value for key, value in new_batches.items() if old_batches.get(key) != value}
        if changed_batches:
            delta["subagent_batches_upsert"] = changed_batches
        return delta if len(delta) > 1 else None

    def load_runtime(self, session_id: str) -> RuntimeState | None:
        with self._connection(session_id) as connection:
            payload = self._json_object(connection, session_id, "runtime_state", session_id)
        if payload is None:
            return None
        state = decode_runtime_state(json.dumps(payload, ensure_ascii=False))
        if state.current_run is not None:
            state.current_run.runtime_messages = self.load_runtime_messages(session_id, state.current_run.run_id)
        return state

    def append_runtime_message(self, session_id: str, run_id: str, message: RuntimeMessage) -> None:
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            if self._json_object(connection, session_id, "run", run_id) is None:
                raise ValueError(f"Unknown session run: {run_id}")
            self._put_runtime_message(connection, session_id, run_id, message)
            self._touch_session(connection, session_id, message.timestamp)

    def load_runtime_messages(self, session_id: str, run_id: str | None = None) -> list[RuntimeMessage]:
        with self._connection(session_id) as connection:
            values = [
                value
                for value in self._json_values(connection, session_id, "runtime_message")
                if run_id is None or str(value.get("run_id") or "") == run_id
            ]
        values.sort(
            key=lambda item: (
                str(item.get("run_id") or ""),
                int(item.get("sequence", 0)),
                str(item.get("created_at") or ""),
            )
        )
        return [
            RuntimeMessage(
                int(item.get("sequence", 0)),
                str(item.get("kind") or ""),
                str(item.get("message") or ""),
                str(item.get("created_at") or utc_now()),
                dict(item.get("data") or {}),
            )
            for item in values
        ]

    def resume_runtime(self, source: RuntimeState, resumed: RuntimeState) -> None:
        self._save_state(source, f"run_{source.current_run.status}" if source.current_run else "resume")
        self._save_state(resumed, "run_resumed")

    def _append_turn_message(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        run_id: str,
        role: str,
        content: str,
        timestamp: str,
        *,
        data: dict[str, object] | None = None,
    ) -> None:
        existing = self._json_values(connection, session_id, "turn_message")
        sequence = 1 + max(
            (int(item.get("sequence", 0)) for item in existing if str(item.get("run_id") or "") == run_id), default=0
        )
        payload = {
            "run_id": run_id,
            "sequence": sequence,
            "role": role,
            "content": content,
            "data": data or {},
            "created_at": timestamp,
        }
        self._put_json_object(connection, session_id, "turn_message", f"{run_id}:{sequence}", payload, timestamp)
        self._append_event(
            connection,
            session_id,
            kind="runtime_message_appended",
            payload={
                "run_id": run_id,
                "sequence": sequence,
                "kind": role,
                "message": content,
                "data": data or {},
                "created_at": timestamp,
            },
        )

    def _put_runtime_message(
        self, connection: sqlite3.Connection, session_id: str, run_id: str, message: RuntimeMessage
    ) -> None:
        payload = {
            "run_id": run_id,
            "sequence": message.sequence,
            "kind": message.kind,
            "message": message.message,
            "data": message.data,
            "created_at": message.timestamp,
        }
        object_id = f"{run_id}:{message.sequence}"
        existing = self._json_object(connection, session_id, "runtime_message", object_id)
        if existing is not None:
            if existing == payload:
                return
            raise ValueError("Runtime messages are immutable and cannot be replaced.")
        self._put_json_object(connection, session_id, "runtime_message", object_id, payload, message.timestamp)
        self._append_event(connection, session_id, kind="runtime_message_appended", payload=payload)

    def _touch_session(self, connection: sqlite3.Connection, session_id: str, timestamp: str) -> None:
        document = self._session_document(connection, session_id)
        document["updated_at"] = timestamp
        self._write_session_document(connection, session_id, document)

    @staticmethod
    def _objects(connection: sqlite3.Connection, session_id: str, namespace: str) -> list[TreeRuntimeState]:
        values = SQLiteRuntimeMixin._json_values(connection, session_id, namespace)
        return [TreeRuntimeState.from_dict(value) for value in values]

    @staticmethod
    def _json_values(connection: sqlite3.Connection, session_id: str, namespace: str) -> list[dict[str, object]]:
        rows = connection.execute(
            "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace=?", (session_id, namespace)
        ).fetchall()
        return [dict(value) for row in rows if isinstance(value := json.loads(str(row[0])), dict)]

    @staticmethod
    def _put_json_object(
        connection: sqlite3.Connection,
        session_id: str,
        namespace: str,
        object_id: str,
        payload: dict[str, object],
        updated_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (
                session_id,
                namespace,
                object_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                updated_at,
            ),
        )

    @staticmethod
    def _json_object(
        connection: sqlite3.Connection, session_id: str, namespace: str, object_id: str
    ) -> dict[str, object] | None:
        row = connection.execute(
            "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace=? AND object_id=?",
            (session_id, namespace, object_id),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row[0]))
        return dict(value) if isinstance(value, dict) else None

    @staticmethod
    def _assert_writable(connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT payload_json FROM json_objects WHERE namespace='session' LIMIT 1").fetchone()
        if row is not None and bool(json.loads(str(row[0])).get("read_only", False)):
            raise PermissionError("Remote sessions are read-only; fork the session before writing.")


__all__ = ["SQLiteRuntimeMixin"]
