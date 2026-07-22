"""SQLite adapters for runtime checkpoints and durable conversations."""

from __future__ import annotations

import json
from pathlib import Path

from mini_agent.domain import (
    DEFAULT_SESSION_TITLE,
    RunStatus,
    RuntimeMessage,
    Session,
    SessionSummary,
    new_session_id,
)
from mini_agent.domain.state import utc_now
from mini_agent.runtime.core.context import RuntimeState

from .mapping import SessionMappingMixin
from .schema import SessionSchemaMixin


class SQLiteSessionStore(SessionSchemaMixin, SessionMappingMixin):
    """Store session metadata and chat messages in the checkpoint database."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._initialize()

    def create_session(self, title: str | None = None) -> Session:
        session = Session(
            session_id=new_session_id(),
            title=self._clean_title(title),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (session_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (session.session_id, session.title, session.created_at, session.updated_at),
            )
        return session

    def get_session(self, session_id: str) -> Session | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_id, title, created_at, updated_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._session_from_row(row) if row else None

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        with self._connect() as connection:
            row = connection.execute(self._summary_query("WHERE s.session_id = ?"), (session_id,)).fetchone()
        return self._summary_from_row(row) if row else None

    def list_sessions(self) -> list[SessionSummary]:
        with self._connect() as connection:
            rows = connection.execute(self._summary_query("") + " ORDER BY s.updated_at DESC").fetchall()
        return [self._summary_from_row(row) for row in rows]

    def load_conversation(self, session_id: str) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content
                FROM session_messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()
        return [{"role": row[0], "content": row[1]} for row in rows]

    def save_runtime(self, state: RuntimeState) -> None:
        payload = json.dumps(state.to_dict(), ensure_ascii=False)
        timestamp = utc_now()
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM sessions WHERE session_id = ?", (state.session_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Unknown session: {state.session_id}")
            connection.execute(
                """
                INSERT INTO session_runtime (session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (state.session_id, payload, timestamp),
            )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (timestamp, state.session_id))

    def load_runtime(self, session_id: str) -> RuntimeState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM session_runtime WHERE session_id = ?", (session_id,)
            ).fetchone()
        return RuntimeState.from_dict(json.loads(row[0])) if row else None

    def append_runtime_message(self, session_id: str, run_id: str, message: RuntimeMessage) -> None:
        """Persist one canonical runtime message as soon as it is emitted."""

        payload = json.dumps(message.data, ensure_ascii=False, default=str)
        with self._connect() as connection:
            run = connection.execute(
                "SELECT 1 FROM session_runs WHERE run_id = ? AND session_id = ?",
                (run_id, session_id),
            ).fetchone()
            if run is None:
                raise ValueError(f"Unknown session run: {run_id}")
            connection.execute(
                """
                INSERT INTO session_runtime_messages
                    (session_id, run_id, sequence, kind, message, data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, sequence) DO UPDATE SET
                    kind = excluded.kind,
                    message = excluded.message,
                    data_json = excluded.data_json,
                    created_at = excluded.created_at
                """,
                (
                    session_id,
                    run_id,
                    message.sequence,
                    message.kind,
                    message.message,
                    payload,
                    message.timestamp,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (message.timestamp, session_id),
            )

    def load_runtime_messages(self, session_id: str, run_id: str | None = None) -> list[RuntimeMessage]:
        """Load the ordered audit trail without changing the LLM conversation projection."""

        query = """
            SELECT m.sequence, m.kind, m.message, m.data_json, m.created_at
            FROM session_runtime_messages AS m
            JOIN session_runs AS r ON r.run_id = m.run_id
            WHERE m.session_id = ?
        """
        parameters: list[object] = [session_id]
        if run_id is not None:
            query += " AND m.run_id = ? ORDER BY m.sequence ASC"
            parameters.append(run_id)
        else:
            query += " ORDER BY r.started_at ASC, m.sequence ASC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        messages: list[RuntimeMessage] = []
        for sequence, kind, message, data_json, timestamp in rows:
            payload = json.loads(str(data_json))
            messages.append(
                RuntimeMessage(
                    sequence=int(sequence),
                    kind=str(kind),
                    message=str(message),
                    timestamp=str(timestamp),
                    data=dict(payload) if isinstance(payload, dict) else {},
                )
            )
        return messages

    def start_turn(self, session_id: str, run_id: str, task: str) -> None:
        timestamp = utc_now()
        with self._connect() as connection:
            session = connection.execute(
                "SELECT title FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise ValueError(f"Unknown session: {session_id}")

            title = session[0]
            if title == DEFAULT_SESSION_TITLE:
                has_messages = connection.execute(
                    "SELECT 1 FROM session_messages WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
                if has_messages is None:
                    title = self._clean_title(task)

            connection.execute(
                """
                INSERT INTO session_runs (run_id, session_id, task, status, started_at, updated_at)
                VALUES (?, ?, ?, 'running', ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    task = excluded.task,
                    status = 'running',
                    updated_at = excluded.updated_at
                """,
                (run_id, session_id, task, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO session_messages (session_id, run_id, role, content, created_at)
                SELECT ?, ?, 'user', ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM session_messages WHERE run_id = ? AND role = 'user'
                )
                """,
                (session_id, run_id, task, timestamp, run_id),
            )
            connection.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (title, timestamp, session_id),
            )

    def append_turn_input(self, session_id: str, run_id: str, content: str) -> None:
        timestamp = utc_now()
        with self._connect() as connection:
            run = connection.execute(
                "SELECT 1 FROM session_runs WHERE run_id = ? AND session_id = ?",
                (run_id, session_id),
            ).fetchone()
            if run is None:
                raise ValueError(f"Unknown session run: {run_id}")
            connection.execute(
                """
                INSERT INTO session_messages (session_id, run_id, role, content, created_at)
                VALUES (?, ?, 'user', ?, ?)
                """,
                (session_id, run_id, content, timestamp),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (timestamp, session_id),
            )

    def finish_turn(self, session_id: str, run_id: str, status: RunStatus, answer: str | None) -> None:
        timestamp = utc_now()
        assistant_content = self._assistant_content(status, answer)
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE session_runs
                SET status = ?, updated_at = ?
                WHERE run_id = ? AND session_id = ?
                """,
                (status, timestamp, run_id, session_id),
            )
            if updated.rowcount == 0:
                raise ValueError(f"Unknown session run: {run_id}")
            assistant = connection.execute(
                """
                UPDATE session_messages
                SET content = ?, created_at = ?
                WHERE run_id = ? AND role = 'assistant'
                """,
                (assistant_content, timestamp, run_id),
            )
            if assistant.rowcount == 0:
                connection.execute(
                    """
                    INSERT INTO session_messages (session_id, run_id, role, content, created_at)
                    VALUES (?, ?, 'assistant', ?, ?)
                    """,
                    (session_id, run_id, assistant_content, timestamp),
                )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (timestamp, session_id),
            )
