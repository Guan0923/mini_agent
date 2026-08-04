"""Per-session SQLite persistence for the local-first client."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from backend.configuration import ClientPaths
from backend.domain import (
    DEFAULT_SESSION_TITLE,
    RunProvenance,
    RunStatus,
    RuntimeMessage,
    Session,
    SessionSummary,
    message_from_dict,
    new_session_id,
)
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
from .sqlite_fork import SQLiteForkMixin
from .sqlite_schema import SCHEMA, SQLiteSchemaMixin
from .sqlite_sync import SQLiteSyncMixin


class SQLiteSessionStore(SQLiteForkMixin, SQLiteSyncMixin, SQLiteSchemaMixin):
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
            connection.executescript(SCHEMA)
            self._migrate_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_sync_listener(self, listener) -> None:
        self._sync_listener = listener

    def create_session(self, title: str | None = None, *, client_id: str | None = None) -> Session:
        session = Session(new_session_id(), normalize_session_title(title), utc_now(), utc_now(), client_id=client_id)
        with self._connection(session.session_id) as connection:
            connection.execute(
                "INSERT INTO session_meta(session_id,title,owner_device_id,created_at,updated_at,client_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.title,
                    self.device_id,
                    session.created_at,
                    session.updated_at,
                    session.client_id,
                ),
            )
            self._queue(connection, session.session_id)
        return session

    def get_session(self, session_id: str) -> Session | None:
        path = self.paths.session_db(session_id)
        if not path.exists():
            return None
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT session_id, title, created_at, updated_at, client_id, archived_at, deleted_at FROM session_meta"
            ).fetchone()
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
                (SELECT status FROM session_runs ORDER BY updated_at DESC, run_id DESC LIMIT 1),
                m.client_id, m.archived_at, m.deleted_at
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
            str(row[7]) if row[7] is not None else None,
            str(row[8]) if row[8] is not None else None,
            str(row[9]) if row[9] is not None else None,
        )

    def list_sessions(self, *, state: str = "active") -> list[SessionSummary]:
        if state not in {"active", "archived", "deleted", "all"}:
            raise ValueError(f"Unknown session state: {state}")
        summaries = [
            summary
            for directory in self.paths.root.iterdir()
            if directory.is_dir() and directory.name.startswith("session_") and (directory / "state.db").exists()
            if (summary := self.get_session_summary(directory.name)) is not None
            if state == "all"
            or (state == "active" and summary.is_active)
            or (state == "archived" and summary.is_archived)
            or (state == "deleted" and summary.is_deleted)
        ]
        return sorted(summaries, key=lambda item: (item.updated_at, item.session_id), reverse=True)

    def latest_session(self) -> Session | None:
        summaries = self.list_sessions()
        return self.get_session(summaries[0].session_id) if summaries else None

    def load_conversation(self, session_id: str) -> list[dict[str, str]]:
        with self._connection(session_id) as connection:
            rows = connection.execute("SELECT role, content FROM session_messages ORDER BY id").fetchall()
        return [{"role": str(row[0]), "content": str(row[1])} for row in rows]

    def load_conversation_records(self, session_id: str) -> list[dict[str, str | int | None]]:
        """Return the durable transcript with stable row and run identifiers."""

        with self._connection(session_id) as connection:
            rows = connection.execute(
                "SELECT id, run_id, role, content, created_at FROM session_messages ORDER BY id"
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "run_id": None if str(row[1]).startswith("import_") else str(row[1]),
                "role": str(row[2]),
                "content": str(row[3]),
                "created_at": str(row[4]),
            }
            for row in rows
        ]

    def find_session_by_client_id(self, client_id: str, *, include_deleted: bool = False) -> Session | None:
        """Find a Web-owned session without assuming a global metadata database."""

        if not client_id:
            return None
        for summary in self.list_sessions(state="all"):
            if summary.deleted_at is not None and not include_deleted:
                continue
            session = self.get_session(summary.session_id)
            if session is not None and session.client_id == client_id:
                return session
        return None

    def set_client_id(self, session_id: str, client_id: str | None) -> Session:
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            row = connection.execute("SELECT deleted_at FROM session_meta").fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[0] is not None:
                raise ValueError("Deleted sessions cannot change their client binding.")
            connection.execute("UPDATE session_meta SET client_id=?, updated_at=?", (client_id, utc_now()))
            self._queue(connection, session_id)
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def import_conversation(
        self,
        title: str | None,
        messages: list[dict[str, str]],
        *,
        client_id: str | None = None,
        force_new: bool = False,
    ) -> Session:
        """Create an idle session seeded with text-only legacy Web history."""

        if client_id and not force_new:
            existing = self.find_session_by_client_id(client_id)
            if existing is not None:
                return existing
        parsed = [message_from_dict(item) for item in messages if item.get("role") in {"user", "assistant"}]
        session = self.create_session(title, client_id=client_id)
        import_run_id = f"import_{uuid4().hex}"
        timestamp = utc_now()
        with self._connection(session.session_id) as connection:
            for message in parsed:
                role = "assistant" if getattr(message, "role", "") == "assistant" else "user"
                content = str(getattr(message, "content", "") or "")
                connection.execute(
                    "INSERT INTO session_messages(run_id,role,content,created_at) VALUES (?,?,?,?)",
                    (import_run_id, role, content, timestamp),
                )
        if parsed:
            runtime = self._empty_import_runtime(session.session_id, parsed)
            self.save_runtime(runtime)
        return session

    def rename_session(self, session_id: str, title: str) -> Session:
        """Rename a non-deleted session and preserve the update in sync history."""

        if not title.strip():
            raise ValueError("Session title cannot be empty.")
        cleaned = normalize_session_title(title)
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            row = connection.execute("SELECT deleted_at FROM session_meta").fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[0] is not None:
                raise ValueError("Deleted sessions cannot be renamed.")
            connection.execute("UPDATE session_meta SET title=?, updated_at=?", (cleaned, utc_now()))
            self._queue(connection, session_id)
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def archive_session(self, session_id: str) -> Session:
        return self._set_lifecycle(session_id, archived_at=utc_now())

    def restore_session(self, session_id: str) -> Session:
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            row = connection.execute("SELECT archived_at, deleted_at FROM session_meta").fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[1] is not None:
                raise ValueError("Deleted sessions cannot be restored.")
            connection.execute("UPDATE session_meta SET archived_at=NULL, updated_at=?", (utc_now(),))
            self._queue(connection, session_id)
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def delete_session(self, session_id: str) -> Session:
        """Soft-delete a session; its SQLite database remains available for audit."""

        return self._set_lifecycle(session_id, deleted_at=utc_now())

    def _set_lifecycle(
        self,
        session_id: str,
        *,
        archived_at: str | None = None,
        deleted_at: str | None = None,
    ) -> Session:
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            row = connection.execute("SELECT deleted_at FROM session_meta").fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[0] is not None and deleted_at is None:
                raise ValueError("Deleted sessions cannot be changed.")
            assignments: list[str] = ["updated_at=?"]
            values: list[object] = [utc_now()]
            if archived_at is not None:
                assignments.append("archived_at=?")
                values.append(archived_at)
            if deleted_at is not None:
                assignments.append("deleted_at=?")
                values.append(deleted_at)
            values.append(session_id)
            connection.execute(
                f"UPDATE session_meta SET {', '.join(assignments)} WHERE session_id=?",
                values,
            )
            self._queue(connection, session_id)
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    @contextmanager
    def _connection_for_existing(self, session_id: str) -> Iterator[sqlite3.Connection]:
        if not self.paths.session_db(session_id).exists():
            raise ValueError(f"Unknown session: {session_id}")
        with self._connection(session_id) as connection:
            yield connection

    @staticmethod
    def _assert_not_running(connection: sqlite3.Connection) -> None:
        if connection.execute("SELECT 1 FROM session_runs WHERE status='running' LIMIT 1").fetchone() is not None:
            raise RuntimeError("The session has a running turn.")

    @staticmethod
    def _empty_import_runtime(session_id: str, messages: list) -> RuntimeState:
        return RuntimeState(session_id=session_id, messages=list(messages))

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
