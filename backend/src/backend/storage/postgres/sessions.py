"""PostgreSQL adapter for session metadata and conversation projections."""

from __future__ import annotations

from uuid import uuid4

from backend.domain import Session, SessionSummary, new_session_id
from backend.domain.state import utc_now

from .mapping import SessionMappingMixin
from .runtime_snapshots import PostgresRuntimeMixin
from .schema import PostgresSchemaMixin
from .turns import PostgresTurnMixin


class PostgresSessionStore(PostgresRuntimeMixin, PostgresTurnMixin, PostgresSchemaMixin, SessionMappingMixin):
    """Store session metadata, messages, snapshots, and audit traces in PostgreSQL."""

    def create_session(self, title: str | None = None, *, client_id: str | None = None) -> Session:
        session = Session(
            session_id=new_session_id(),
            title=self._clean_title(title),
            created_at=utc_now(),
            updated_at=utc_now(),
            client_id=client_id,
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO sessions (session_id, title, created_at, updated_at, client_id)
                VALUES (%s, %s, %s, %s, %s)""",
                (session.session_id, session.title, session.created_at, session.updated_at, session.client_id),
            )
        return session

    def get_session(self, session_id: str) -> Session | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_id, title, created_at, updated_at, client_id, archived_at, deleted_at "
                "FROM sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        return self._session_from_row(row) if row else None

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        with self._connect() as connection:
            row = connection.execute(self._summary_query("WHERE s.session_id = %s"), (session_id,)).fetchone()
        return self._summary_from_row(row) if row else None

    def list_sessions(self, *, state: str = "active") -> list[SessionSummary]:
        if state == "active":
            where = "WHERE s.archived_at IS NULL AND s.deleted_at IS NULL"
        elif state == "archived":
            where = "WHERE s.archived_at IS NOT NULL AND s.deleted_at IS NULL"
        elif state == "deleted":
            where = "WHERE s.deleted_at IS NOT NULL"
        elif state == "all":
            where = ""
        else:
            raise ValueError(f"Unknown session state: {state}")
        with self._connect() as connection:
            rows = connection.execute(self._summary_query(where) + " ORDER BY s.updated_at DESC").fetchall()
        return [self._summary_from_row(row) for row in rows]

    def latest_session(self) -> Session | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, title, created_at, updated_at, client_id, archived_at, deleted_at
                FROM sessions
                WHERE archived_at IS NULL AND deleted_at IS NULL
                ORDER BY updated_at DESC, session_id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._session_from_row(row) if row else None

    def find_session_by_client_id(self, client_id: str, *, include_deleted: bool = False) -> Session | None:
        deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_id, title, created_at, updated_at, client_id, archived_at, deleted_at "
                f"FROM sessions WHERE client_id = %s{deleted_clause} "
                "ORDER BY updated_at DESC LIMIT 1",
                (client_id,),
            ).fetchone()
        return self._session_from_row(row) if row else None

    def rename_session(self, session_id: str, title: str) -> Session:
        if not title.strip():
            raise ValueError("Session title cannot be empty.")
        with self._connect() as connection:
            row = connection.execute("SELECT deleted_at FROM sessions WHERE session_id=%s", (session_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[0] is not None:
                raise ValueError("Deleted sessions cannot be renamed.")
            running = connection.execute(
                "SELECT 1 FROM session_runs WHERE session_id=%s AND status='running' LIMIT 1", (session_id,)
            ).fetchone()
            if running is not None:
                raise RuntimeError("The session has a running turn.")
            connection.execute(
                "UPDATE sessions SET title=%s, updated_at=%s WHERE session_id=%s",
                (self._clean_title(title), utc_now(), session_id),
            )
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def set_client_id(self, session_id: str, client_id: str | None) -> Session:
        with self._connect() as connection:
            running = connection.execute(
                "SELECT 1 FROM session_runs WHERE session_id=%s AND status='running' LIMIT 1", (session_id,)
            ).fetchone()
            if running is not None:
                raise RuntimeError("The session has a running turn.")
            updated = connection.execute(
                "UPDATE sessions SET client_id=%s, updated_at=%s WHERE session_id=%s",
                (client_id, utc_now(), session_id),
            )
            if updated.rowcount == 0:
                raise ValueError(f"Unknown session: {session_id}")
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def archive_session(self, session_id: str) -> Session:
        return self._set_lifecycle(session_id, archived_at=utc_now())

    def restore_session(self, session_id: str) -> Session:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT archived_at, deleted_at FROM sessions WHERE session_id=%s", (session_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[1] is not None:
                raise ValueError("Deleted sessions cannot be restored.")
            running = connection.execute(
                "SELECT 1 FROM session_runs WHERE session_id=%s AND status='running' LIMIT 1", (session_id,)
            ).fetchone()
            if running is not None:
                raise RuntimeError("The session has a running turn.")
            connection.execute(
                "UPDATE sessions SET archived_at=NULL, updated_at=%s WHERE session_id=%s", (utc_now(), session_id)
            )
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def delete_session(self, session_id: str) -> Session:
        return self._set_lifecycle(session_id, deleted_at=utc_now())

    def _set_lifecycle(
        self, session_id: str, *, archived_at: str | None = None, deleted_at: str | None = None
    ) -> Session:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE session_id=%s",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            running = connection.execute(
                "SELECT 1 FROM session_runs WHERE session_id=%s AND status='running' LIMIT 1",
                (session_id,),
            ).fetchone()
            if running is not None:
                raise RuntimeError("The session has a running turn.")
            assignments = ["updated_at=%s"]
            values: list[object] = [utc_now()]
            if archived_at is not None:
                assignments.append("archived_at=%s")
                values.append(archived_at)
            if deleted_at is not None:
                assignments.append("deleted_at=%s")
                values.append(deleted_at)
            values.append(session_id)
            connection.execute(f"UPDATE sessions SET {', '.join(assignments)} WHERE session_id=%s", values)
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def load_conversation(self, session_id: str) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT role, content FROM session_messages
                WHERE session_id = %s ORDER BY id ASC""",
                (session_id,),
            ).fetchall()
        return [{"role": row[0], "content": row[1]} for row in rows]

    def load_conversation_records(self, session_id: str) -> list[dict[str, str | int | None]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, run_id, role, content, created_at FROM session_messages "
                "WHERE session_id = %s ORDER BY id ASC",
                (session_id,),
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

    def import_conversation(
        self,
        title: str | None,
        messages: list[dict[str, str]],
        *,
        client_id: str | None = None,
        force_new: bool = False,
    ) -> Session:
        if client_id and not force_new:
            existing = self.find_session_by_client_id(client_id)
            if existing is not None:
                return existing
        parsed = [
            {"role": str(item.get("role")), "content": str(item.get("content") or "")}
            for item in messages
            if item.get("role") in {"user", "assistant"}
        ]
        session = self.create_session(title, client_id=client_id)
        if not parsed:
            return session
        run_id = f"import_{uuid4().hex}"
        timestamp = utc_now()
        task = next((item["content"] for item in parsed if item["role"] == "user"), "legacy import")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO session_runs (
                    run_id, session_id, task, status, workflow_id, attempt, origin_kind,
                    source_session_id, source_run_id, started_at, updated_at
                ) VALUES (%s, %s, %s, 'completed', %s, 1, 'legacy', NULL, NULL, %s, %s)""",
                (run_id, session.session_id, task, run_id, timestamp, timestamp),
            )
            for item in parsed:
                connection.execute(
                    "INSERT INTO session_messages (session_id, run_id, role, content, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (session.session_id, run_id, item["role"], item["content"], timestamp),
                )
            connection.execute("UPDATE sessions SET updated_at=%s WHERE session_id=%s", (timestamp, session.session_id))
        refreshed = self.get_session(session.session_id)
        if refreshed is None:
            raise ValueError(f"Unknown session: {session.session_id}")
        return refreshed

    def load_conversation_page(
        self, session_id: str, *, before_id: int | None = None, limit: int = 100
    ) -> tuple[list[dict[str, str]], int | None]:
        """Return one cursor page in chronological display order."""

        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            if before_id is None:
                rows = connection.execute(
                    """SELECT id, role, content FROM session_messages
                    WHERE session_id = %s ORDER BY id DESC LIMIT %s""",
                    (session_id, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT id, role, content FROM session_messages
                    WHERE session_id = %s AND id < %s ORDER BY id DESC LIMIT %s""",
                    (session_id, before_id, limit + 1),
                ).fetchall()
        has_older = len(rows) > limit
        page_rows = rows[:limit]
        next_before_id = int(page_rows[-1][0]) if has_older and page_rows else None
        return ([{"role": str(row[1]), "content": str(row[2])} for row in reversed(page_rows)], next_before_id)
