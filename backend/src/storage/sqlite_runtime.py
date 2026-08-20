"""Runtime turns, checkpoints, and message persistence for SQLite."""

from __future__ import annotations

import json
import sqlite3

from backend.domain import RunProvenance, RunStatus, RuntimeMessage, Session
from backend.domain.runtime_state import RuntimeState as TreeRuntimeState
from backend.domain.runtime_state import session_root_id
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState

from .codec import (
    assistant_content,
    decode_message_data,
    decode_runtime_state,
    encode_message_data,
    encode_runtime_state,
    normalize_session_title,
)
from .sqlite_schema import SCHEMA_VERSION


class SQLiteRuntimeMixin:
    """Legacy turn persistence plus the canonical ``runtime_nodes`` store."""

    def create_node(self, node: TreeRuntimeState) -> None:
        """Insert a durable running leaf placeholder.

        Dynamic updates are intentionally not represented here; callers use
        :class:`backend.domain.runtime_state.NodeWriter` and invoke
        :meth:`finalize_node` only for the terminal delete frame.
        """

        if node.data_type == "root":
            raise ValueError("Root nodes are created only with session metadata.")
        if node.status != "running":
            raise ValueError("A runtime node must be created with status='running'.")
        if node.parent_id and self.get_node(node.parent_session_id, node.parent_id) is None:
            raise ValueError("A runtime node parent must be present in the store.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            if connection.execute("SELECT 1 FROM session_meta").fetchone() is None:
                raise ValueError(f"Unknown session: {node.session_id}")
            if node.parent_id:
                parent = connection.execute(
                    "SELECT status FROM runtime_nodes WHERE session_id=? AND id=?",
                    (node.parent_session_id, node.parent_id),
                ).fetchone()
                if parent is not None and str(parent[0]) == "running":
                    raise ValueError("A running node cannot have a running child.")
            active = connection.execute(
                """SELECT n.id FROM runtime_nodes n
                   WHERE n.session_id=? AND n.status='running'
                     AND NOT EXISTS (
                       SELECT 1 FROM runtime_nodes c
                       WHERE c.parent_session_id=n.session_id AND c.parent_id=n.id
                     ) LIMIT 1""",
                (node.session_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("A session may have only one running leaf.")
            connection.execute(
                """INSERT INTO runtime_nodes (
                    session_id, parent_session_id, id, parent_id, version,
                    first_kept_entry_id, compaction_idx, user, provider_name, model_json,
                    permission_mode, running_mode, usage_json, cwd, timestamp, status, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._node_values(node),
            )
            connection.execute("UPDATE session_meta SET updated_at=?", (node.timestamp,))
            self._queue(
                connection,
                node.session_id,
                kind="node_upserted",
                payload={"node": node.to_dict()},
                object_namespace="runtime_node",
                object_id=node.id,
            )
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)

    def get_node(self, session_id: str, node_id: str) -> TreeRuntimeState | None:
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider_name,model_json,permission_mode,running_mode,usage_json,cwd,timestamp,status,data_json "
                "FROM runtime_nodes WHERE session_id=? AND id=?",
                (session_id, node_id),
            ).fetchone()
        return self._node_from_row(row) if row is not None else None

    def get_session_root(self, session_id: str) -> TreeRuntimeState | None:
        """Return the deterministic root owned by a session."""

        return self.get_node(session_id, session_root_id(session_id))

    def list_children(self, parent_session_id: str, parent_id: str) -> list[TreeRuntimeState]:
        """Query children using the cross-session parent reference."""

        path = self.paths.session_db(parent_session_id)
        if not path.exists():
            return []
        with self._connection(parent_session_id) as connection:
            rows = connection.execute(
                "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider_name,model_json,permission_mode,running_mode,usage_json,cwd,timestamp,status,data_json "
                "FROM runtime_nodes WHERE parent_session_id=? AND parent_id=? ORDER BY timestamp,id",
                (parent_session_id, parent_id),
            ).fetchall()
        # The query above finds same-session children.  Fork roots live in a
        # different database, so scan the local session directories as well;
        # this remains bounded by the user's local session set.
        result = [self._node_from_row(row) for row in rows]
        for directory in self.paths.root.iterdir():
            if (
                not directory.is_dir()
                or not directory.name.startswith("session_")
                or directory.name == parent_session_id
            ):
                continue
            if not (directory / "state.db").exists():
                continue
            with self._connection(directory.name) as connection:
                rows = connection.execute(
                    "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider_name,model_json,permission_mode,running_mode,usage_json,cwd,timestamp,status,data_json "
                    "FROM runtime_nodes WHERE parent_session_id=? AND parent_id=? ORDER BY timestamp,id",
                    (parent_session_id, parent_id),
                ).fetchall()
            result.extend(self._node_from_row(row) for row in rows)
        return result

    def load_nodes(self, session_id: str) -> list[TreeRuntimeState]:
        with self._connection(session_id) as connection:
            objects = connection.execute(
                "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace='runtime_node' ORDER BY updated_at,object_id",
                (session_id,),
            ).fetchall()
            if objects:
                nodes = [TreeRuntimeState.from_dict(json.loads(str(row[0]))) for row in objects]
            else:
                rows = connection.execute(
                    "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider_name,model_json,permission_mode,running_mode,usage_json,cwd,timestamp,status,data_json "
                    "FROM runtime_nodes ORDER BY timestamp,id"
                ).fetchall()
                nodes = [self._node_from_row(row) for row in rows]
        result = {node.key: node for node in nodes}
        pending = list(result.values())
        while pending:
            node = pending.pop()
            if not node.parent_id:
                continue
            key = (node.parent_session_id, node.parent_id)
            if key in result:
                continue
            parent = self.get_node(*key)
            if parent is not None:
                result[key] = parent
                pending.append(parent)
        return sorted(result.values(), key=lambda item: (item.timestamp, item.id))

    def finalize_node(self, node: TreeRuntimeState) -> None:
        """Atomically replace a running leaf with its terminal node."""

        if node.data_type == "root":
            raise ValueError("Root nodes are immutable.")
        if self.list_children(node.session_id, node.id):
            raise ValueError("Only a leaf runtime node can be finalized.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            existing = connection.execute(
                "SELECT status FROM runtime_nodes WHERE session_id=? AND id=?", (node.session_id, node.id)
            ).fetchone()
            if existing is None:
                raise ValueError(f"Unknown runtime node: {node.session_id}/{node.id}")
            if str(existing[0]) != "running":
                raise ValueError("Sealed runtime nodes are read-only.")
            if node.status not in {"success", "abort"}:
                raise ValueError("A runtime node can only be finalized as success or abort.")
            child = connection.execute(
                "SELECT 1 FROM runtime_nodes WHERE parent_session_id=? AND parent_id=? LIMIT 1",
                (node.session_id, node.id),
            ).fetchone()
            if child is not None:
                raise ValueError("Only a leaf runtime node can be finalized.")
            connection.execute(
                """UPDATE runtime_nodes SET parent_session_id=?, parent_id=?, version=?,
                    first_kept_entry_id=?, compaction_idx=?, user=?, provider_name=?, model_json=?,
                    permission_mode=?, running_mode=?, usage_json=?, cwd=?, timestamp=?, status=?, data_json=?
                    WHERE session_id=? AND id=?""",
                (
                    node.parent_session_id,
                    node.parent_id,
                    node.version,
                    node.firstKeptEntryId,
                    node.compactionIdx,
                    node.user,
                    node.provider_name,
                    json.dumps(node.model, ensure_ascii=False, separators=(",", ":")),
                    node.permission_mode,
                    node.running_mode,
                    json.dumps(node.usage, ensure_ascii=False, separators=(",", ":")),
                    node.cwd,
                    node.timestamp,
                    node.status,
                    json.dumps(node.data, ensure_ascii=False, separators=(",", ":")),
                    node.session_id,
                    node.id,
                ),
            )
            connection.execute("UPDATE session_meta SET updated_at=?", (node.timestamp,))
            self._queue(
                connection,
                node.session_id,
                kind="node_finalized",
                payload={"node": node.to_dict()},
                object_namespace="runtime_node",
                object_id=node.id,
            )
            self._put_json_object(connection, node.session_id, "runtime_node", node.id, node.to_dict(), node.timestamp)

    def runtime_state_document(self, session_id: str) -> dict[str, object]:
        """Return the new sync shape; no legacy runtime tables are included."""

        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        if session.local_only:
            raise ValueError("Local-only sessions are excluded from cloud sync.")
        return {
            "schema_version": SCHEMA_VERSION,
            "session": {
                "session_id": session.session_id,
                "title": session.title,
                "title_is_custom": session.title_is_custom,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "client_id": session.client_id,
                "owner_device_id": self.device_id,
            },
            "nodes": [node.to_dict() for node in self.load_nodes(session_id) if node.session_id == session_id],
        }

    @staticmethod
    def _node_values(node: TreeRuntimeState) -> tuple[object, ...]:
        return (
            node.session_id,
            node.parent_session_id,
            node.id,
            node.parent_id,
            node.version,
            node.firstKeptEntryId,
            node.compactionIdx,
            node.user,
            node.provider_name,
            json.dumps(node.model, ensure_ascii=False, separators=(",", ":")),
            node.permission_mode,
            node.running_mode,
            json.dumps(node.usage, ensure_ascii=False, separators=(",", ":")),
            node.cwd,
            node.timestamp,
            node.status,
            json.dumps(node.data, ensure_ascii=False, separators=(",", ":")),
        )

    @staticmethod
    def _node_from_row(row: sqlite3.Row | tuple[object, ...]) -> TreeRuntimeState:
        return TreeRuntimeState.from_dict(
            {
                "session_id": row[0],
                "parent_session_id": row[1],
                "id": row[2],
                "parent_id": row[3],
                "version": row[4],
                "firstKeptEntryId": row[5],
                "compactionIdx": row[6],
                "user": row[7],
                "provider_name": row[8],
                "model": json.loads(str(row[9])),
                "permission_mode": row[10],
                "running_mode": row[11],
                "usage": json.loads(str(row[12])),
                "cwd": row[13],
                "timestamp": row[14],
                "status": row[15],
                "data": json.loads(str(row[16])),
            }
        )

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
            meta = connection.execute("SELECT title, title_is_custom FROM session_meta").fetchone()
            if meta is None:
                raise ValueError(f"Unknown session: {session_id}")
            title = str(meta[0])
            # Automatic naming applies only while the full parent chain has no
            # user message, so the first prompt of a fresh conversation (and of
            # a rewind that undid its first user message) becomes the title.
            # The legacy session_messages projection is checked for the current
            # session only; cross-session ancestors always carry canonical
            # runtime_nodes and are walked through the deterministic roots.
            if not bool(meta[1]) and not self._chain_has_user_message(connection, session_id):
                title = normalize_session_title(task)
            connection.execute(
                """INSERT INTO session_runs VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET task=excluded.task,status='running',updated_at=excluded.updated_at""",
                (
                    run_id,
                    task,
                    origin.workflow_id,
                    origin.attempt,
                    origin.trigger,
                    origin.source_session_id,
                    origin.source_run_id,
                    timestamp,
                    timestamp,
                ),
            )
            if append_user_message:
                connection.execute(
                    "INSERT INTO session_messages(run_id,role,content,created_at) "
                    "SELECT ?, 'user', ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM session_messages WHERE run_id=? AND role='user')",
                    (run_id, task, timestamp, run_id),
                )
                self._queue(
                    connection,
                    session_id,
                    kind="runtime_message_appended",
                    payload={
                        "run_id": run_id,
                        "sequence": 1,
                        "kind": "user",
                        "message": task,
                        "data": {},
                        "created_at": timestamp,
                    },
                    object_namespace="runtime_message",
                    object_id=f"{run_id}:1",
                )
            connection.execute("UPDATE session_meta SET title=?, updated_at=?", (title, timestamp))
            self._queue(
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
        """Return whether the full parent chain contains a user message.

        Walks the deterministic session roots upward so fork/rewind ancestors
        that live in a different session database are included.  The caller's
        connection is reused for the current session (opening a second
        connection to the same database would hit the migration write lock);
        ancestor databases are opened individually.  The chain is bounded by
        the user's local session set, so a cycle is impossible.
        """

        visited: set[str] = set()
        current = session_id
        while current and current not in visited:
            visited.add(current)
            if current == session_id:
                found, parent_session = self._chain_query(connection, current)
            else:
                if not self.paths.session_db(current).exists():
                    return False
                with self._connection(current) as ancestor:
                    found, parent_session = self._chain_query(ancestor, current)
            if found:
                return True
            if parent_session is None:
                return False
            current = parent_session
        return False

    @staticmethod
    def _chain_query(connection: sqlite3.Connection, session_id: str) -> tuple[bool, str | None]:
        """Query one chain session for a user message and its root parent."""

        if (
            connection.execute(
                "SELECT 1 FROM runtime_nodes "
                "WHERE json_extract(data_json, '$.type')='message' "
                "AND json_extract(data_json, '$.message.role')='user' LIMIT 1"
            ).fetchone()
            is not None
            or connection.execute("SELECT 1 FROM session_messages WHERE role='user' LIMIT 1").fetchone()
            is not None
        ):
            return True, None
        parent_row = connection.execute(
            "SELECT parent_session_id FROM runtime_nodes WHERE id=?",
            (session_root_id(session_id),),
        ).fetchone()
        if parent_row is None or not str(parent_row[0]):
            return False, None
        return False, str(parent_row[0])

    def append_turn_input(self, session_id: str, run_id: str, content: str) -> None:
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            if connection.execute("SELECT 1 FROM session_runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise ValueError(f"Unknown session run: {run_id}")
            timestamp = utc_now()
            connection.execute(
                "INSERT INTO session_messages(run_id,role,content,created_at) VALUES (?, 'user', ?, ?)",
                (run_id, content, timestamp),
            )
            connection.execute("UPDATE session_meta SET updated_at=?", (timestamp,))
            self._queue(
                connection,
                session_id,
                kind="turn_input_appended",
                payload={"run_id": run_id, "role": "user", "content": content, "created_at": timestamp},
            )

    def finish_turn(self, session_id: str, run_id: str, status: RunStatus, answer: str | None) -> None:
        timestamp = utc_now()
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            if (
                connection.execute(
                    "UPDATE session_runs SET status=?, updated_at=? WHERE run_id=?", (status, timestamp, run_id)
                ).rowcount
                == 0
            ):
                raise ValueError(f"Unknown session run: {run_id}")
            content = assistant_content(status, answer)
            if (
                connection.execute(
                    "UPDATE session_messages SET content=?,created_at=? WHERE run_id=? AND role='assistant'",
                    (content, timestamp, run_id),
                ).rowcount
                == 0
            ):
                connection.execute(
                    "INSERT INTO session_messages(run_id,role,content,created_at) VALUES (?, 'assistant', ?, ?)",
                    (run_id, content, timestamp),
                )
            self._queue(
                connection,
                session_id,
                kind="runtime_message_appended",
                payload={
                    "run_id": run_id,
                    "sequence": 2,
                    "kind": "assistant",
                    "message": content,
                    "data": {"status": str(status)},
                    "created_at": timestamp,
                },
                object_namespace="runtime_message",
                object_id=f"{run_id}:2",
            )
            connection.execute("UPDATE session_meta SET updated_at=?", (timestamp,))
            self._queue(
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
        payload = encode_runtime_state(state)
        with self._connection(state.session_id) as connection:
            self._assert_writable(connection)
            if connection.execute("SELECT 1 FROM session_meta").fetchone() is None:
                raise ValueError(f"Unknown session: {state.session_id}")
            connection.execute(
                "INSERT INTO session_runtime VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at",
                (state.session_id, payload, timestamp),
            )
            run = state.current_run
            if run is not None:
                # Resume reconstruction creates a new RunState before the
                # execution loop emits its first checkpoint.  Register that
                # attempt in the per-session run index before updating its
                # status; otherwise the first durable event (and the final
                # answer) is rejected as an unknown run.
                existing_run = connection.execute("SELECT 1 FROM session_runs WHERE run_id=?", (run.run_id,)).fetchone()
                if existing_run is None:
                    provenance = run.provenance
                    connection.execute(
                        """INSERT INTO session_runs
                        (run_id, task, status, workflow_id, attempt, origin_kind,
                         source_session_id, source_run_id, started_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            run.run_id,
                            run.task,
                            run.status,
                            provenance.workflow_id,
                            provenance.attempt,
                            provenance.trigger,
                            provenance.source_session_id,
                            provenance.source_run_id,
                            timestamp,
                            timestamp,
                        ),
                    )
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,state_json=excluded.state_json,updated_at=excluded.updated_at",
                    (run.run_id, run.status, payload, timestamp),
                )
                connection.execute(
                    "INSERT INTO checkpoints(run_id, reason, state_json, created_at) VALUES (?, ?, ?, ?)",
                    (run.run_id, reason, payload, timestamp),
                )
                connection.execute(
                    "UPDATE session_runs SET status=?, updated_at=? WHERE run_id=?",
                    (run.status, timestamp, run.run_id),
                )
                self._save_runtime_messages(connection, run.runtime_messages, run.run_id)
            connection.execute("UPDATE session_meta SET updated_at=?", (timestamp,))
            state_payload = state.to_dict(include_runtime_messages=False)
            # Chat history and audit messages have their own immutable append
            # events; keeping them out of this state delta prevents a growing
            # conversation from being copied into every checkpoint event.
            state_payload["messages"] = []
            self._queue(
                connection,
                state.session_id,
                kind="runtime_state_saved",
                payload={
                    "reason": reason,
                    "state": state_payload,
                    "run_id": run.run_id if run is not None else None,
                },
            )
            self._put_json_object(connection, state.session_id, "runtime_state", state.session_id, json.loads(payload), timestamp)
            if run is not None:
                checkpoint_payload = {
                    "run_id": run.run_id,
                    "reason": reason,
                    "state": state_payload,
                    "created_at": timestamp,
                }
                self._queue(
                    connection,
                    state.session_id,
                    kind="checkpoint_recorded",
                    payload=checkpoint_payload,
                    object_namespace="checkpoint",
                    object_id=f"{run.run_id}:{timestamp}:{reason}",
                )

    def load_runtime(self, session_id: str) -> RuntimeState | None:
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace='runtime_state' LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                row = connection.execute("SELECT state_json FROM session_runtime").fetchone()
        if row is None:
            return None
        state = decode_runtime_state(str(row[0]))
        if state.current_run is not None:
            state.current_run.runtime_messages = self.load_runtime_messages(session_id, state.current_run.run_id)
        return state

    def append_runtime_message(self, session_id: str, run_id: str, message: RuntimeMessage) -> None:
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            if connection.execute("SELECT 1 FROM session_runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise ValueError(f"Unknown session run: {run_id}")
            self._insert_runtime_message(connection, message, run_id)
            connection.execute("UPDATE session_meta SET updated_at=?", (message.timestamp,))
            self._queue(
                connection,
                session_id,
                kind="runtime_message_appended",
                payload={
                    "run_id": run_id,
                    "sequence": message.sequence,
                    "kind": message.kind,
                    "message": message.message,
                    "data": message.data,
                    "created_at": message.timestamp,
                },
                object_namespace="runtime_message",
                object_id=f"{run_id}:{message.sequence}",
            )
            self._put_json_object(
                connection,
                session_id,
                "runtime_message",
                f"{run_id}:{message.sequence}",
                {
                    "run_id": run_id,
                    "sequence": message.sequence,
                    "kind": message.kind,
                    "message": message.message,
                    "data": message.data,
                    "created_at": message.timestamp,
                },
                message.timestamp,
            )

    def load_runtime_messages(self, session_id: str, run_id: str | None = None) -> list[RuntimeMessage]:
        with self._connection(session_id) as connection:
            objects = connection.execute(
                "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace='runtime_message' ORDER BY object_id",
                (session_id,),
            ).fetchall()
            if objects:
                values = [json.loads(str(row[0])) for row in objects]
                if run_id is not None:
                    values = [item for item in values if str(item.get("run_id") or "") == run_id]
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
            query = "SELECT sequence, kind, message, data_json, created_at FROM runtime_messages"
            values: tuple[object, ...] = () if run_id is None else (run_id,)
            if run_id is not None:
                query += " WHERE run_id=?"
            rows = connection.execute(query + " ORDER BY run_id, sequence", values).fetchall()
        return [
            RuntimeMessage(int(row[0]), str(row[1]), str(row[2]), str(row[4]), decode_message_data(str(row[3])))
            for row in rows
        ]

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
            "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (
                session_id,
                namespace,
                object_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                updated_at,
            ),
        )

    def resume_runtime(self, source: RuntimeState, resumed: RuntimeState) -> None:
        self._save_state(source, f"run_{source.current_run.status}" if source.current_run else "resume")
        self._save_state(resumed, "run_resumed")

    @staticmethod
    def _session(row: sqlite3.Row) -> Session:
        return Session(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]) if row[4] is not None else None,
            str(row[5]) if row[5] is not None else None,
            str(row[6]) if row[6] is not None else None,
            bool(row[7]) if len(row) > 7 else False,
            bool(row[8]) if len(row) > 8 else False,
        )

    @staticmethod
    def _insert_runtime_message(connection: sqlite3.Connection, message: RuntimeMessage, run_id: str) -> None:
        connection.execute(
            "INSERT INTO runtime_messages VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, sequence) DO UPDATE SET kind=excluded.kind,message=excluded.message,data_json=excluded.data_json,created_at=excluded.created_at",
            (
                run_id,
                message.sequence,
                message.kind,
                message.message,
                encode_message_data(message.data),
                message.timestamp,
            ),
        )

    def _save_runtime_messages(
        self, connection: sqlite3.Connection, messages: list[RuntimeMessage], run_id: str
    ) -> None:
        for message in messages:
            self._insert_runtime_message(connection, message, run_id)

    @staticmethod
    def _assert_writable(connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT read_only FROM session_meta").fetchone()
        if row is not None and int(row[0]):
            raise PermissionError("Remote sessions are read-only; fork the session before writing.")
