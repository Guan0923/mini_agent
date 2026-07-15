"""SQLite-backed checkpoints for Human-in-the-Loop agent runs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mini_agent.domain import RunState
from mini_agent.domain.state import utc_now


class SQLiteCheckpointStore:
    """Store the latest run snapshot and its ordered checkpoint history locally."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._initialize()

    def save(self, state: RunState, reason: str) -> None:
        payload = json.dumps(state.to_dict(), ensure_ascii=False)
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
        return RunState.from_dict(json.loads(row[0])) if row else None

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
