"""PostgreSQL adapter for runtime checkpoints."""

from __future__ import annotations

import json

from backend.domain import RunState
from backend.domain.state import utc_now
from backend.runtime.core.context import AgentRuntime, RuntimeState

from ..codec import decode_message_data, encode_message_data, encode_runtime_state
from .schema import PostgresSchemaMixin


class PostgresCheckpointStore(PostgresSchemaMixin):
    """Store the latest run snapshot and its ordered checkpoint history."""

    def save(self, runtime: AgentRuntime | RunState, reason: str) -> None:
        if isinstance(runtime, RunState):
            state = runtime
            payload = json.dumps(state.to_dict(include_runtime_messages=False), ensure_ascii=False)
        else:
            state = runtime.run
            payload = encode_runtime_state(runtime.state)
        timestamp = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (run_id, status, state_json, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    state_json = EXCLUDED.state_json,
                    updated_at = EXCLUDED.updated_at
                """,
                (state.run_id, state.status, payload, timestamp),
            )
            connection.execute(
                "INSERT INTO checkpoints (run_id, reason, state_json, created_at) VALUES (%s, %s, %s, %s)",
                (state.run_id, reason, payload, timestamp),
            )
            self._save_runtime_messages(connection, state.run_id, state.runtime_messages)

    def load(self, run_id: str) -> RunState | None:
        with self._connect() as connection:
            row = connection.execute("SELECT state_json FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        if "session_id" in payload:
            state = RuntimeState.from_dict(payload)
            if state.current_run is not None:
                state.current_run.runtime_messages = self._load_runtime_messages(run_id)
            return state.current_run
        state = RunState.from_dict(payload)
        state.runtime_messages = self._load_runtime_messages(run_id)
        return state

    def load_runtime_state(self, run_id: str) -> RuntimeState | None:
        with self._connect() as connection:
            row = connection.execute("SELECT state_json FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        if "session_id" not in payload:
            return None
        state = RuntimeState.from_dict(payload)
        if state.current_run is not None:
            state.current_run.runtime_messages = self._load_runtime_messages(run_id)
        return state

    @staticmethod
    def _save_runtime_messages(connection, run_id: str, messages) -> None:
        for message in messages:
            connection.execute(
                """INSERT INTO run_runtime_messages (run_id, sequence, kind, message, data_json, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, sequence) DO UPDATE SET kind = EXCLUDED.kind, message = EXCLUDED.message,
                data_json = EXCLUDED.data_json, created_at = EXCLUDED.created_at""",
                (
                    run_id,
                    message.sequence,
                    message.kind,
                    message.message,
                    encode_message_data(message.data),
                    message.timestamp,
                ),
            )

    def _load_runtime_messages(self, run_id: str):
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sequence, kind, message, data_json, created_at FROM run_runtime_messages
                WHERE run_id = %s ORDER BY sequence ASC""",
                (run_id,),
            ).fetchall()
        from backend.domain import RuntimeMessage

        return [
            RuntimeMessage(int(sequence), str(kind), str(message), str(created_at), decode_message_data(str(data_json)))
            for sequence, kind, message, data_json, created_at in rows
        ]

    def checkpoint_count(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id = %s", (run_id,)).fetchone()
        assert row is not None
        return int(row[0])
