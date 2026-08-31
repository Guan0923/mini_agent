"""The sole supported SQLite session-store schema."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 16
UNSUPPORTED_SCHEMA_MESSAGE = "Unsupported state.db schema; Mini-Agent requires v16 and left the database untouched."

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS store_metadata (
    session_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL CHECK (schema_version = {SCHEMA_VERSION}),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS runtime_threads (
    session_id TEXT NOT NULL,
    thread_id TEXT PRIMARY KEY,
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('main','fork','subagent')),
    current_turn_id TEXT,
    running_turn_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runtime_threads_session_idx
    ON runtime_threads (session_id, created_at, thread_id);

CREATE TABLE IF NOT EXISTS thread_nodes (
    session_id TEXT NOT NULL,
    thread_id TEXT PRIMARY KEY REFERENCES runtime_threads(thread_id) ON DELETE CASCADE,
    root_thread_id TEXT NOT NULL REFERENCES thread_nodes(thread_id),
    parent_thread_id TEXT REFERENCES thread_nodes(thread_id),
    thread_path TEXT NOT NULL,
    depth INTEGER NOT NULL CHECK (depth >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (session_id, root_thread_id, thread_path)
);
CREATE INDEX IF NOT EXISTS thread_nodes_parent_idx
    ON thread_nodes (session_id, parent_thread_id, created_at, thread_id);

CREATE TABLE IF NOT EXISTS thread_contexts (
    thread_id TEXT PRIMARY KEY REFERENCES thread_nodes(thread_id) ON DELETE CASCADE,
    requested_strategy TEXT NOT NULL CHECK (requested_strategy IN ('share','compaction_share','independent')),
    effective_strategy TEXT NOT NULL CHECK (effective_strategy IN ('share','compaction_share','independent')),
    source_turn_id TEXT NOT NULL,
    source_data_idx INTEGER NOT NULL CHECK (source_data_idx >= 0),
    snapshot_json TEXT CHECK (snapshot_json IS NULL OR json_valid(snapshot_json)),
    summary TEXT
);

CREATE TABLE IF NOT EXISTS agent_turn_reports (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    agent_thread_id TEXT NOT NULL REFERENCES runtime_threads(thread_id) ON DELETE CASCADE,
    recipient_thread_id TEXT NOT NULL REFERENCES runtime_threads(thread_id) ON DELETE CASCADE,
    thread_status TEXT CHECK (thread_status IS NULL OR thread_status IN ('success','failed')),
    reply_content TEXT NOT NULL DEFAULT '',
    delivery_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('waiting','queued','delivered')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (turn_id, recipient_thread_id)
);
CREATE INDEX IF NOT EXISTS agent_turn_reports_pending_idx
    ON agent_turn_reports (session_id, state, created_at, delivery_id);
"""

_TABLES = {
    "store_metadata",
    "json_objects",
    "workspace_files",
    "sandbox_approvals",
    "runtime_threads",
    "thread_nodes",
    "thread_contexts",
    "agent_turn_reports",
}

_COLUMNS = {
    "store_metadata": {"session_id", "schema_version", "created_at", "updated_at"},
    "json_objects": {"session_id", "namespace", "object_id", "payload_json", "updated_at"},
    "workspace_files": {"session_id", "relative_path", "size", "sha256", "mtime_ns"},
    "sandbox_approvals": {
        "request_hash",
        "session_id",
        "command_hash",
        "cwd_hash",
        "permission_target",
        "network_target_hash",
        "command_summary",
        "cwd_summary",
        "created_at",
    },
    "runtime_threads": {
        "session_id",
        "thread_id",
        "origin_kind",
        "current_turn_id",
        "running_turn_id",
        "created_at",
        "updated_at",
    },
    "thread_nodes": {
        "session_id",
        "thread_id",
        "root_thread_id",
        "parent_thread_id",
        "thread_path",
        "depth",
        "created_at",
        "updated_at",
    },
    "thread_contexts": {
        "thread_id",
        "requested_strategy",
        "effective_strategy",
        "source_turn_id",
        "source_data_idx",
        "snapshot_json",
        "summary",
    },
    "agent_turn_reports": {
        "session_id",
        "turn_id",
        "agent_thread_id",
        "recipient_thread_id",
        "thread_status",
        "reply_content",
        "delivery_id",
        "state",
        "created_at",
        "updated_at",
    },
}


class SQLiteSchemaMixin:
    """Accept only the current schema; no legacy database is mutated."""

    @staticmethod
    def _assert_supported_schema(connection: sqlite3.Connection) -> None:
        cursor = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = {str(row[0]) for row in cursor} if cursor is not None else set()
        if not tables:
            return
        SQLiteSchemaMixin._assert_schema_shape(connection, tables)
        row = connection.execute("SELECT schema_version FROM store_metadata LIMIT 1").fetchone()
        if row is None or int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)

    @staticmethod
    def _assert_schema_shape(connection: sqlite3.Connection, tables: set[str] | None = None) -> None:
        actual_tables = tables
        if actual_tables is None:
            actual_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        if actual_tables != _TABLES:
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)
        for table, expected in _COLUMNS.items():
            columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            if columns != expected:
                raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)

    @staticmethod
    def _prepare_schema(connection: sqlite3.Connection) -> None:
        del connection

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        SQLiteSchemaMixin._assert_schema_shape(connection)
        row = connection.execute("SELECT schema_version FROM store_metadata LIMIT 1").fetchone()
        if row is not None and int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)


__all__ = ["SCHEMA", "SCHEMA_VERSION", "SQLiteSchemaMixin"]
