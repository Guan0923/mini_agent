"""SQLite snapshot synchronization and durable outbox behavior."""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

from backend.domain import DEFAULT_SESSION_TITLE
from backend.domain.runtime_state import RuntimeState as TreeRuntimeState
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState, text_messages

from .sqlite_schema import SCHEMA_VERSION


class SQLiteSyncMixin:
    """Add remote snapshot import/export to a per-session SQLite store."""

    def export_runtime_node_snapshot(self, session_id: str) -> dict[str, object]:
        """Export only session metadata and canonical nodes (schema 3)."""

        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT owner_device_id,title,created_at,updated_at,client_id,archived_at,deleted_at FROM session_meta"
            ).fetchone()
            nodes = connection.execute("SELECT * FROM runtime_nodes ORDER BY timestamp,id").fetchall()
        if row is None:
            raise ValueError(f"Unknown session: {session_id}")
        return {
            "schema_version": 3,
            "session": {
                "session_id": session_id,
                "owner_device_id": str(row[0]),
                "title": str(row[1]),
                "created_at": str(row[2]),
                "updated_at": str(row[3]),
                "client_id": str(row[4]) if row[4] is not None else None,
                "archived_at": str(row[5]) if row[5] is not None else None,
                "deleted_at": str(row[6]) if row[6] is not None else None,
            },
            "nodes": [self._node_from_row(row_item).to_dict() for row_item in nodes],
        }

    def apply_runtime_node_snapshot(self, snapshot: dict[str, object], *, local_device_id: str) -> None:
        """Import a schema-3 node snapshot; legacy runtime snapshots are rejected."""

        if int(snapshot.get("schema_version", -1)) != 3:
            raise ValueError("Only RuntimeState node snapshots (schema_version=3) are supported.")
        meta = snapshot.get("session")
        raw_nodes = snapshot.get("nodes")
        if not isinstance(meta, dict) or not isinstance(raw_nodes, list):
            raise ValueError("Node snapshot must contain session metadata and nodes.")
        if not all(isinstance(item, dict) for item in raw_nodes):
            raise ValueError("Node snapshot nodes must be objects.")
        session_id = str(meta.get("session_id") or "")
        owner = str(meta.get("owner_device_id") or "")
        if not session_id or not owner:
            raise ValueError("Node snapshot is missing session ownership metadata.")
        nodes = [TreeRuntimeState.from_dict(item) for item in raw_nodes]
        if any(node.session_id != session_id for node in nodes):
            raise ValueError("Node snapshot contains a node from another session.")
        with self._connection(session_id) as connection:
            self._clear_snapshot_tables(connection)
            connection.execute(
                """INSERT INTO session_meta(session_id,title,owner_device_id,remote_revision,read_only,
                    schema_version,created_at,updated_at,client_id,archived_at,deleted_at)
                    VALUES (?,?,?,0,1,3,?,?,?,?,?)""",
                (
                    session_id,
                    str(meta.get("title") or DEFAULT_SESSION_TITLE),
                    owner,
                    str(meta.get("created_at") or utc_now()),
                    str(meta.get("updated_at") or utc_now()),
                    str(meta["client_id"]) if meta.get("client_id") is not None else None,
                    str(meta["archived_at"]) if meta.get("archived_at") is not None else None,
                    str(meta["deleted_at"]) if meta.get("deleted_at") is not None else None,
                ),
            )
            for node in nodes:
                connection.execute(
                    """INSERT INTO runtime_nodes(
                        session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                        compaction_idx,user,provider,cwd,timestamp,status,data_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    self._node_values(node),
                )

    def pending_sync_operations(self) -> list[dict[str, object]]:
        operations: list[dict[str, object]] = []
        for summary in self.list_sessions(state="all"):
            with self._connection(summary.session_id) as connection:
                rows = connection.execute(
                    "SELECT operation_id,base_revision,kind,payload_json FROM sync_outbox "
                    "WHERE acknowledged_at IS NULL AND operation_id IS NOT NULL ORDER BY id DESC LIMIT 1"
                ).fetchall()
            for row in rows:
                operations.append(
                    {
                        "operation_id": str(row[0]),
                        "session_id": summary.session_id,
                        "base_revision": int(row[1]),
                        "kind": str(row[2]),
                        "snapshot": dict(json.loads(str(row[3]))),
                    }
                )
        return operations

    def acknowledge_sync_operations(self, acknowledgements: list[dict[str, object]]) -> None:
        by_id = {
            str(item["operation_id"]): int(item["revision"])
            for item in acknowledgements
            if item.get("operation_id") and item.get("revision") is not None
        }
        if not by_id:
            return
        for summary in self.list_sessions(state="all"):
            with self._connection(summary.session_id) as connection:
                for operation_id, revision in by_id.items():
                    row = connection.execute(
                        "SELECT id FROM sync_outbox WHERE operation_id=?", (operation_id,)
                    ).fetchone()
                    updated = connection.execute(
                        "UPDATE sync_outbox SET acknowledged_at=? WHERE acknowledged_at IS NULL AND id<=?",
                        (utc_now(), int(row[0]) if row else -1),
                    )
                    if updated.rowcount:
                        connection.execute("UPDATE session_meta SET remote_revision=?", (revision,))
                        connection.execute(
                            "UPDATE sync_outbox SET base_revision=? WHERE acknowledged_at IS NULL AND id>?",
                            (revision, int(row[0])),
                        )

    def remote_revision(self, session_id: str) -> int:
        with self._connection(session_id) as connection:
            row = connection.execute("SELECT remote_revision FROM session_meta").fetchone()
        return int(row[0]) if row else 0

    def apply_remote_snapshot(self, item: dict[str, object], *, local_device_id: str) -> None:
        session_id = str(item.get("session_id") or "")
        owner_device_id = str(item.get("owner_device_id") or "")
        revision = int(item.get("revision", 0))
        snapshot = item.get("snapshot")
        if not session_id or not owner_device_id or revision < 1 or not isinstance(snapshot, dict):
            raise ValueError("Invalid remote session snapshot.")
        existing = self.get_session(session_id)
        if existing is not None:
            with self._connection(session_id) as connection:
                current = connection.execute("SELECT owner_device_id,remote_revision FROM session_meta").fetchone()
                if current is None:
                    raise ValueError("Existing session is missing metadata.")
                current_owner, current_revision = str(current[0]), int(current[1])
                if current_owner != owner_device_id:
                    raise ValueError("Remote session owner does not match local metadata.")
                if current_owner == local_device_id:
                    if revision > current_revision:
                        connection.execute("UPDATE session_meta SET remote_revision=?", (revision,))
                    return
                if revision <= current_revision:
                    return
        meta = snapshot.get("session")
        if not isinstance(meta, dict):
            raise ValueError("Remote snapshot is missing session metadata.")
        if meta.get("session_id") not in {None, session_id}:
            raise ValueError("Remote snapshot session id does not match its envelope.")
        if int(snapshot.get("schema_version", -1)) != 3 or not isinstance(snapshot.get("nodes"), list):
            raise ValueError("Legacy runtime snapshots are no longer supported; expected schema_version=3 nodes.")
        if not all(isinstance(item, dict) for item in snapshot["nodes"]):
            raise ValueError("Remote snapshot nodes must be objects.")
        nodes = [TreeRuntimeState.from_dict(item) for item in snapshot["nodes"]]
        if any(node.session_id != session_id for node in nodes):
            raise ValueError("Remote snapshot contains a node from another session.")
        with self._connection(session_id) as connection:
            self._clear_snapshot_tables(connection)
            connection.execute(
                "INSERT INTO session_meta(session_id,title,owner_device_id,remote_revision,read_only,"
                "schema_version,created_at,updated_at,client_id,archived_at,deleted_at) "
                "VALUES (?,?,?,?,1,?,?,?,?,?,?)",
                (
                    session_id,
                    str(meta.get("title") or DEFAULT_SESSION_TITLE),
                    owner_device_id,
                    revision,
                    int(snapshot.get("schema_version", SCHEMA_VERSION)),
                    str(meta.get("created_at") or utc_now()),
                    str(meta.get("updated_at") or utc_now()),
                    str(meta["client_id"]) if meta.get("client_id") is not None else None,
                    str(meta["archived_at"]) if meta.get("archived_at") is not None else None,
                    str(meta["deleted_at"]) if meta.get("deleted_at") is not None else None,
                ),
            )
            for node in nodes:
                connection.execute(
                    """INSERT INTO runtime_nodes(
                        session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                        compaction_idx,user,provider,cwd,timestamp,status,data_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    self._node_values(node),
                )

    @staticmethod
    def _clear_snapshot_tables(connection: sqlite3.Connection) -> None:
        for table in (
            "session_meta",
            "session_runs",
            "session_messages",
            "session_runtime",
            "runs",
            "checkpoints",
            "runtime_messages",
            "runtime_nodes",
            "sync_outbox",
        ):
            connection.execute(f"DELETE FROM {table}")

    @staticmethod
    def _restore_snapshot_tables(connection: sqlite3.Connection, snapshot: dict[str, object], runtime: object) -> None:
        table_specs = {
            "session_runs": (
                "run_id",
                "task",
                "status",
                "workflow_id",
                "attempt",
                "origin_kind",
                "source_session_id",
                "source_run_id",
                "started_at",
                "updated_at",
            ),
            "session_messages": ("id", "run_id", "role", "content", "created_at"),
            "runs": ("run_id", "status", "state_json", "updated_at"),
            "checkpoints": ("id", "run_id", "reason", "state_json", "created_at"),
            "runtime_messages": ("run_id", "sequence", "kind", "message", "data_json", "created_at"),
        }
        restored_any = False
        for table, columns in table_specs.items():
            rows = snapshot.get(table, [])
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"Remote snapshot {table} must be a list of objects.")
            if rows:
                placeholders = ",".join("?" for _ in columns)
                connection.executemany(
                    f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
                    [tuple(row.get(column) for column in columns) for row in rows],
                )
                restored_any = True
        if restored_any or not isinstance(runtime, dict):
            return
        state = RuntimeState.from_dict(runtime)
        run = state.current_run
        if run is None:
            return
        origin = run.provenance
        connection.execute(
            "INSERT INTO session_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run.run_id,
                run.task,
                run.status,
                origin.workflow_id,
                origin.attempt,
                origin.trigger,
                origin.source_session_id,
                origin.source_run_id,
                state.created_at,
                state.updated_at,
            ),
        )
        connection.execute(
            "INSERT INTO runs(run_id,status,state_json,updated_at) VALUES (?,?,?,?)",
            (run.run_id, run.status, json.dumps(runtime, ensure_ascii=False), state.updated_at),
        )
        for message in text_messages(state.messages):
            connection.execute(
                "INSERT INTO session_messages(run_id,role,content,created_at) VALUES (?,?,?,?)",
                (run.run_id, message["role"], message["content"], state.updated_at),
            )

    @classmethod
    def _queue(cls, connection: sqlite3.Connection, session_id: str) -> None:
        meta = connection.execute("SELECT remote_revision FROM session_meta").fetchone()
        if meta is None:
            return
        snapshot = cls._export_snapshot(connection, session_id)
        connection.execute(
            "INSERT INTO sync_outbox(operation_id,base_revision,kind,payload_json,created_at) VALUES (?,?,?,?,?)",
            (
                f"operation_{uuid4().hex}",
                int(meta[0]),
                "snapshot",
                json.dumps(snapshot, ensure_ascii=False),
                utc_now(),
            ),
        )

    @staticmethod
    def _export_snapshot(connection: sqlite3.Connection, session_id: str) -> dict[str, object]:
        meta = connection.execute(
            "SELECT title,owner_device_id,created_at,updated_at,client_id,archived_at,deleted_at FROM session_meta"
        ).fetchone()
        if meta is None:
            raise ValueError(f"Unknown session: {session_id}")
        snapshot: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "session": {
                "session_id": session_id,
                "title": str(meta[0]),
                "owner_device_id": str(meta[1]),
                "created_at": str(meta[2]),
                "updated_at": str(meta[3]),
                "client_id": str(meta[4]) if meta[4] is not None else None,
                "archived_at": str(meta[5]) if meta[5] is not None else None,
                "deleted_at": str(meta[6]) if meta[6] is not None else None,
            },
            "nodes": [],
        }
        rows = connection.execute("SELECT * FROM runtime_nodes ORDER BY timestamp,id").fetchall()
        snapshot["nodes"] = [
            {
                "session_id": row[0],
                "parent_session_id": row[1],
                "id": row[2],
                "parent_id": row[3],
                "version": row[4],
                "firstKeptEntryId": row[5],
                "compactionIdx": row[6],
                "user": row[7],
                "provider": row[8],
                "cwd": row[9],
                "timestamp": row[10],
                "status": row[11],
                "data": json.loads(str(row[12])),
            }
            for row in rows
        ]
        return snapshot
