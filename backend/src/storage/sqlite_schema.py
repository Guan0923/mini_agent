"""SQLite schema creation and the one-way RuntimeState node migration."""

from __future__ import annotations

import json
import sqlite3

from backend.domain.runtime_state import create_root_node, session_root_id

from .codec import is_default_session_title, normalize_session_title

SCHEMA_VERSION = 8
# Keep the structural version stable: v4 databases only need the session-root
# migration and should not have their message tree dropped. New databases use
# the three-state CHECK constraint below; stale failed rows are intentionally
# outside the supported protocol and are left for user cleanup.
RUNTIME_NODE_SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT PRIMARY KEY, title TEXT NOT NULL, owner_device_id TEXT NOT NULL,
    remote_revision INTEGER NOT NULL DEFAULT 0, read_only INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 8, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    client_id TEXT, archived_at TEXT, deleted_at TEXT,
    local_only INTEGER NOT NULL DEFAULT 0, title_is_custom INTEGER NOT NULL DEFAULT 0
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
    permission_mode TEXT NOT NULL DEFAULT 'read_only',
    running_mode TEXT NOT NULL DEFAULT 'agent',
    usage_json TEXT NOT NULL,
    cwd TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'abort')),
    data_json TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
);
CREATE INDEX IF NOT EXISTS runtime_nodes_session_timestamp_idx
    ON runtime_nodes (session_id, timestamp, id);
CREATE INDEX IF NOT EXISTS runtime_nodes_parent_idx
    ON runtime_nodes (parent_session_id, parent_id, timestamp, id);
-- v8 JSON event storage.  Business payloads are kept as JSON documents;
-- scalar columns exist only for ordering, idempotency, and synchronization.
CREATE TABLE IF NOT EXISTS json_objects (
    session_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    object_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, namespace, object_id)
);
CREATE INDEX IF NOT EXISTS json_objects_session_idx
    ON json_objects (session_id, namespace, updated_at, object_id);
