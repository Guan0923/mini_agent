"""PostgreSQL schema creation for sessions and checkpoints."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from .database import PostgresDatabase


class PostgresSchemaMixin:
    def __init__(self, database_url: str | None = None, *, database: PostgresDatabase | None = None) -> None:
        self._database = database or PostgresDatabase.shared(database_url)
        self._owns_database = False
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection]:
        with self._database.connection() as connection:
            yield connection

    def close(self) -> None:
        """Close a privately-owned pool; injected pools remain application-owned."""

        if self._owns_database:
            self._database.close()

    def _initialize(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                reason TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS checkpoints_run_id_idx ON checkpoints (run_id, id)",
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                client_id TEXT,
                archived_at TEXT,
                deleted_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS session_runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                task TEXT NOT NULL,
                status TEXT NOT NULL,
                workflow_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                origin_kind TEXT NOT NULL DEFAULT 'legacy',
                source_session_id TEXT,
                source_run_id TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS workflow_id TEXT",
            "ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS origin_kind TEXT NOT NULL DEFAULT 'legacy'",
            "ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS source_session_id TEXT",
            "ALTER TABLE session_runs ADD COLUMN IF NOT EXISTS source_run_id TEXT",
            "UPDATE session_runs SET workflow_id = run_id WHERE workflow_id IS NULL",
            """
            CREATE TABLE IF NOT EXISTS session_messages (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                run_id TEXT NOT NULL REFERENCES session_runs(run_id),
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS session_runtime (
                session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS session_runtime_messages (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                run_id TEXT NOT NULL REFERENCES session_runs(run_id),
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            )
            """,
            "CREATE INDEX IF NOT EXISTS session_messages_session_idx ON session_messages (session_id, id)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS session_messages_assistant_run_idx
            ON session_messages (run_id) WHERE role = 'assistant'
            """,
            "CREATE INDEX IF NOT EXISTS session_runs_session_idx ON session_runs (session_id, updated_at)",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS session_runs_workflow_attempt_idx
            ON session_runs (workflow_id, attempt) WHERE workflow_id IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS session_runtime_messages_session_run_idx
            ON session_runtime_messages (session_id, run_id, sequence)
            """,
        )
        with self._connect() as connection:
            migration_table = connection.execute("SELECT to_regclass('public.schema_migrations')").fetchone()
            if migration_table is None or migration_table[0] is None:
                connection.execute(
                    """CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"""
                )
            applied = {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations").fetchall()}
            if 1 not in applied:
                for statement in statements:
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (1, CURRENT_TIMESTAMP)")
            if 2 not in applied:
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS session_messages_page_idx ON session_messages (session_id, id DESC)"
                )
                connection.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (2, CURRENT_TIMESTAMP)")
            if 3 not in applied:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS run_runtime_messages (
                    run_id TEXT NOT NULL REFERENCES runs(run_id), sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY (run_id, sequence))"""
                )
                connection.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (3, CURRENT_TIMESTAMP)")
            if 4 not in applied:
                for statement in (
                    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS client_id TEXT",
                    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS archived_at TEXT",
                    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS deleted_at TEXT",
                ):
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (4, CURRENT_TIMESTAMP)")
