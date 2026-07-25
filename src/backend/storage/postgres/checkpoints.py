"""PostgreSQL adapter for runtime checkpoints."""

from __future__ import annotations

import json

from backend.domain import RunState
from backend.domain.state import utc_now
from backend.runtime.core.context import AgentRuntime, RuntimeState

from .schema import PostgresSchemaMixin


class PostgresCheckpointStore(PostgresSchemaMixin):
    """Store the latest run snapshot and its ordered checkpoint history."""

    def save(self, runtime: AgentRuntime | RunState, reason: str) -> None:
        if isinstance(runtime, RunState):
            state = runtime
            payload = json.dumps(state.to_dict(), ensure_ascii=False)
        else:
            state = runtime.run
            payload = json.dumps(runtime.state.to_dict(), ensure_ascii=False)
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

    def load(self, run_id: str) -> RunState | None:
        with self._connect() as connection:
            row = connection.execute("SELECT state_json FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        if "session_id" in payload:
            return RuntimeState.from_dict(payload).current_run
        return RunState.from_dict(payload)

    def load_runtime_state(self, run_id: str) -> RuntimeState | None:
        with self._connect() as connection:
            row = connection.execute("SELECT state_json FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        return RuntimeState.from_dict(payload) if "session_id" in payload else None

    def checkpoint_count(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id = %s", (run_id,)).fetchone()
        assert row is not None
        return int(row[0])
