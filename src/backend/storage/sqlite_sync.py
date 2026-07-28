"""SQLite snapshot synchronization and durable outbox behavior."""

from __future__ import annotations

import json
import sqlite3
from uuid import uuid4

from backend.domain import DEFAULT_SESSION_TITLE
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState, text_messages

from .sqlite_schema import SCHEMA_VERSION


class SQLiteSyncMixin:
    """Add remote snapshot import/export to a per-session SQLite store."""

    def pending_sync_operations(self) -> list[dict[str, object]]:
        operations: list[dict[str, object]] = []
        for summary in self.list_sessions():
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
        for summary in self.list_sessions():
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
        runtime = snapshot.get("runtime")
        if not isinstance(meta, dict):
            raise ValueError("Remote snapshot is missing session metadata.")
        if meta.get("session_id") not in {None, session_id}:
            raise ValueError("Remote snapshot session id does not match its envelope.")
        with self._connection(session_id) as connection:
            self._clear_snapshot_tables(connection)
            connection.execute(
                "INSERT INTO session_meta(session_id,title,owner_device_id,remote_revision,read_only,"
                "schema_version,created_at,updated_at) VALUES (?,?,?,?,1,?,?,?)",
                (
                    session_id,
                    str(meta.get("title") or DEFAULT_SESSION_TITLE),
                    owner_device_id,
                    revision,
                    int(snapshot.get("schema_version", SCHEMA_VERSION)),
                    str(meta.get("created_at") or utc_now()),
                    str(meta.get("updated_at") or utc_now()),
                ),
            )
            if isinstance(runtime, dict):
                restored_runtime = dict(runtime)
                restored_runtime["session_id"] = session_id
                connection.execute(
                    "INSERT INTO session_runtime(session_id,state_json,updated_at) VALUES (?,?,?)",
                    (session_id, json.dumps(restored_runtime, ensure_ascii=False), utc_now()),
                )
            self._restore_snapshot_tables(connection, snapshot, runtime)

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
        meta = connection.execute("SELECT title,owner_device_id,created_at,updated_at FROM session_meta").fetchone()
        if meta is None:
            raise ValueError(f"Unknown session: {session_id}")
        runtime = connection.execute("SELECT state_json FROM session_runtime").fetchone()
        snapshot: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "session": {
                "session_id": session_id,
                "title": str(meta[0]),
                "owner_device_id": str(meta[1]),
                "created_at": str(meta[2]),
                "updated_at": str(meta[3]),
            },
            "runtime": json.loads(str(runtime[0])) if runtime is not None else None,
        }
        for table in ("session_runs", "session_messages", "runs", "checkpoints", "runtime_messages"):
            snapshot[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        return snapshot
