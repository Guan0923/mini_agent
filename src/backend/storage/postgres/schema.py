"""PostgreSQL schema creation for sessions and checkpoints."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg


class PostgresSchemaMixin:
    def __init__(self, database_url: str | None = None) -> None:
        configured_url = database_url or os.environ.get("TEST_DATABASE_URL")
        configured_url = configured_url or os.environ.get("DATABASE_URL", "")
        if not configured_url.strip():
            raise ValueError("DATABASE_URL must be configured for PostgreSQL storage.")
        self._database_url = configured_url
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection]:
        connection: psycopg.Connection | None = None
        last_error: psycopg.OperationalError | None = None
        for attempt in range(3):
            try:
                connection = psycopg.connect(self._database_url)
                break
            except psycopg.OperationalError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
        if connection is None:
            raise RuntimeError("Unable to connect to PostgreSQL using DATABASE_URL.") from last_error
        try:
            with connection:
                yield connection
        except psycopg.OperationalError as exc:
            raise RuntimeError("PostgreSQL operation failed.") from exc

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
                updated_at TEXT NOT NULL
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
            for statement in statements:
                connection.execute(statement)
