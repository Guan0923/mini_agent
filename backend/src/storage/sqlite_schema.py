"""Schema for the append-only JSON session store."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 12
UNSUPPORTED_SCHEMA_MESSAGE = "Unsupported state.db schema; Mini-Agent requires v12 and left the database untouched."

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS store_metadata (
    session_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = {SCHEMA_VERSION}),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Business values are stored only in payload_json.  The remaining columns
-- are object identity and ordering metadata used by SQLite indexes.
CREATE TABLE IF NOT EXISTS json_objects (
    session_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    object_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, namespace, object_id)
);
CREATE INDEX IF NOT EXISTS json_objects_session_idx
    ON json_objects (session_id, namespace, updated_at, object_id);

-- This table is only a content index; file bytes remain in the session workspace.
CREATE TABLE IF NOT EXISTS workspace_files (
    session_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    PRIMARY KEY (session_id, relative_path)
);
CREATE INDEX IF NOT EXISTS workspace_files_session_idx
    ON workspace_files (session_id, relative_path);

CREATE TABLE IF NOT EXISTS sandbox_approvals (
    request_hash TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    cwd_hash TEXT NOT NULL,
    permission_target TEXT NOT NULL,
    network_target_hash TEXT NOT NULL,
    command_summary TEXT NOT NULL,
    cwd_summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sandbox_approvals_session_idx
    ON sandbox_approvals (session_id, permission_target, request_hash);
"""


class SQLiteSchemaMixin:
    """Reject every database that does not use the local-only v12 schema."""

    @staticmethod
    def _assert_supported_schema(connection: sqlite3.Connection) -> None:
        cursor = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        # Keep the connection lifecycle defensive for test/instrumentation
        # connection objects that return no cursor from a probe.  A real
        # sqlite3 connection always returns a cursor here.
        tables = {str(row[0]) for row in cursor} if cursor is not None else set()
        if not tables:
            return
        allowed = {"store_metadata", "json_objects", "workspace_files", "sandbox_approvals"}
        if not tables.issubset(allowed):
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)
        if "store_metadata" not in tables:
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(store_metadata)")}
        if not {"session_id", "schema_version", "created_at", "updated_at"}.issubset(columns):
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)
        row = connection.execute("SELECT schema_version FROM store_metadata LIMIT 1").fetchone()
        if row is not None and int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        """Validate v12; intentionally perform no migration or backfill."""

        SQLiteSchemaMixin._assert_supported_schema(connection)


__all__ = ["SCHEMA", "SCHEMA_VERSION", "SQLiteSchemaMixin"]
