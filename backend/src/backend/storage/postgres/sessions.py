"""PostgreSQL adapter for session metadata and conversation projections."""

from __future__ import annotations

from backend.domain import Session, SessionSummary, new_session_id
from backend.domain.state import utc_now

from .mapping import SessionMappingMixin
from .runtime_snapshots import PostgresRuntimeMixin
from .schema import PostgresSchemaMixin
from .turns import PostgresTurnMixin


class PostgresSessionStore(PostgresRuntimeMixin, PostgresTurnMixin, PostgresSchemaMixin, SessionMappingMixin):
    """Store session metadata, messages, snapshots, and audit traces in PostgreSQL."""

    def create_session(self, title: str | None = None) -> Session:
        session = Session(
            session_id=new_session_id(),
            title=self._clean_title(title),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO sessions (session_id, title, created_at, updated_at)
                VALUES (%s, %s, %s, %s)""",
                (session.session_id, session.title, session.created_at, session.updated_at),
            )
        return session

    def get_session(self, session_id: str) -> Session | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_id, title, created_at, updated_at FROM sessions WHERE session_id = %s", (session_id,)
            ).fetchone()
        return self._session_from_row(row) if row else None

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        with self._connect() as connection:
            row = connection.execute(self._summary_query("WHERE s.session_id = %s"), (session_id,)).fetchone()
        return self._summary_from_row(row) if row else None

    def list_sessions(self) -> list[SessionSummary]:
        with self._connect() as connection:
            rows = connection.execute(self._summary_query("") + " ORDER BY s.updated_at DESC").fetchall()
        return [self._summary_from_row(row) for row in rows]

    def latest_session(self) -> Session | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, title, created_at, updated_at
                FROM sessions
                ORDER BY updated_at DESC, session_id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._session_from_row(row) if row else None

    def load_conversation(self, session_id: str) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT role, content FROM session_messages
                WHERE session_id = %s ORDER BY id ASC""",
                (session_id,),
            ).fetchall()
        return [{"role": row[0], "content": row[1]} for row in rows]

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
