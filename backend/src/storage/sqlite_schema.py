"""Schema and the one supported v12 to v13 session-store migration."""

from __future__ import annotations

import json
import sqlite3

SCHEMA_VERSION = 13
PREVIOUS_SCHEMA_VERSION = 12
UNSUPPORTED_SCHEMA_MESSAGE = (
    "Unsupported state.db schema; Mini-Agent requires v12 or v13 and left the database untouched."
)

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
    parent_thread_id TEXT REFERENCES thread_nodes(thread_id),
    thread_path TEXT NOT NULL,
    thread_task TEXT NOT NULL,
    thread_status TEXT NOT NULL CHECK (thread_status IN ('opening','closed')),
    depth INTEGER NOT NULL CHECK (depth >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (session_id, thread_path)
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
"""


_V12_TABLES = {"store_metadata", "json_objects", "workspace_files", "sandbox_approvals"}
_V13_TABLES = _V12_TABLES | {"runtime_threads", "thread_nodes", "thread_contexts"}


class SQLiteSchemaMixin:
    """Accept v13 and transactionally upgrade only the immediately previous schema."""

    @staticmethod
    def _assert_supported_schema(connection: sqlite3.Connection) -> None:
        cursor = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        # Keep the connection lifecycle defensive for test/instrumentation
        # connection objects that return no cursor from a probe.  A real
        # sqlite3 connection always returns a cursor here.
        tables = {str(row[0]) for row in cursor} if cursor is not None else set()
        if not tables:
            return
        if not tables.issubset(_V13_TABLES):
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)
        if "store_metadata" not in tables:
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(store_metadata)")}
        if not {"session_id", "schema_version", "created_at", "updated_at"}.issubset(columns):
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)
        row = connection.execute("SELECT schema_version FROM store_metadata LIMIT 1").fetchone()
        if row is not None and int(row[0]) not in {PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION}:
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)

    @staticmethod
    def _prepare_schema(connection: sqlite3.Connection) -> None:
        cursor = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='store_metadata'")
        exists = cursor.fetchone() if cursor is not None else None
        cursor = connection.execute("SELECT schema_version FROM store_metadata LIMIT 1") if exists else None
        row = cursor.fetchone() if cursor is not None else None
        if row is None or int(row[0]) == SCHEMA_VERSION:
            return
        if int(row[0]) != PREVIOUS_SCHEMA_VERSION:
            raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)
        SQLiteSchemaMixin._migrate_v12_to_v13(connection)

    @staticmethod
    def _migrate_v12_to_v13(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            metadata = connection.execute(
                "SELECT session_id,created_at,updated_at FROM store_metadata LIMIT 1"
            ).fetchone()
            if metadata is None:
                raise RuntimeError(UNSUPPORTED_SCHEMA_MESSAGE)
            session_id, created_at, updated_at = map(str, metadata)
            connection.execute(
                "CREATE TABLE store_metadata_v13 ("
                "session_id TEXT PRIMARY KEY,schema_version INTEGER NOT NULL CHECK(schema_version=13),"
                "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO store_metadata_v13 VALUES (?,?,?,?)",
                (session_id, SCHEMA_VERSION, created_at, updated_at),
            )
            connection.execute("DROP TABLE store_metadata")
            connection.execute("ALTER TABLE store_metadata_v13 RENAME TO store_metadata")
            SQLiteSchemaMixin._create_v13_thread_tables(connection)
            SQLiteSchemaMixin._backfill_threads(connection, session_id, created_at)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _create_v13_thread_tables(connection: sqlite3.Connection) -> None:
        statements = (
            "CREATE TABLE runtime_threads (session_id TEXT NOT NULL,thread_id TEXT PRIMARY KEY,"
            "origin_kind TEXT NOT NULL CHECK(origin_kind IN ('main','fork','subagent')),current_turn_id TEXT,"
            "running_turn_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE INDEX runtime_threads_session_idx ON runtime_threads(session_id,created_at,thread_id)",
            "CREATE TABLE thread_nodes (session_id TEXT NOT NULL,thread_id TEXT PRIMARY KEY "
            "REFERENCES runtime_threads(thread_id) ON DELETE CASCADE,parent_thread_id TEXT "
            "REFERENCES thread_nodes(thread_id),thread_path TEXT NOT NULL,thread_task TEXT NOT NULL,"
            "thread_status TEXT NOT NULL CHECK(thread_status IN ('opening','closed')),"
            "depth INTEGER NOT NULL CHECK(depth>=0),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
            "UNIQUE(session_id,thread_path))",
            "CREATE INDEX thread_nodes_parent_idx ON thread_nodes(session_id,parent_thread_id,created_at,thread_id)",
            "CREATE TABLE thread_contexts (thread_id TEXT PRIMARY KEY REFERENCES thread_nodes(thread_id) "
            "ON DELETE CASCADE,requested_strategy TEXT NOT NULL "
            "CHECK(requested_strategy IN ('share','compaction_share','independent')),"
            "effective_strategy TEXT NOT NULL CHECK(effective_strategy IN ('share','compaction_share','independent')),"
            "source_turn_id TEXT NOT NULL,source_data_idx INTEGER NOT NULL CHECK(source_data_idx>=0),"
            "snapshot_json TEXT CHECK(snapshot_json IS NULL OR json_valid(snapshot_json)),summary TEXT)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _backfill_threads(connection: sqlite3.Connection, session_id: str, created_at: str) -> None:
        rows = connection.execute(
            "SELECT namespace,object_id,payload_json,updated_at FROM json_objects "
            "WHERE session_id=? AND namespace IN ('runtime_node','sidebar_thread')",
            (session_id,),
        ).fetchall()
        nodes: list[dict[str, object]] = []
        thread_ids = {session_id}
        for namespace, _object_id, payload_json, _updated_at in rows:
            payload = json.loads(str(payload_json))
            if not isinstance(payload, dict):
                continue
            thread_id = str(payload.get("thread_id") or "")
            if thread_id:
                thread_ids.add(thread_id)
            if namespace == "runtime_node" and "data" in payload:
                nodes.append(payload)
        for thread_id in sorted(thread_ids):
            candidates = [node for node in nodes if node.get("thread_id") == thread_id]
            running = [node for node in candidates if node.get("status") == "running"]
            if len(running) > 1:
                raise RuntimeError("A thread may have only one running Turn.")
            parent_ids = {str(node.get("parent_id") or "") for node in candidates if node.get("parent_id")}
            leaves = [node for node in candidates if str(node.get("id") or "") not in parent_ids]
            selected = running or sorted(
                leaves or candidates, key=lambda item: (str(item.get("timestamp") or ""), str(item.get("id") or ""))
            )
            head = selected[-1] if selected else None
            timestamp = str(head.get("timestamp") or created_at) if head else created_at
            connection.execute(
                "INSERT INTO runtime_threads(session_id,thread_id,origin_kind,current_turn_id,running_turn_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    session_id,
                    thread_id,
                    "main" if thread_id == session_id else "fork",
                    str(head.get("id")) if head else None,
                    str(running[0].get("id")) if running else None,
                    timestamp,
                    timestamp,
                ),
            )
        main = sorted(
            [node for node in nodes if node.get("thread_id") == session_id],
            key=lambda item: (str(item.get("timestamp") or ""), str(item.get("id") or "")),
        )
        task = ""
        for node in main:
            try:
                item = node["data"][int(node.get("current_data_idx") or 0)][0]["content"][0]  # type: ignore[index]
                task = str(item.get("text") or "")
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            if task:
                break
        connection.execute(
            "INSERT INTO thread_nodes(session_id,thread_id,parent_thread_id,thread_path,thread_task,thread_status,depth,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'opening',0,?,?)",
            (session_id, session_id, None, "/root", task, created_at, created_at),
        )

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        """Validate the current schema after optional migration."""

        SQLiteSchemaMixin._assert_supported_schema(connection)


__all__ = ["PREVIOUS_SCHEMA_VERSION", "SCHEMA", "SCHEMA_VERSION", "SQLiteSchemaMixin"]
