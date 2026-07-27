"""Per-session SQLite persistence for the local-first client."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from backend.configuration import ClientPaths
from backend.domain import (
    DEFAULT_SESSION_TITLE,
    RunProvenance,
    RunStatus,
    RuntimeMessage,
    Session,
    SessionSummary,
    new_run_id,
    new_session_id,
)
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState, text_messages

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT PRIMARY KEY, title TEXT NOT NULL, owner_device_id TEXT NOT NULL,
    remote_revision INTEGER NOT NULL DEFAULT 0, read_only INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_runs (
    run_id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL, workflow_id TEXT,
    attempt INTEGER NOT NULL, origin_kind TEXT NOT NULL, source_session_id TEXT,
    source_run_id TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_runtime (
    session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, reason TEXT NOT NULL,
    state_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_messages (
    run_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
    data_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT UNIQUE, base_revision INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'snapshot', payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
    acknowledged_at TEXT
);
"""


class SQLiteSessionStore:
    """Local durable store; each session owns one self-contained state.db."""

    def __init__(self, paths: ClientPaths, device_id: str) -> None:
        self.paths = paths
        self.device_id = device_id
        self.paths.ensure()
        self._sync_listener = None

    @contextmanager
    def _connection(self, session_id: str) -> Iterator[sqlite3.Connection]:
        path = self.paths.session_db(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_SCHEMA)
            self._migrate_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _title(title: str | None) -> str:
        return " ".join((title or "").split())[:80] or DEFAULT_SESSION_TITLE

    @staticmethod
    def _assistant_content(status: RunStatus, answer: str | None) -> str:
        if status == "completed":
            return answer or ""
        if status == "cancelled":
            return "Task cancelled by user."
        return answer or f"Task {status}."

    def set_sync_listener(self, listener) -> None:
        self._sync_listener = listener

    def create_session(self, title: str | None = None) -> Session:
        session = Session(new_session_id(), self._title(title), utc_now(), utc_now())
        with self._connection(session.session_id) as connection:
            connection.execute(
                "INSERT INTO session_meta(session_id,title,owner_device_id,created_at,updated_at) VALUES (?, ?, ?, ?, ?)",
                (session.session_id, session.title, self.device_id, session.created_at, session.updated_at),
            )
            self._queue(connection, session.session_id)
        return session

    def get_session(self, session_id: str) -> Session | None:
        path = self.paths.session_db(session_id)
        if not path.exists():
            return None
        with self._connection(session_id) as connection:
            row = connection.execute("SELECT session_id, title, created_at, updated_at FROM session_meta").fetchone()
        return self._session(row) if row else None

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        path = self.paths.session_db(session_id)
        if not path.exists():
            return None
        with self._connection(session_id) as connection:
            row = connection.execute(
                """SELECT m.session_id, m.title, m.created_at, m.updated_at,
                (SELECT COUNT(*) FROM session_messages),
                (SELECT run_id FROM session_runs ORDER BY updated_at DESC, run_id DESC LIMIT 1),
                (SELECT status FROM session_runs ORDER BY updated_at DESC, run_id DESC LIMIT 1)
                FROM session_meta AS m"""
            ).fetchone()
        if row is None:
            return None
        return SessionSummary(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            int(row[4]),
            str(row[5]) if row[5] is not None else None,
            str(row[6]) if row[6] is not None else None,
        )

    def list_sessions(self) -> list[SessionSummary]:
        summaries = [
            summary
            for directory in self.paths.root.iterdir()
            if directory.is_dir() and directory.name.startswith("session_") and (directory / "state.db").exists()
            if (summary := self.get_session_summary(directory.name)) is not None
        ]
        return sorted(summaries, key=lambda item: (item.updated_at, item.session_id), reverse=True)

    def latest_session(self) -> Session | None:
        summaries = self.list_sessions()
        return self.get_session(summaries[0].session_id) if summaries else None

    def load_conversation(self, session_id: str) -> list[dict[str, str]]:
        with self._connection(session_id) as connection:
            rows = connection.execute("SELECT role, content FROM session_messages ORDER BY id").fetchall()
        return [{"role": str(row[0]), "content": str(row[1])} for row in rows]

    def load_conversation_page(
        self, session_id: str, *, before_id: int | None = None, limit: int = 100
    ) -> tuple[list[dict[str, str]], int | None]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connection(session_id) as connection:
            query = "SELECT id, role, content FROM session_messages"
            values: tuple[object, ...] = () if before_id is None else (before_id,)
            if before_id is not None:
                query += " WHERE id < ?"
            rows = connection.execute(query + " ORDER BY id DESC LIMIT ?", (*values, limit + 1)).fetchall()
        page = rows[:limit]
        next_before = int(page[-1][0]) if len(rows) > limit and page else None
        return ([{"role": str(row[1]), "content": str(row[2])} for row in reversed(page)], next_before)

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
                title = self._title(task)
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
            content = self._assistant_content(status, answer)
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
        payload = json.dumps(state.to_dict(include_runtime_messages=False), ensure_ascii=False)
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
        state = RuntimeState.from_dict(json.loads(str(row[0])))
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
            RuntimeMessage(int(row[0]), str(row[1]), str(row[2]), str(row[4]), dict(json.loads(str(row[3]))))
            for row in rows
        ]

    def resume_runtime(self, source: RuntimeState, resumed: RuntimeState) -> None:
        self._save_state(source, f"run_{source.current_run.status}" if source.current_run else "resume")
        self._save_state(resumed, "run_resumed")

    def pending_sync_operations(self) -> list[dict[str, object]]:
        operations: list[dict[str, object]] = []
        for summary in self.list_sessions():
            with self._connection(summary.session_id) as connection:
                rows = connection.execute(
                    "SELECT operation_id,base_revision,kind,payload_json FROM sync_outbox WHERE acknowledged_at IS NULL AND operation_id IS NOT NULL ORDER BY id DESC LIMIT 1"
                ).fetchall()
            for row in rows:
                operations.append(
                    {
                        "operation_id": str(row[0]),
                        "session_id": summary.session_id,
                        "base_revision": int(row[1]),
                        "kind": str(row[2]),
                        "snapshot": dict(json.loads(str(row[3]))),
                    }
                )
        return operations

    def acknowledge_sync_operations(self, acknowledgements: list[dict[str, object]]) -> None:
        by_id = {
            str(item["operation_id"]): int(item["revision"])
            for item in acknowledgements
            if item.get("operation_id") and item.get("revision") is not None
        }
        if not by_id:
            return
        for summary in self.list_sessions():
            with self._connection(summary.session_id) as connection:
                for operation_id, revision in by_id.items():
                    row = connection.execute(
                        "SELECT id FROM sync_outbox WHERE operation_id=?", (operation_id,)
                    ).fetchone()
                    updated = connection.execute(
                        "UPDATE sync_outbox SET acknowledged_at=? WHERE acknowledged_at IS NULL AND id<=?",
                        (utc_now(), int(row[0]) if row else -1),
                    )
                    if updated.rowcount:
                        connection.execute("UPDATE session_meta SET remote_revision=?", (revision,))
                        connection.execute(
                            "UPDATE sync_outbox SET base_revision=? WHERE acknowledged_at IS NULL AND id>?",
                            (revision, int(row[0])),
                        )

    def remote_revision(self, session_id: str) -> int:
        with self._connection(session_id) as connection:
            row = connection.execute("SELECT remote_revision FROM session_meta").fetchone()
        return int(row[0]) if row else 0

    def apply_remote_snapshot(self, item: dict[str, object], *, local_device_id: str) -> None:
        session_id = str(item.get("session_id") or "")
        owner_device_id = str(item.get("owner_device_id") or "")
        revision = int(item.get("revision", 0))
        snapshot = item.get("snapshot")
        if not session_id or not owner_device_id or revision < 1 or not isinstance(snapshot, dict):
            raise ValueError("Invalid remote session snapshot.")
        existing = self.get_session(session_id)
        if existing is not None:
            with self._connection(session_id) as connection:
                current = connection.execute("SELECT owner_device_id,remote_revision FROM session_meta").fetchone()
                if current is None:
                    raise ValueError("Existing session is missing metadata.")
                current_owner, current_revision = str(current[0]), int(current[1])
                if current_owner != owner_device_id:
                    raise ValueError("Remote session owner does not match local metadata.")
                if current_owner == local_device_id:
                    if revision > current_revision:
                        connection.execute("UPDATE session_meta SET remote_revision=?", (revision,))
                    return
                if revision <= current_revision:
                    return
        meta = snapshot.get("session")
        runtime = snapshot.get("runtime")
        if not isinstance(meta, dict):
            raise ValueError("Remote snapshot is missing session metadata.")
        if meta.get("session_id") not in {None, session_id}:
            raise ValueError("Remote snapshot session id does not match its envelope.")
        with self._connection(session_id) as connection:
            for table in (
                "session_meta",
                "session_runs",
                "session_messages",
                "session_runtime",
                "runs",
                "checkpoints",
                "runtime_messages",
                "sync_outbox",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.execute(
                "INSERT INTO session_meta(session_id,title,owner_device_id,remote_revision,read_only,schema_version,created_at,updated_at) VALUES (?,?,?,?,1,?,?,?)",
                (
                    session_id,
                    str(meta.get("title") or DEFAULT_SESSION_TITLE),
                    owner_device_id,
                    revision,
                    int(snapshot.get("schema_version", _SCHEMA_VERSION)),
                    str(meta.get("created_at") or utc_now()),
                    str(meta.get("updated_at") or utc_now()),
                ),
            )
            if isinstance(runtime, dict):
                restored_runtime = dict(runtime)
                restored_runtime["session_id"] = session_id
                connection.execute(
                    "INSERT INTO session_runtime(session_id,state_json,updated_at) VALUES (?,?,?)",
                    (session_id, json.dumps(restored_runtime, ensure_ascii=False), utc_now()),
                )
            self._restore_snapshot_tables(connection, snapshot, runtime)

    @staticmethod
    def _restore_snapshot_tables(connection: sqlite3.Connection, snapshot: dict[str, object], runtime: object) -> None:
        table_specs = {
            "session_runs": (
                "run_id",
                "task",
                "status",
                "workflow_id",
                "attempt",
                "origin_kind",
                "source_session_id",
                "source_run_id",
                "started_at",
                "updated_at",
            ),
            "session_messages": ("id", "run_id", "role", "content", "created_at"),
            "runs": ("run_id", "status", "state_json", "updated_at"),
            "checkpoints": ("id", "run_id", "reason", "state_json", "created_at"),
            "runtime_messages": ("run_id", "sequence", "kind", "message", "data_json", "created_at"),
        }
        restored_any = False
        for table, columns in table_specs.items():
            rows = snapshot.get(table, [])
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"Remote snapshot {table} must be a list of objects.")
            if rows:
                placeholders = ",".join("?" for _ in columns)
                connection.executemany(
                    f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
                    [tuple(row.get(column) for column in columns) for row in rows],
                )
                restored_any = True
        if restored_any or not isinstance(runtime, dict):
            return
        state = RuntimeState.from_dict(runtime)
        run = state.current_run
        if run is None:
            return
        origin = run.provenance
        connection.execute(
            "INSERT INTO session_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run.run_id,
                run.task,
                run.status,
                origin.workflow_id,
                origin.attempt,
                origin.trigger,
                origin.source_session_id,
                origin.source_run_id,
                state.created_at,
                state.updated_at,
            ),
        )
        connection.execute(
            "INSERT INTO runs(run_id,status,state_json,updated_at) VALUES (?,?,?,?)",
            (run.run_id, run.status, json.dumps(runtime, ensure_ascii=False), state.updated_at),
        )
        for message in text_messages(state.messages):
            connection.execute(
                "INSERT INTO session_messages(run_id,role,content,created_at) VALUES (?,?,?,?)",
                (run.run_id, message["role"], message["content"], state.updated_at),
            )

    def list_forkable_runs(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for summary in self.list_sessions():
            with self._connection(summary.session_id) as connection:
                rows = connection.execute(
                    "SELECT s.run_id,s.task,s.status,s.updated_at FROM session_runs AS s "
                    "JOIN runs AS r ON r.run_id=s.run_id "
                    "WHERE s.status!='running' AND r.status!='running' ORDER BY s.updated_at DESC"
                ).fetchall()
            result.extend(
                {
                    "run_id": str(row[0]),
                    "task": str(row[1]),
                    "status": str(row[2]),
                    "updated_at": str(row[3]),
                }
                for row in rows
            )
        return sorted(result, key=lambda item: item["updated_at"], reverse=True)

    def fork_run(self, run_id: str) -> Session:
        for summary in self.list_sessions():
            with self._connection(summary.session_id) as source:
                row = source.execute("SELECT status, state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                continue
            if row[0] == "running":
                raise ValueError("A running run cannot be forked.")
            state = RuntimeState.from_dict(json.loads(str(row[1])))
            if state.current_run is None:
                raise ValueError("Run snapshot cannot be forked.")
            target = self.create_session(f"Fork: {summary.title}")
            state.session_id = target.session_id
            state.current_run.run_id = new_run_id()
            state.current_run.provenance = RunProvenance(
                workflow_id=state.current_run.provenance.workflow_id,
                trigger="legacy",
                source_session_id=summary.session_id,
                source_run_id=run_id,
            )
            self.start_turn(
                target.session_id,
                state.current_run.run_id,
                state.current_run.task,
                state.current_run.provenance,
                append_user_message=False,
            )
            with self._connection(target.session_id) as connection:
                timestamp = utc_now()
                for message in text_messages(state.messages):
                    connection.execute(
                        "INSERT INTO session_messages(run_id,role,content,created_at) VALUES (?,?,?,?)",
                        (state.current_run.run_id, message["role"], message["content"], timestamp),
                    )
            self._save_state(state, "forked")
            return target
        raise ValueError(f"Unknown run: {run_id}")

    @staticmethod
    def _session(row: sqlite3.Row) -> Session:
        return Session(str(row[0]), str(row[1]), str(row[2]), str(row[3]))

    @staticmethod
    def _insert_runtime_message(connection: sqlite3.Connection, message: RuntimeMessage, run_id: str) -> None:
        connection.execute(
            "INSERT INTO runtime_messages VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, sequence) DO UPDATE SET kind=excluded.kind,message=excluded.message,data_json=excluded.data_json,created_at=excluded.created_at",
            (
                run_id,
                message.sequence,
                message.kind,
                message.message,
                json.dumps(message.data, ensure_ascii=False, default=str),
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

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        meta_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(session_meta)")}
        if "owner_device_id" not in meta_columns:
            connection.execute("ALTER TABLE session_meta ADD COLUMN owner_device_id TEXT NOT NULL DEFAULT ''")
        for name, definition in (
            ("remote_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("read_only", "INTEGER NOT NULL DEFAULT 0"),
            ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in meta_columns:
                connection.execute(f"ALTER TABLE session_meta ADD COLUMN {name} {definition}")
        outbox_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sync_outbox)")}
        connection.execute(
            "UPDATE session_meta SET owner_device_id=? WHERE owner_device_id IS NULL OR owner_device_id=''",
            (self.device_id,),
        )
        for name, definition in (
            ("operation_id", "TEXT"),
            ("base_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("kind", "TEXT NOT NULL DEFAULT 'snapshot'"),
            ("acknowledged_at", "TEXT"),
        ):
            if name not in outbox_columns:
                connection.execute(f"ALTER TABLE sync_outbox ADD COLUMN {name} {definition}")
        legacy_outbox = connection.execute("SELECT 1 FROM sync_outbox WHERE operation_id IS NULL LIMIT 1").fetchone()
        if legacy_outbox is not None:
            connection.execute("DELETE FROM sync_outbox WHERE operation_id IS NULL")
            meta = connection.execute("SELECT session_id,read_only FROM session_meta").fetchone()
            if meta is not None and not int(meta[1]):
                self._queue(connection, str(meta[0]))

    @classmethod
    def _queue(cls, connection: sqlite3.Connection, session_id: str) -> None:
        from uuid import uuid4

        meta = connection.execute("SELECT remote_revision FROM session_meta").fetchone()
        if meta is None:
            return
        snapshot = cls._export_snapshot(connection, session_id)
        connection.execute(
            "INSERT INTO sync_outbox(operation_id,base_revision,kind,payload_json,created_at) VALUES (?,?,?,?,?)",
            (
                f"operation_{uuid4().hex}",
                int(meta[0]),
                "snapshot",
                json.dumps(snapshot, ensure_ascii=False),
                utc_now(),
            ),
        )

    @staticmethod
    def _export_snapshot(connection: sqlite3.Connection, session_id: str) -> dict[str, object]:
        meta = connection.execute("SELECT title,owner_device_id,created_at,updated_at FROM session_meta").fetchone()
        if meta is None:
            raise ValueError(f"Unknown session: {session_id}")
        runtime = connection.execute("SELECT state_json FROM session_runtime").fetchone()
        snapshot: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "session": {
                "session_id": session_id,
                "title": str(meta[0]),
                "owner_device_id": str(meta[1]),
                "created_at": str(meta[2]),
                "updated_at": str(meta[3]),
            },
            "runtime": json.loads(str(runtime[0])) if runtime is not None else None,
        }
        for table in ("session_runs", "session_messages", "runs", "checkpoints", "runtime_messages"):
            snapshot[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        return snapshot
