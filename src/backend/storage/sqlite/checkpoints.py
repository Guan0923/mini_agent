"""SQLite adapters for runtime checkpoints and durable conversations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from backend.domain import (
    RunState,
)
from backend.domain.state import utc_now
from backend.runtime.core.context import AgentRuntime, RuntimeState


class SQLiteCheckpointStore:
    """Store the latest run snapshot and its ordered checkpoint history locally."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._initialize()

    def save(self, runtime: AgentRuntime | RunState, reason: str) -> None:
        if isinstance(runtime, RunState):
            state = runtime
            payload = json.dumps(state.to_dict(), ensure_ascii=False)
        else:
            state = runtime.run
            payload = json.dumps(runtime.state.to_dict(), ensure_ascii=False)
        timestamp = utc_now()
        with sqlite3.connect(self._database_path) as connection:
            connection.execute(
                """
                INSERT INTO runs (run_id, status, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (state.run_id, state.status, payload, timestamp),
            )
            connection.execute(
                "INSERT INTO checkpoints (run_id, reason, state_json, created_at) VALUES (?, ?, ?, ?)",
                (state.run_id, reason, payload, timestamp),
            )

    def load(self, run_id: str) -> RunState | None:
        with sqlite3.connect(self._database_path) as connection:
            row = connection.execute("SELECT state_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        if "session_id" in payload:
            return RuntimeState.from_dict(payload).current_run
        return RunState.from_dict(payload)

    def load_runtime_state(self, run_id: str) -> RuntimeState | None:
        with sqlite3.connect(self._database_path) as connection:
            row = connection.execute("SELECT state_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        return RuntimeState.from_dict(payload) if "session_id" in payload else None

    def checkpoint_count(self, run_id: str) -> int:
        """Return the number of durable snapshots for diagnostics and tests."""
        with sqlite3.connect(self._database_path) as connection:
            row = connection.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id = ?", (run_id,)).fetchone()
        assert row is not None
        return int(row[0])

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS checkpoints_run_id_idx ON checkpoints (run_id, id)")
