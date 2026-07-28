"""SQLite schema creation and in-place compatibility migrations."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT PRIMARY KEY, title TEXT NOT NULL, owner_device_id TEXT NOT NULL,
    remote_revision INTEGER NOT NULL DEFAULT 0, read_only INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_runs (
    run_id TEXT PRIMARY KEY, task TEXT NOT NULL, status TEXT NOT NULL, workflow_id TEXT,
    attempt INTEGER NOT NULL, origin_kind TEXT NOT NULL, source_session_id TEXT,
    source_run_id TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_runtime (
    session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL, state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, reason TEXT NOT NULL,
    state_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_messages (
    run_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
    data_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT UNIQUE, base_revision INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'snapshot', payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
    acknowledged_at TEXT
);
"""


class SQLiteSchemaMixin:
    """Upgrade older per-session databases before normal access."""

    device_id: str

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        meta_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(session_meta)")}
        if "owner_device_id" not in meta_columns:
            connection.execute("ALTER TABLE session_meta ADD COLUMN owner_device_id TEXT NOT NULL DEFAULT ''")
        for name, definition in (
            ("remote_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("read_only", "INTEGER NOT NULL DEFAULT 0"),
            ("schema_version", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in meta_columns:
                connection.execute(f"ALTER TABLE session_meta ADD COLUMN {name} {definition}")
        connection.execute(
            "UPDATE session_meta SET owner_device_id=? WHERE owner_device_id IS NULL OR owner_device_id=''",
            (self.device_id,),
        )
        outbox_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sync_outbox)")}
        for name, definition in (
            ("operation_id", "TEXT"),
            ("base_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("kind", "TEXT NOT NULL DEFAULT 'snapshot'"),
            ("acknowledged_at", "TEXT"),
        ):
            if name not in outbox_columns:
                connection.execute(f"ALTER TABLE sync_outbox ADD COLUMN {name} {definition}")
        legacy_outbox = connection.execute("SELECT 1 FROM sync_outbox WHERE operation_id IS NULL LIMIT 1").fetchone()
        if legacy_outbox is not None:
            connection.execute("DELETE FROM sync_outbox WHERE operation_id IS NULL")
            meta = connection.execute("SELECT session_id,read_only FROM session_meta").fetchone()
            if meta is not None and not int(meta[1]):
                self._queue(connection, str(meta[0]))
