"""Runtime state, message and node persistence using JSON objects."""

from __future__ import annotations

import json
import sqlite3

from backend.domain import RunProvenance, RunStatus, RuntimeMessage
from backend.domain.runtime_state import RuntimeState as TreeRuntimeState
from backend.domain.runtime_state import session_root_id
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState

from .codec import assistant_content, decode_runtime_state, normalize_session_title


class SQLiteRuntimeMixin:
    """Persist all runtime business values as JSON objects and events."""

    def create_node(self, node: TreeRuntimeState) -> None:
        if node.data_type == "root":
            raise ValueError("Root nodes are created only with session metadata.")
        if node.status != "running":
            raise ValueError("A runtime node must be created with status='running'.")
        if node.parent_id and self.get_node(node.parent_session_id, node.parent_id) is None:
            raise ValueError("A runtime node parent must be present in the store.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, node.session_id)
            nodes = self._objects(connection, node.session_id, "runtime_node")
            if node.parent_id:
                parent = next(
                    (item for item in nodes if item.id == node.parent_id and item.session_id == node.parent_session_id),
                    None,
                )
                if parent is not None and parent.status == "running":
                    raise ValueError("A running node cannot have a running child.")
            if any(
                item.status == "running"
                and not any(
                    child.parent_session_id == item.session_id and child.parent_id == item.id for child in nodes
                )
                for item in nodes
            ):
                raise ValueError("A session may have only one running leaf.")
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            self._touch_session(connection, node.session_id, node.timestamp)
            self._append_event(connection, node.session_id, kind="node_upserted", payload={"node": node.to_dict()})

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
                if node.status not in {"success", "cancel", "abort"}:
                    raise ValueError("A finalized node batch must contain terminal nodes.")
                if node.key in staged:
                    raise ValueError(f"Runtime node already exists: {node.session_id}/{node.id}")
                if node.parent_id and (node.parent_session_id, node.parent_id) not in staged:
                    # Branch sessions may continue from an ancestor stored in
                    # another session database.  The local transaction cannot
                    # stage that external row, but it must still exist before
                    # the child batch is accepted.
                    if node.parent_session_id == session_id or self.get_node(node.parent_session_id, node.parent_id) is None:
                        raise ValueError("A finalized node parent must be present in the store.")
                staged[node.key] = node
            timestamp = nodes[-1].timestamp
            for node in nodes:
                self._put_json_object(connection, session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
                self._append_event(connection, session_id, kind="node_finalized", payload={"node": node.to_dict()})
            self._touch_session(connection, session_id, timestamp)

    def get_node(self, session_id: str, node_id: str) -> TreeRuntimeState | None:
        if not self.paths.session_db(session_id).exists():
            return None
        with self._connection(session_id) as connection:
            value = self._json_object(connection, session_id, "runtime_node", node_id)
        return TreeRuntimeState.from_dict(value) if value is not None else None

    def get_session_root(self, session_id: str) -> TreeRuntimeState | None:
        return self.get_node(session_id, session_root_id(session_id))

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
        result = {node.key: node for node in nodes}
        pending = list(result.values())
        while pending:
            node = pending.pop()
            if not node.parent_id:
                continue
            key = (node.parent_session_id, node.parent_id)
            if key in result or not self.paths.session_db(node.parent_session_id).exists():
                continue
            parent = self.get_node(*key)
            if parent is not None:
                result[key] = parent
                pending.append(parent)
        return sorted(result.values(), key=lambda item: (item.timestamp, item.id))

    def finalize_node(self, node: TreeRuntimeState) -> None:
        if node.data_type == "root":
            raise ValueError("Root nodes are immutable.")
        if self.list_children(node.session_id, node.id):
            raise ValueError("Only a leaf runtime node can be finalized.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            existing = self._json_object(connection, node.session_id, "runtime_node", node.id)
            if existing is None:
                raise ValueError(f"Unknown runtime node: {node.session_id}/{node.id}")
            if str(existing.get("status")) != "running":
                raise ValueError("Sealed runtime nodes are read-only.")
            if node.status not in {"success", "cancel", "abort"}:
                raise ValueError("A runtime node can only be finalized as success, cancel, or abort.")
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)
            self._touch_session(connection, node.session_id, node.timestamp)
            self._append_event(connection, node.session_id, kind="node_finalized", payload={"node": node.to_dict()})

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
            if not bool(document.get("title_is_custom")) and not self._chain_has_user_message(connection, session_id):
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

    def _chain_has_user_message(self, connection: sqlite3.Connection, session_id: str) -> bool:
        visited: set[str] = set()
        current = session_id
        while current and current not in visited:
            visited.add(current)
            if current == session_id:
                found, parent = self._chain_query(connection, current)
            elif self.paths.session_db(current).exists():
                with self._connection(current) as ancestor:
                    found, parent = self._chain_query(ancestor, current)
            else:
                return False
            if found:
                return True
            current = parent or ""
        return False

    @staticmethod
    def _chain_query(connection: sqlite3.Connection, session_id: str) -> tuple[bool, str | None]:
        found = connection.execute(
            "SELECT 1 FROM json_objects WHERE namespace='runtime_node' AND json_extract(payload_json,'$.data.type')='message' AND json_extract(payload_json,'$.data.message.role')='user' LIMIT 1"
        ).fetchone()
        if found is not None:
            return True, None
        root = connection.execute(
            "SELECT payload_json FROM json_objects WHERE namespace='runtime_node' AND object_id=?",
            (session_root_id(session_id),),
        ).fetchone()
        if root is None:
            return False, None
        payload = json.loads(str(root[0]))
        return False, str(payload.get("parent_session_id") or "") or None

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
