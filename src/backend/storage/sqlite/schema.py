"""SQLite session schema creation and migration."""

from __future__ import annotations

import sqlite3


class SessionSchemaMixin:
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    workflow_id TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    origin_kind TEXT NOT NULL DEFAULT 'legacy',
                    source_session_id TEXT,
                    source_run_id TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
                """
            )
            self._ensure_session_runs_schema(connection)
            self._ensure_session_messages_schema(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_runtime (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_runtime_messages (
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY (run_id) REFERENCES session_runs(run_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS session_messages_session_idx ON session_messages (session_id, id)"
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS session_messages_assistant_run_idx
                ON session_messages (run_id) WHERE role = 'assistant'
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS session_runs_session_idx ON session_runs (session_id, updated_at)"
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS session_runs_workflow_attempt_idx
                ON session_runs (workflow_id, attempt) WHERE workflow_id IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS session_runtime_messages_session_run_idx
                ON session_runtime_messages (session_id, run_id, sequence)
                """
            )

    @staticmethod
    def _ensure_session_runs_schema(connection: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(session_runs)").fetchall()}
        additions = {
            "workflow_id": "TEXT",
            "attempt": "INTEGER NOT NULL DEFAULT 1",
            "origin_kind": "TEXT NOT NULL DEFAULT 'legacy'",
            "source_session_id": "TEXT",
            "source_run_id": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE session_runs ADD COLUMN {name} {definition}")
        connection.execute("UPDATE session_runs SET workflow_id = run_id WHERE workflow_id IS NULL")

    @staticmethod
    def _ensure_session_messages_schema(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'session_messages'"
        ).fetchone()
        if row is None:
            SessionSchemaMixin._create_session_messages_table(connection)
            return
        definition = " ".join(str(row[0]).lower().split())
        if "unique (run_id, role)" not in definition and "unique(run_id, role)" not in definition:
            return

        connection.execute("ALTER TABLE session_messages RENAME TO session_messages_legacy")
        SessionSchemaMixin._create_session_messages_table(connection)
        connection.execute(
            """
            INSERT INTO session_messages (id, session_id, run_id, role, content, created_at)
            SELECT id, session_id, run_id, role, content, created_at
            FROM session_messages_legacy
            ORDER BY id
            """
        )
        connection.execute("DROP TABLE session_messages_legacy")

    @staticmethod
    def _create_session_messages_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id),
                FOREIGN KEY (run_id) REFERENCES session_runs(run_id)
            )
            """
        )
