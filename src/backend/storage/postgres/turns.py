"""PostgreSQL persistence for conversation turns and runtime audit messages."""

from __future__ import annotations

import json

from backend.domain import DEFAULT_SESSION_TITLE, RunProvenance, RunStatus, RuntimeMessage
from backend.domain.state import utc_now


class PostgresTurnMixin:
    """Store turn projections and ordered presentation-independent events."""

    def append_runtime_message(self, session_id: str, run_id: str, message: RuntimeMessage) -> None:
        payload = json.dumps(message.data, ensure_ascii=False, default=str)
        with self._connect() as connection:
            run = connection.execute(
                "SELECT 1 FROM session_runs WHERE run_id = %s AND session_id = %s", (run_id, session_id)
            ).fetchone()
            if run is None:
                raise ValueError(f"Unknown session run: {run_id}")
            self._insert_runtime_message(connection, session_id, run_id, message, payload)
            connection.execute(
                "UPDATE sessions SET updated_at = %s WHERE session_id = %s", (message.timestamp, session_id)
            )

    @classmethod
    def _save_latest_runtime_message(
        cls, connection, session_id: str, run_id: str, messages: list[RuntimeMessage]
    ) -> None:
        if not messages:
            return
        message = messages[-1]
        cls._insert_runtime_message(
            connection, session_id, run_id, message, json.dumps(message.data, ensure_ascii=False, default=str)
        )

    @staticmethod
    def _insert_runtime_message(
        connection, session_id: str, run_id: str, message: RuntimeMessage, payload: str
    ) -> None:
        connection.execute(
            """INSERT INTO session_runtime_messages
                (session_id, run_id, sequence, kind, message, data_json, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, sequence) DO UPDATE SET
                kind = EXCLUDED.kind, message = EXCLUDED.message, data_json = EXCLUDED.data_json,
                created_at = EXCLUDED.created_at""",
            (session_id, run_id, message.sequence, message.kind, message.message, payload, message.timestamp),
        )

    @classmethod
    def _save_runtime_messages(cls, connection, session_id: str, run_id: str, messages: list[RuntimeMessage]) -> None:
        for message in messages:
            cls._insert_runtime_message(
                connection,
                session_id,
                run_id,
                message,
                json.dumps(message.data, ensure_ascii=False, default=str),
            )

    def load_runtime_messages(self, session_id: str, run_id: str | None = None) -> list[RuntimeMessage]:
        query = """SELECT m.sequence, m.kind, m.message, m.data_json, m.created_at
            FROM session_runtime_messages AS m
            JOIN session_runs AS r ON r.run_id = m.run_id
            WHERE m.session_id = %s"""
        parameters: list[object] = [session_id]
        if run_id is not None:
            query += " AND m.run_id = %s ORDER BY m.sequence ASC"
            parameters.append(run_id)
        else:
            query += " ORDER BY r.started_at ASC, m.sequence ASC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            RuntimeMessage(
                sequence=int(sequence),
                kind=str(kind),
                message=str(message),
                timestamp=str(timestamp),
                data=dict(payload) if isinstance(payload := json.loads(str(data_json)), dict) else {},
            )
            for sequence, kind, message, data_json, timestamp in rows
        ]

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
        with self._connect() as connection:
            session = connection.execute("SELECT title FROM sessions WHERE session_id = %s", (session_id,)).fetchone()
            if session is None:
                raise ValueError(f"Unknown session: {session_id}")
            title = session[0]
            if title == DEFAULT_SESSION_TITLE:
                has_messages = connection.execute(
                    "SELECT 1 FROM session_messages WHERE session_id = %s LIMIT 1", (session_id,)
                ).fetchone()
                if has_messages is None:
                    title = self._clean_title(task)
            connection.execute(
                """INSERT INTO session_runs (
                    run_id, session_id, task, status, workflow_id, attempt, origin_kind,
                    source_session_id, source_run_id, started_at, updated_at
                ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    session_id = EXCLUDED.session_id, task = EXCLUDED.task, status = 'running',
                    workflow_id = EXCLUDED.workflow_id, attempt = EXCLUDED.attempt, origin_kind = EXCLUDED.origin_kind,
                    source_session_id = EXCLUDED.source_session_id, source_run_id = EXCLUDED.source_run_id,
                    updated_at = EXCLUDED.updated_at""",
                (
                    run_id,
                    session_id,
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
                    """INSERT INTO session_messages (session_id, run_id, role, content, created_at)
                    SELECT %s, %s, 'user', %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM session_messages WHERE run_id = %s AND role = 'user'
                    )""",
                    (session_id, run_id, task, timestamp, run_id),
                )
            connection.execute(
                "UPDATE sessions SET title = %s, updated_at = %s WHERE session_id = %s", (title, timestamp, session_id)
            )

    def append_turn_input(self, session_id: str, run_id: str, content: str) -> None:
        timestamp = utc_now()
        with self._connect() as connection:
            run = connection.execute(
                "SELECT 1 FROM session_runs WHERE run_id = %s AND session_id = %s", (run_id, session_id)
            ).fetchone()
            if run is None:
                raise ValueError(f"Unknown session run: {run_id}")
            connection.execute(
                """INSERT INTO session_messages (session_id, run_id, role, content, created_at)
                VALUES (%s, %s, 'user', %s, %s)""",
                (session_id, run_id, content, timestamp),
            )
            connection.execute("UPDATE sessions SET updated_at = %s WHERE session_id = %s", (timestamp, session_id))

    def finish_turn(self, session_id: str, run_id: str, status: RunStatus, answer: str | None) -> None:
        timestamp = utc_now()
        assistant_content = self._assistant_content(status, answer)
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE session_runs SET status = %s, updated_at = %s WHERE run_id = %s AND session_id = %s",
                (status, timestamp, run_id, session_id),
            )
            if updated.rowcount == 0:
                raise ValueError(f"Unknown session run: {run_id}")
            assistant = connection.execute(
                "UPDATE session_messages SET content = %s, created_at = %s WHERE run_id = %s AND role = 'assistant'",
                (assistant_content, timestamp, run_id),
            )
            if assistant.rowcount == 0:
                connection.execute(
                    """INSERT INTO session_messages (session_id, run_id, role, content, created_at)
                    VALUES (%s, %s, 'assistant', %s, %s)""",
                    (session_id, run_id, assistant_content, timestamp),
                )
            connection.execute("UPDATE sessions SET updated_at = %s WHERE session_id = %s", (timestamp, session_id))
