"""SQLite schema creation and the one-way RuntimeState node migration."""

from __future__ import annotations

import sqlite3

from backend.domain.runtime_state import create_root_node, session_root_id

SCHEMA_VERSION = 5
RUNTIME_NODE_SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT PRIMARY KEY, title TEXT NOT NULL, owner_device_id TEXT NOT NULL,
    remote_revision INTEGER NOT NULL DEFAULT 0, read_only INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 5, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    client_id TEXT, archived_at TEXT, deleted_at TEXT,
    local_only INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS runtime_nodes (
    session_id TEXT NOT NULL,
    parent_session_id TEXT NOT NULL DEFAULT '',
    id TEXT NOT NULL,
    parent_id TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL,
    first_kept_entry_id TEXT NOT NULL,
    compaction_idx TEXT NOT NULL,
    user TEXT NOT NULL DEFAULT '',
    provider_name TEXT NOT NULL DEFAULT '',
    model_json TEXT NOT NULL,
    permission_mode TEXT NOT NULL DEFAULT 'approval_for_me',
    running_mode TEXT NOT NULL DEFAULT 'agent',
    usage_json TEXT NOT NULL,
    cwd TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('failed', 'success', 'abort')),
    data_json TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
);
CREATE INDEX IF NOT EXISTS runtime_nodes_session_timestamp_idx
    ON runtime_nodes (session_id, timestamp, id);
CREATE INDEX IF NOT EXISTS runtime_nodes_parent_idx
    ON runtime_nodes (parent_session_id, parent_id, timestamp, id);
