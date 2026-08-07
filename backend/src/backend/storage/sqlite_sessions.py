"""Session metadata and transcript operations for SQLite storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from backend.domain import (
    Session,
    SessionSummary,
    message_from_dict,
    new_session_id,
)
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState

from .codec import normalize_session_title


class SQLiteSessionMixin:
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
