"""PostgreSQL persistence for durable runtime snapshots and resume transitions."""

from __future__ import annotations

from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState

from ..codec import decode_runtime_state, encode_runtime_state


class PostgresRuntimeMixin:
    """Persist current runtime state and atomic workflow transitions."""

    def save(self, runtime, reason: str) -> None:
        state = runtime.state
        run = runtime.run
        payload = self._snapshot_payload(state)
        timestamp = utc_now()
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM sessions WHERE session_id = %s", (state.session_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Unknown session: {state.session_id}")
            connection.execute(
                """INSERT INTO runs (run_id, status, state_json, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status, state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at
                """,
                (run.run_id, run.status, payload, timestamp),
            )
            connection.execute(
                "INSERT INTO checkpoints (run_id, reason, state_json, created_at) VALUES (%s, %s, %s, %s)",
                (run.run_id, reason, payload, timestamp),
            )
            connection.execute(
                """INSERT INTO session_runtime (session_id, state_json, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at
                """,
                (state.session_id, payload, timestamp),
            )
            connection.execute(
                "UPDATE session_runs SET status = %s, updated_at = %s WHERE run_id = %s AND session_id = %s",
                (run.status, timestamp, run.run_id, state.session_id),
            )
            self._save_latest_runtime_message(connection, state.session_id, run.run_id, run.runtime_messages)
            connection.execute(
                "UPDATE sessions SET updated_at = %s WHERE session_id = %s", (timestamp, state.session_id)
            )

    def save_runtime(self, state: RuntimeState) -> None:
        payload = self._snapshot_payload(state)
        timestamp = utc_now()
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM sessions WHERE session_id = %s", (state.session_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Unknown session: {state.session_id}")
            connection.execute(
                """INSERT INTO session_runtime (session_id, state_json, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at
                """,
                (state.session_id, payload, timestamp),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = %s WHERE session_id = %s", (timestamp, state.session_id)
            )

    def load_runtime(self, session_id: str) -> RuntimeState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM session_runtime WHERE session_id = %s", (session_id,)
            ).fetchone()
        if row is None:
            return None
        state = decode_runtime_state(row[0])
        if state.current_run is not None:
            state.current_run.runtime_messages = self.load_runtime_messages(session_id, state.current_run.run_id)
        return state

    def resume_runtime(self, source: RuntimeState, resumed: RuntimeState) -> None:
        """Atomically archive one attempt and install its resumed successor."""

        if source.session_id != resumed.session_id or source.current_run is None or resumed.current_run is None:
            raise ValueError("Resume transition must contain two attempts from the same session.")
        source_run = source.current_run
        resumed_run = resumed.current_run
        timestamp = utc_now()
        source_payload = self._snapshot_payload(source)
        resumed_payload = self._snapshot_payload(resumed)
        origin = resumed_run.provenance
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE session_runs SET status = %s, updated_at = %s WHERE run_id = %s AND session_id = %s",
                (source_run.status, timestamp, source_run.run_id, source.session_id),
            )
            if updated.rowcount == 0:
                raise ValueError(f"Unknown session run: {source_run.run_id}")
            connection.execute(
                """INSERT INTO runs (run_id, status, state_json, updated_at) VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status, state_json = EXCLUDED.state_json,
                updated_at = EXCLUDED.updated_at""",
                (source_run.run_id, source_run.status, source_payload, timestamp),
            )
            connection.execute(
                "INSERT INTO checkpoints (run_id, reason, state_json, created_at) VALUES (%s, %s, %s, %s)",
                (source_run.run_id, f"run_{source_run.status}", source_payload, timestamp),
            )
            self._save_runtime_messages(connection, source.session_id, source_run.run_id, source_run.runtime_messages)
            connection.execute(
                """INSERT INTO session_runs (
                    run_id, session_id, task, status, workflow_id, attempt, origin_kind,
                    source_session_id, source_run_id, started_at, updated_at
                ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s, %s, %s)""",
                (
                    resumed_run.run_id,
                    resumed.session_id,
                    resumed_run.task,
                    origin.workflow_id,
                    origin.attempt,
                    origin.trigger,
                    origin.source_session_id,
                    origin.source_run_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO runs (run_id, status, state_json, updated_at) VALUES (%s, %s, %s, %s)",
                (resumed_run.run_id, resumed_run.status, resumed_payload, timestamp),
            )
            connection.execute(
                "INSERT INTO checkpoints (run_id, reason, state_json, created_at) VALUES (%s, %s, %s, %s)",
                (resumed_run.run_id, "run_resumed", resumed_payload, timestamp),
            )
            self._save_runtime_messages(
                connection, resumed.session_id, resumed_run.run_id, resumed_run.runtime_messages
            )
            connection.execute(
                """INSERT INTO session_runtime (session_id, state_json, updated_at) VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at""",
                (resumed.session_id, resumed_payload, timestamp),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = %s WHERE session_id = %s", (timestamp, resumed.session_id)
            )

    @staticmethod
    def _snapshot_payload(state: RuntimeState) -> str:
        return encode_runtime_state(state)