CREATE TABLE IF NOT EXISTS sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT UNIQUE, base_revision INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'snapshot', payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
    acknowledged_at TEXT
);
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
"""


class SQLiteSchemaMixin:
    """Upgrade older per-session databases before normal access."""

    device_id: str

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        runtime_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runtime_nodes)")}
        required_runtime_columns = {
            "provider_name",
            "model_json",
            "permission_mode",
            "running_mode",
            "usage_json",
        }
        meta_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(session_meta)")}
        prior_version_row = (
            connection.execute("SELECT schema_version FROM session_meta LIMIT 1").fetchone()
            if "schema_version" in meta_columns
            else None
        )
        prior_version = (
            int(prior_version_row[0]) if prior_version_row is not None and prior_version_row[0] is not None else 1
        )
        # RuntimeState 0.3 is a protocol break.  Preserve the existing one-way
        # rebuild for pre-v4 databases, but v4 -> v5 only adds the formal
        # session root and must retain the canonical message tree.
        runtime_rebuild_required = runtime_columns and (
            prior_version < RUNTIME_NODE_SCHEMA_VERSION or not required_runtime_columns.issubset(runtime_columns)
        )
        if runtime_rebuild_required:
            # Protocol 0.3 is a deliberate break: old message nodes are not
            # migrated.  Provider credentials live in the user settings DB and
            # are therefore unaffected by rebuilding this local table.
            connection.execute("DROP TABLE runtime_nodes")
            connection.executescript(
                """
                CREATE TABLE runtime_nodes (
                    session_id TEXT NOT NULL, parent_session_id TEXT NOT NULL DEFAULT '',
                    id TEXT NOT NULL, parent_id TEXT NOT NULL DEFAULT '', version TEXT NOT NULL,
                    first_kept_entry_id TEXT NOT NULL, compaction_idx TEXT NOT NULL,
                    user TEXT NOT NULL DEFAULT '', provider_name TEXT NOT NULL DEFAULT '',
                    model_json TEXT NOT NULL, permission_mode TEXT NOT NULL DEFAULT 'approval_for_me',
                    running_mode TEXT NOT NULL DEFAULT 'agent', usage_json TEXT NOT NULL,
                    cwd TEXT NOT NULL DEFAULT '', timestamp TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('failed', 'success', 'abort')),
                    data_json TEXT NOT NULL, PRIMARY KEY (session_id, id)
                );
                CREATE INDEX IF NOT EXISTS runtime_nodes_session_timestamp_idx
                    ON runtime_nodes (session_id, timestamp, id);
                CREATE INDEX IF NOT EXISTS runtime_nodes_parent_idx
                    ON runtime_nodes (parent_session_id, parent_id, timestamp, id);
                """
            )
        if "owner_device_id" not in meta_columns:
            connection.execute("ALTER TABLE session_meta ADD COLUMN owner_device_id TEXT NOT NULL DEFAULT ''")
        for name, definition in (
            ("remote_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("read_only", "INTEGER NOT NULL DEFAULT 0"),
            ("schema_version", f"INTEGER NOT NULL DEFAULT {SCHEMA_VERSION}"),
            ("client_id", "TEXT"),
            ("archived_at", "TEXT"),
            ("deleted_at", "TEXT"),
            ("local_only", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in meta_columns:
                connection.execute(f"ALTER TABLE session_meta ADD COLUMN {name} {definition}")
        connection.execute(
            "UPDATE session_meta SET schema_version=? WHERE schema_version < ?", (SCHEMA_VERSION, SCHEMA_VERSION)
        )
        connection.execute(
            "UPDATE session_meta SET owner_device_id=? WHERE owner_device_id IS NULL OR owner_device_id=''",
            (self.device_id,),
        )
        if prior_version < SCHEMA_VERSION or (runtime_columns and not required_runtime_columns.issubset(runtime_columns)):
            # Version 0.3 is a protocol break for the message tree itself:
            # the old ``runtime_nodes`` table is rebuilt and its rows are not
            # interpreted as v4 nodes.  The remaining tables are deliberately
            # retained as a local, non-authoritative execution projection.
            # They are still needed to resume an interrupted worker and to
            # identify a run for the fork API; conversation history and model
            # context never read these tables (they use runtime_nodes only).
            connection.execute(
                "UPDATE session_meta SET schema_version=? WHERE schema_version < ?",
                (SCHEMA_VERSION, SCHEMA_VERSION),
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
        # A database created by an older client may already have an outbox
        # entry before the local project binding is imported.  Once the
        # session is marked local-only, remove that stale payload immediately.
        local_only = connection.execute("SELECT local_only FROM session_meta LIMIT 1").fetchone()
        if local_only is not None and int(local_only[0]):
            connection.execute("DELETE FROM sync_outbox")

        # Every v5 session has a deterministic root.  This runs after the
        # outbox columns are ready so a migrated cloud-backed session can queue
        # the new canonical snapshot in the same transaction.
        if self._ensure_session_root(connection):
            meta = connection.execute("SELECT session_id,local_only,read_only FROM session_meta LIMIT 1").fetchone()
            if meta is not None and not int(meta[1]) and not int(meta[2]):
                self._queue(connection, str(meta[0]))

    def _ensure_session_root(self, connection: sqlite3.Connection) -> bool:
        """Create a root and reparent legacy local roots when needed."""

        meta = connection.execute("SELECT session_id,created_at FROM session_meta LIMIT 1").fetchone()
        if meta is None:
            return False
        session_id = str(meta[0])
        root_id = session_root_id(session_id)
        existing = connection.execute(
            "SELECT data_json FROM runtime_nodes WHERE session_id=? AND id=?",
            (session_id, root_id),
        ).fetchone()
        if existing is not None:
            if '"type":"root"' not in str(existing[0]).replace(" ", ""):
                raise ValueError(f"Reserved root id is already used by a non-root node: {root_id}.")
            return False

        all_local_nodes = connection.execute(
            "SELECT id,parent_session_id,parent_id,json_extract(data_json,'$.type') "
            "FROM runtime_nodes WHERE session_id=? ORDER BY timestamp,id",
            (session_id,),
        ).fetchall()
        legacy_roots = [row for row in all_local_nodes if str(row[3]) == "root"]
        legacy_root_ids = {str(row[0]) for row in legacy_roots}
        local_roots = [
            row
            for row in all_local_nodes
            if str(row[0]) not in legacy_root_ids and (not str(row[2]) or str(row[1]) != session_id)
        ]
        first_parent = None
        parent_candidate = next((row for row in [*legacy_roots, *local_roots] if str(row[2])), None)
        if parent_candidate is not None:
            first_parent = (str(parent_candidate[1]), str(parent_candidate[2]))
        root = create_root_node(session_id, parent=first_parent, timestamp=str(meta[1]))
        connection.execute(
            """INSERT INTO runtime_nodes(
                session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                compaction_idx,user,provider_name,model_json,permission_mode,running_mode,
                usage_json,cwd,timestamp,status,data_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            self._node_values(root),
        )
        direct_children = [
            row
            for row in all_local_nodes
            if str(row[1]) == session_id and str(row[2]) in legacy_root_ids
        ]
        root_keys = [str(row[0]) for row in local_roots] + [str(row[0]) for row in direct_children]
        if root_keys:
            placeholders = ",".join("?" for _ in root_keys)
            connection.execute(
                f"UPDATE runtime_nodes SET parent_session_id=?,parent_id=? "
                f"WHERE session_id=? AND id IN ({placeholders})",
                (session_id, root.id, session_id, *root_keys),
            )
        if legacy_root_ids:
            placeholders = ",".join("?" for _ in legacy_root_ids)
            connection.execute(
                f"DELETE FROM runtime_nodes WHERE session_id=? AND id IN ({placeholders})",
                (session_id, *legacy_root_ids),
            )
        return True

    def _insert_session_root(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        timestamp: str,
        parent: tuple[str, str] | None = None,
    ) -> None:
        """Insert a new session root inside the session metadata transaction."""

        root = create_root_node(session_id, parent=parent, timestamp=timestamp)
        connection.execute(
            """INSERT INTO runtime_nodes(
                session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                compaction_idx,user,provider_name,model_json,permission_mode,running_mode,
                usage_json,cwd,timestamp,status,data_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            self._node_values(root),
        )