CREATE TABLE IF NOT EXISTS json_events (
    session_id TEXT NOT NULL,
    local_sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    base_revision INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    PRIMARY KEY (session_id, local_sequence)
);
CREATE INDEX IF NOT EXISTS json_events_pending_idx
    ON json_events (session_id, acknowledged_at, local_sequence);
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
    """Upgrade older per-session databases before normal access."""

    device_id: str

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        # The v8 JSON event store is intentionally a protocol break.  Never
        # reinterpret or mutate a pre-v8 relationship/snapshot database: the
        # owner explicitly removes those files before first use.
        meta_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(session_meta)")}
        schema_default = next(
            (str(row[4]).strip("'") for row in connection.execute("PRAGMA table_info(session_meta)") if str(row[1]) == "schema_version"),
            str(SCHEMA_VERSION),
        )
        if schema_default != str(SCHEMA_VERSION):
            raise RuntimeError(
                "Unsupported legacy state.db schema; remove the old state.db before using JSON event storage."
            )
        prior_version_row = (
            connection.execute("SELECT schema_version FROM session_meta LIMIT 1").fetchone()
            if "schema_version" in meta_columns
            else None
        )
        if prior_version_row is not None and int(prior_version_row[0] or 0) < SCHEMA_VERSION:
            raise RuntimeError(
                "Unsupported legacy state.db schema; remove the old state.db before using JSON event storage."
            )
        if prior_version_row is None and meta_columns and connection.execute("SELECT 1 FROM session_meta LIMIT 1").fetchone() is not None:
            legacy_tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "sync_outbox" in legacy_tables or "runtime_nodes" in legacy_tables:
                # A metadata table without a v8 row is an old database, not a
                # partially initialized new one, when any runtime table exists.
                if "session_meta" in legacy_tables and legacy_tables.intersection(
                    {"sync_outbox", "runtime_nodes", "session_runtime"}
                ):
                    raise RuntimeError(
                        "Unsupported legacy state.db schema; remove the old state.db before using JSON event storage."
                    )
        runtime_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runtime_nodes)")}
        required_runtime_columns = {
            "provider_name",
            "model_json",
            "permission_mode",
            "running_mode",
            "usage_json",
        }
        prior_version = int(prior_version_row[0]) if prior_version_row is not None else SCHEMA_VERSION
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
                    model_json TEXT NOT NULL, permission_mode TEXT NOT NULL DEFAULT 'read_only',
                    running_mode TEXT NOT NULL DEFAULT 'agent', usage_json TEXT NOT NULL,
                    cwd TEXT NOT NULL DEFAULT '', timestamp TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'abort')),
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
            ("title_is_custom", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in meta_columns:
                connection.execute(f"ALTER TABLE session_meta ADD COLUMN {name} {definition}")
        connection.execute("UPDATE session_meta SET schema_version=? WHERE schema_version IS NULL", (SCHEMA_VERSION,))
        connection.execute(
            "UPDATE session_meta SET owner_device_id=? WHERE owner_device_id IS NULL OR owner_device_id=''",
            (self.device_id,),
        )
        if prior_version < SCHEMA_VERSION or (
            runtime_columns and not required_runtime_columns.issubset(runtime_columns)
        ):
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
        # New databases are created with their deterministic root by
        # create_session.  No legacy snapshot/outbox backfill is performed.

    @staticmethod
    def _migrate_legacy_permissions(connection: sqlite3.Connection) -> None:
        """Downgrade modes that predate the joint file/network confirmation."""

        connection.execute(
            "UPDATE runtime_nodes SET permission_mode='read_only' "
            "WHERE permission_mode IN ('approval_for_me','full_access')"
        )
        for table, key in (("session_runtime", "session_id"), ("runs", "run_id")):
            rows = connection.execute(f"SELECT {key},state_json FROM {table}").fetchall()
            for identity, raw in rows:
                try:
                    payload = json.loads(str(raw))
                except (TypeError, ValueError):
                    continue
                if _replace_legacy_permissions(payload):
                    connection.execute(
                        f"UPDATE {table} SET state_json=? WHERE {key}=?",
                        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), identity),
                    )

    def _backfill_title_is_custom(self, connection: sqlite3.Connection) -> None:
        """Infer ``title_is_custom`` for a pre-v6 database and backfill titles.

        Runs exactly once, inside the same transaction as the schema upgrade.
        Cloud-backed sessions then re-queue their canonical snapshot so the
        sync service observes the v6 shape; local-only project sessions stay
        out of the outbox.
        """

        meta = connection.execute("SELECT title FROM session_meta LIMIT 1").fetchone()
        if meta is None:
            return
        title = str(meta[0])
        if is_default_session_title(title):
            user_text = first_local_user_message(connection)
            if user_text is not None:
                connection.execute("UPDATE session_meta SET title=?", (normalize_session_title(user_text),))
            connection.execute("UPDATE session_meta SET title_is_custom=0")
        else:
            # Historical non-placeholder titles cannot reliably distinguish
            # automatic from manual naming.  Preserve them by treating every
            # such title as custom; the automatic rule then never overwrites
            # a title the user may have typed themselves.
            connection.execute("UPDATE session_meta SET title_is_custom=1")
        meta = connection.execute("SELECT session_id,local_only,read_only FROM session_meta").fetchone()
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
        connection.execute(
            "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?)",
            (session_id, "runtime_node", root.id, json.dumps(root.to_dict(), ensure_ascii=False, separators=(",", ":")), str(meta[1])),
        )
        direct_children = [
            row for row in all_local_nodes if str(row[1]) == session_id and str(row[2]) in legacy_root_ids
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
        connection.execute(
            "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?)",
            (session_id, "runtime_node", root.id, json.dumps(root.to_dict(), ensure_ascii=False, separators=(",", ":")), timestamp),
        )


def message_text(data: object) -> str:
    """Return the joined text of a canonical message node payload."""

    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") in {"text", "reasoning", "bash"}
    )


def _replace_legacy_permissions(value: object) -> bool:
    changed = False
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "permission_mode" and item in {"approval_for_me", "full_access"}:
                value[key] = "read_only"
                changed = True
            else:
                changed = _replace_legacy_permissions(item) or changed
    elif isinstance(value, list):
        for item in value:
            changed = _replace_legacy_permissions(item) or changed
    return changed


def first_local_user_message(connection: sqlite3.Connection) -> str | None:
    """Return the text of the first local user message, if any.

    ``runtime_nodes`` is the canonical conversation source.  The legacy
    ``session_messages`` projection remains a fallback for pre-node stores so
    the placeholder title of an ancient session is still replaced.
    """

    rows = connection.execute(
        "SELECT data_json FROM runtime_nodes "
        "WHERE json_extract(data_json, '$.type')='message' "
        "AND json_extract(data_json, '$.message.role')='user' "
        "ORDER BY timestamp,id"
    ).fetchall()
    for (payload,) in rows:
        try:
            data = json.loads(str(payload))
        except (TypeError, ValueError):
            continue
        text = message_text(data)
        if text:
            return text
    row = connection.execute("SELECT content FROM session_messages WHERE role='user' ORDER BY id LIMIT 1").fetchone()
    return str(row[0]) if row is not None else None
