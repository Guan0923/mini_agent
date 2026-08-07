"""Runtime turns, checkpoints, and message persistence for SQLite."""

from __future__ import annotations

import json
import sqlite3

from backend.domain import DEFAULT_SESSION_TITLE, RunProvenance, RunStatus, RuntimeMessage, Session
from backend.domain.runtime_state import RuntimeState as TreeRuntimeState
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


class SQLiteRuntimeMixin:
    """Legacy turn persistence plus the canonical ``runtime_nodes`` store."""

    def create_node(self, node: TreeRuntimeState) -> None:
        """Insert a failed placeholder node.

        Dynamic updates are intentionally not represented here; callers use
        :class:`backend.domain.runtime_state.NodeWriter` and invoke
        :meth:`finalize_node` only for the terminal delete frame.
        """

        if node.status != "failed":
            raise ValueError("A runtime node must be created with status='failed'.")
        if node.parent_id and self.get_node(node.parent_session_id, node.parent_id) is None:
            raise ValueError("A runtime node parent must be present in the store.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            if connection.execute("SELECT 1 FROM session_meta").fetchone() is None:
                raise ValueError(f"Unknown session: {node.session_id}")
            connection.execute(
                """INSERT INTO runtime_nodes (
                    session_id, parent_session_id, id, parent_id, version,
                    first_kept_entry_id, compaction_idx, user, provider, cwd,
                    timestamp, status, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._node_values(node),
            )
            connection.execute("UPDATE session_meta SET updated_at=?", (node.timestamp,))
            self._queue(connection, node.session_id)

    def get_node(self, session_id: str, node_id: str) -> TreeRuntimeState | None:
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider,cwd,timestamp,status,data_json "
                "FROM runtime_nodes WHERE session_id=? AND id=?",
                (session_id, node_id),
            ).fetchone()
        return self._node_from_row(row) if row is not None else None

    def list_children(self, parent_session_id: str, parent_id: str) -> list[TreeRuntimeState]:
        """Query children using the cross-session parent reference."""

        path = self.paths.session_db(parent_session_id)
        if not path.exists():
            return []
        with self._connection(parent_session_id) as connection:
            rows = connection.execute(
                "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider,cwd,timestamp,status,data_json "
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
                    "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider,cwd,timestamp,status,data_json "
                    "FROM runtime_nodes WHERE parent_session_id=? AND parent_id=? ORDER BY timestamp,id",
                    (parent_session_id, parent_id),
                ).fetchall()
            result.extend(self._node_from_row(row) for row in rows)
        return result

    def load_nodes(self, session_id: str) -> list[TreeRuntimeState]:
        with self._connection(session_id) as connection:
            rows = connection.execute(
                "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider,cwd,timestamp,status,data_json "
                "FROM runtime_nodes ORDER BY timestamp,id"
            ).fetchall()
        result = {node.key: node for node in (self._node_from_row(row) for row in rows)}
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
        """Atomically replace a failed leaf with its final node."""

        if self.list_children(node.session_id, node.id):
            raise ValueError("Only a leaf runtime node can be finalized.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            existing = connection.execute(
                "SELECT status FROM runtime_nodes WHERE session_id=? AND id=?", (node.session_id, node.id)
            ).fetchone()
            if existing is None:
                raise ValueError(f"Unknown runtime node: {node.session_id}/{node.id}")
            if str(existing[0]) != "failed":
                raise ValueError("Sealed runtime nodes are read-only.")
            child = connection.execute(
                "SELECT 1 FROM runtime_nodes WHERE parent_session_id=? AND parent_id=? LIMIT 1",
                (node.session_id, node.id),
            ).fetchone()
            if child is not None:
                raise ValueError("Only a leaf runtime node can be finalized.")
            connection.execute(
                """UPDATE runtime_nodes SET parent_session_id=?, parent_id=?, version=?,
                    first_kept_entry_id=?, compaction_idx=?, user=?, provider=?, cwd=?,
                    timestamp=?, status=?, data_json=? WHERE session_id=? AND id=?""",
                (
                    node.parent_session_id,
                    node.parent_id,
                    node.version,
                    node.firstKeptEntryId,
                    node.compactionIdx,
                    node.user,
                    node.provider,
                    node.cwd,
                    node.timestamp,
                    node.status,
                    json.dumps(node.data, ensure_ascii=False, separators=(",", ":")),
                    node.session_id,
                    node.id,
                ),
            )
            connection.execute("UPDATE session_meta SET updated_at=?", (node.timestamp,))
            self._queue(connection, node.session_id)

    def runtime_node_snapshot(self, session_id: str) -> dict[str, object]:
        """Return the new sync shape; no legacy runtime tables are included."""

        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return {
            "schema_version": 3,
            "session": {
                "session_id": session.session_id,
                "title": session.title,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "client_id": session.client_id,
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
            node.provider,
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
                "provider": row[8],
                "cwd": row[9],
                "timestamp": row[10],
                "status": row[11],
                "data": json.loads(str(row[12])),
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
            meta = connection.execute("SELECT title FROM session_meta").fetchone()
            if meta is None:
                raise ValueError(f"Unknown session: {session_id}")
            title = str(meta[0])
            if (
                title == DEFAULT_SESSION_TITLE
                and connection.execute("SELECT 1 FROM session_messages LIMIT 1").fetchone() is None
            ):
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
                    "INSERT INTO session_messages(run_id, role, content, created_at) SELECT ?, 'user', ?, ? WHERE NOT EXISTS (SELECT 1 FROM session_messages WHERE run_id=? AND role='user')",
                    (run_id, task, timestamp, run_id),
                )
            connection.execute("UPDATE session_meta SET title=?, updated_at=?", (title, timestamp))
            self._queue(connection, session_id)

    def append_turn_input(self, session_id: str, run_id: str, content: str) -> None:
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            if connection.execute("SELECT 1 FROM session_runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise ValueError(f"Unknown session run: {run_id}")
            timestamp = utc_now()
            connection.execute(
                "INSERT INTO session_messages(run_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                (run_id, content, timestamp),
            )
            connection.execute("UPDATE session_meta SET updated_at=?", (timestamp,))
            self._queue(connection, session_id)

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
                    "UPDATE session_messages SET content=?, created_at=? WHERE run_id=? AND role='assistant'",
                    (content, timestamp, run_id),
                ).rowcount
                == 0
            ):
                connection.execute(
                    "INSERT INTO session_messages(run_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                    (run_id, content, timestamp),
                )
            connection.execute("UPDATE session_meta SET updated_at=?", (timestamp,))
            self._queue(connection, session_id)

    def save(self, runtime, reason: str) -> None:
        self._save_state(runtime.state, reason)
        if self._sync_listener is not None:
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
            self._queue(connection, state.session_id)

    def load_runtime(self, session_id: str) -> RuntimeState | None:
        with self._connection(session_id) as connection:
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
            self._queue(connection, session_id)

    def load_runtime_messages(self, session_id: str, run_id: str | None = None) -> list[RuntimeMessage]:
        with self._connection(session_id) as connection:
            query = "SELECT sequence, kind, message, data_json, created_at FROM runtime_messages"
            values: tuple[object, ...] = () if run_id is None else (run_id,)
            if run_id is not None:
                query += " WHERE run_id=?"
            rows = connection.execute(query + " ORDER BY run_id, sequence", values).fetchall()
        return [
            RuntimeMessage(int(row[0]), str(row[1]), str(row[2]), str(row[4]), decode_message_data(str(row[3])))
            for row in rows
        ]

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
