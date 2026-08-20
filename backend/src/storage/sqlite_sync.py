"""SQLite snapshot synchronization and durable outbox behavior."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

from backend.domain import DEFAULT_SESSION_TITLE
from backend.domain.runtime_state import RuntimeState as TreeRuntimeState
from backend.domain.runtime_state import ensure_session_root
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState, text_messages

from .codec import is_default_session_title, normalize_session_title
from .sqlite_schema import SCHEMA_VERSION, message_text


def _migrate_snapshot_permission(node: TreeRuntimeState) -> TreeRuntimeState:
    if node.permission_mode in {"approval_for_me", "full_access"}:
        return replace(node, permission_mode="read_only")
    return node


class SQLiteSyncMixin:
    """Add remote snapshot import/export to a per-session SQLite store."""

    _SUPPORTED_NODE_SNAPSHOT_VERSIONS = frozenset({8})

    def export_baseline(self, session_id: str) -> dict[str, object]:
        """Export only session metadata and canonical nodes (schema 6)."""

        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        if session.local_only:
            raise ValueError("Local-only sessions are excluded from cloud sync.")
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT owner_device_id,title,created_at,updated_at,client_id,archived_at,deleted_at,title_is_custom FROM session_meta"
            ).fetchone()
            nodes = connection.execute(
                "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider_name,model_json,permission_mode,running_mode,usage_json,cwd,timestamp,status,data_json FROM runtime_nodes ORDER BY timestamp,id"
            ).fetchall()
        if row is None:
            raise ValueError(f"Unknown session: {session_id}")
        return {
            "schema_version": SCHEMA_VERSION,
            "session": {
                "session_id": session_id,
                "owner_device_id": str(row[0]),
                "title": str(row[1]),
                "title_is_custom": bool(row[7]),
                "created_at": str(row[2]),
                "updated_at": str(row[3]),
                "client_id": str(row[4]) if row[4] is not None else None,
                "archived_at": str(row[5]) if row[5] is not None else None,
                "deleted_at": str(row[6]) if row[6] is not None else None,
            },
            "nodes": [self._node_from_row(row_item).to_dict() for row_item in nodes],
        }

    @staticmethod
    def _snapshot_title_meta(meta: dict[str, object], nodes: list[TreeRuntimeState]) -> tuple[str, bool]:
        """Resolve the title and its custom flag for an incoming snapshot.

        v6 snapshots carry ``title_is_custom`` and are trusted verbatim.
        Older v4/v5 snapshots lack the field and are inferred conservatively:
        a placeholder title is backfilled from the first user message and
        stays automatic, while any other title is treated as a manual rename
        so a historical custom title is never overwritten by auto-naming.
        """

        title = str(meta.get("title") or DEFAULT_SESSION_TITLE)
        custom = meta.get("title_is_custom")
        if custom is not None:
            return title, bool(custom)
        if not is_default_session_title(title):
            return title, True
        for node in nodes:
            data = getattr(node, "data", None)
            if not isinstance(data, dict) or data.get("type") != "message":
                continue
            message = data.get("message")
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = message_text(data)
            if text:
                return normalize_session_title(text), False
        return title, False

    def apply_baseline(self, snapshot: dict[str, object], *, local_device_id: str) -> None:
        """Import a supported snapshot and persist the normalized v7 shape."""

        snapshot_version = int(snapshot.get("schema_version", -1))
        if snapshot_version not in self._SUPPORTED_NODE_SNAPSHOT_VERSIONS:
            raise ValueError("Only JSON event baselines (schema_version=8) are supported.")
        meta = snapshot.get("session")
        raw_nodes = snapshot.get("nodes")
        if not isinstance(meta, dict) or not isinstance(raw_nodes, list):
            raise ValueError("Node snapshot must contain session metadata and nodes.")
        if not all(isinstance(item, dict) for item in raw_nodes):
            raise ValueError("Node snapshot nodes must be objects.")
        session_id = str(meta.get("session_id") or "")
        owner = str(meta.get("owner_device_id") or local_device_id)
        if not session_id or not owner:
            raise ValueError("Node snapshot is missing session ownership metadata.")
        existing = self.get_session(session_id)
        if existing is not None and existing.local_only:
            # A stale/forged remote snapshot must never replace a local
            # project conversation, even if it reuses the same session id.
            return
        nodes = [TreeRuntimeState.from_dict(item) for item in raw_nodes]
        # v8 nodes already carry the current permission protocol.
        if any(node.session_id != session_id for node in nodes):
            raise ValueError("Node snapshot contains a node from another session.")
        nodes = ensure_session_root(
            nodes,
            session_id,
            timestamp=str(meta.get("created_at") or utc_now()),
        )
        title, title_is_custom = self._snapshot_title_meta(meta, nodes)
        # A remote session must satisfy the same local directory contract as
        # a newly created session.  ``_connection`` initializes SQLite but
        # deliberately does not create workspace/uploads, which would make a
        # freshly restored session impossible to fork or use with tools.
        self.paths.ensure_session(session_id)
        with self._connection(session_id) as connection:
            self._clear_snapshot_tables(connection)
            connection.execute(
                """INSERT INTO session_meta(session_id,title,owner_device_id,remote_revision,read_only,
                    schema_version,created_at,updated_at,client_id,archived_at,deleted_at,title_is_custom)
                    VALUES (?,?,?,0,1,?,?,?,?,?,?,?)""",
                (
                    session_id,
                    title,
                    owner,
                    SCHEMA_VERSION,
                    str(meta.get("created_at") or utc_now()),
                    str(meta.get("updated_at") or utc_now()),
                    str(meta["client_id"]) if meta.get("client_id") is not None else None,
                    str(meta["archived_at"]) if meta.get("archived_at") is not None else None,
                    str(meta["deleted_at"]) if meta.get("deleted_at") is not None else None,
                    int(title_is_custom),
                ),
            )
            for node in nodes:
                connection.execute(
                    """INSERT INTO runtime_nodes(
                        session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                        compaction_idx,user,provider_name,model_json,permission_mode,running_mode,
                        usage_json,cwd,timestamp,status,data_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    self._node_values(node),
                )
                connection.execute(
                    "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?)",
                    (session_id, "runtime_node", node.id, json.dumps(node.to_dict(), ensure_ascii=False, separators=(",", ":")), node.timestamp),
                )
            runtime = snapshot.get("runtime")
            if isinstance(runtime, dict):
                raw_runtime = json.dumps(runtime, ensure_ascii=False, separators=(",", ":"))
                connection.execute(
                    "INSERT INTO session_runtime(session_id,state_json,updated_at) VALUES (?,?,?)",
                    (session_id, raw_runtime, str(runtime.get("updated_at") or utc_now())),
                )
                connection.execute(
                    "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?)",
                    (session_id, "runtime_state", session_id, raw_runtime, str(runtime.get("updated_at") or utc_now())),
                )

    def pending_sync_operations(self) -> list[dict[str, object]]:
        """Return unacknowledged JSON events without materializing a snapshot.

        A single operation is returned per session.  Its payload is an ordered
        list of immutable events, so the size of a pending operation is bounded
        by the changes made since the last acknowledgement rather than by the
        complete conversation history.
        """
        operations: list[dict[str, object]] = []
        for summary in self.list_sessions(state="all"):
            if summary.local_only:
                continue
            with self._connection(summary.session_id) as connection:
                rows = connection.execute(
                    "SELECT event_id,local_sequence,base_revision,kind,payload_json,checksum,created_at "
                    "FROM json_events WHERE session_id=? AND acknowledged_at IS NULL "
                    "ORDER BY local_sequence",
                    (summary.session_id,),
                ).fetchall()
            if not rows:
                continue
            operations.append(
                {
                    "operation_id": f"operation_{rows[0][0]}",
                    "session_id": summary.session_id,
                    "base_revision": int(self.remote_revision(summary.session_id)),
                    "kind": "events",
                    "events": [
                        {
                            "event_id": str(row[0]),
                            "sequence": int(row[1]),
                            "base_revision": int(row[2]),
                            "kind": str(row[3]),
                            "payload": dict(json.loads(str(row[4]))),
                            "checksum": str(row[5]),
                            "created_at": str(row[6]),
                        }
                        for row in rows
                    ],
                }
            )
        return operations

    def acknowledge_sync_operations(self, acknowledgements: list[dict[str, object]]) -> None:
        # Acknowledgements are scoped to a session and exact event IDs.  Do
        # not acknowledge by sequence alone: two sessions can legitimately
        # have the same local sequence, and a retry may acknowledge a subset.
        for item in acknowledgements:
            session_id = str(item.get("session_id") or "")
            revision = item.get("revision")
            raw_ids = item.get("event_ids")
            if raw_ids is None:
                candidate = item.get("event_id") or item.get("operation_id")
                raw_ids = [candidate] if candidate else []
            if not session_id or revision is None or not isinstance(raw_ids, list):
                continue
            event_ids = [str(value) for value in raw_ids if value]
            if not event_ids:
                continue
            with self._connection(session_id) as connection:
                placeholders = ",".join("?" for _ in event_ids)
                connection.execute(
                    f"UPDATE json_events SET acknowledged_at=? WHERE acknowledged_at IS NULL AND event_id IN ({placeholders})",
                    [utc_now(), *event_ids],
                )
                connection.execute(
                    "UPDATE session_meta SET remote_revision=? WHERE session_id=? AND remote_revision<=?",
                    (int(revision), session_id, int(revision)),
                )

    def remote_revision(self, session_id: str) -> int:
        with self._connection(session_id) as connection:
            row = connection.execute("SELECT remote_revision FROM session_meta").fetchone()
        return int(row[0]) if row else 0

    def event_head(self, session_id: str) -> int:
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(local_sequence), 0) FROM json_events WHERE session_id=?", (session_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def apply_sync_events(
        self,
        item: dict[str, object],
        *,
        local_device_id: str,
    ) -> int:
        """Apply an ordered remote JSON event batch transactionally.

        The first ``baseline`` event uses the existing snapshot normalizer as
        a one-time bootstrap.  All subsequent events are small object-level
        deltas and are applied without reconstructing a full snapshot.
        """

        session_id = str(item.get("session_id") or "")
        revision = int(item.get("revision", 0))
        parent_revision = int(item.get("parent_revision", max(revision - 1, 0)))
        events = item.get("events")
        owner = str(item.get("owner_device_id") or local_device_id)
        if not session_id or revision < 1 or not isinstance(events, list):
            raise ValueError("Invalid remote event batch.")
        existing_revision = self.remote_revision(session_id) if self.get_session(session_id) else 0
        if revision <= existing_revision:
            return existing_revision
        if parent_revision != existing_revision:
            raise ValueError("Remote event parent revision does not match the local head.")
        if not all(isinstance(event, dict) for event in events):
            raise ValueError("Remote events must be objects.")
        previous_sequence = 0
        for event in events:
            event_sequence = int(event.get("sequence", 0))
            if event_sequence <= previous_sequence:
                raise ValueError("Remote events are out of order.")
            previous_sequence = event_sequence
            raw_payload = json.dumps(
                event.get("payload"), ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            checksum = str(event.get("checksum") or "")
            if hashlib.sha256(raw_payload.encode("utf-8")).hexdigest() != checksum:
                raise ValueError("Remote event checksum mismatch.")

        first = events[0] if events else None
        if first is not None and str(first.get("kind")) == "baseline" and self.get_session(session_id) is None:
            payload = first.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("Remote baseline payload must be an object.")
            self.apply_baseline(
                {**payload, "schema_version": int(payload.get("schema_version", SCHEMA_VERSION))},
                local_device_id=local_device_id,
            )

        if self.get_session(session_id) is None:
            raise ValueError("Remote event session has no baseline.")
        with self._connection(session_id) as connection:
            next_local_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(local_sequence), 0) FROM json_events WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
            for event in events:
                event_id = str(event.get("event_id") or "")
                sequence = int(event.get("sequence", 0))
                kind = str(event.get("kind") or "")
                payload = event.get("payload")
                if not event_id or sequence < 1 or not isinstance(payload, dict):
                    raise ValueError("Remote event envelope is invalid.")
                duplicate = connection.execute(
                    "SELECT 1 FROM json_events WHERE event_id=?", (event_id,)
                ).fetchone()
                if duplicate is not None:
                    continue
                self._apply_event_payload(connection, session_id, kind, payload)
                next_local_sequence += 1
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                connection.execute(
                    "INSERT INTO json_events(session_id,local_sequence,event_id,base_revision,kind,payload_json,checksum,created_at,acknowledged_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        next_local_sequence,
                        event_id,
                        parent_revision,
                        kind,
                        raw,
                        str(event.get("checksum") or hashlib.sha256(raw.encode("utf-8")).hexdigest()),
                        str(event.get("created_at") or utc_now()),
                        utc_now(),
                    ),
                )
            connection.execute("UPDATE session_meta SET remote_revision=?, owner_device_id=?", (revision, owner))
        return revision

    def _apply_event_payload(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        kind: str,
        payload: dict[str, object],
    ) -> None:
        """Apply one JSON delta to the existing runtime projections."""

        if kind in {"node_upserted", "node_finalized"}:
            node = payload.get("node")
            if not isinstance(node, dict):
                raise ValueError("Node event payload is invalid.")
            values = self._node_values(TreeRuntimeState.from_dict(node))
            connection.execute(
                "INSERT INTO runtime_nodes(session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider_name,model_json,permission_mode,running_mode,usage_json,cwd,timestamp,status,data_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,id) DO UPDATE SET "
                "parent_session_id=excluded.parent_session_id,parent_id=excluded.parent_id,version=excluded.version,first_kept_entry_id=excluded.first_kept_entry_id,compaction_idx=excluded.compaction_idx,user=excluded.user,provider_name=excluded.provider_name,model_json=excluded.model_json,permission_mode=excluded.permission_mode,running_mode=excluded.running_mode,usage_json=excluded.usage_json,cwd=excluded.cwd,timestamp=excluded.timestamp,status=excluded.status,data_json=excluded.data_json",
                values,
            )
            connection.execute(
                "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (session_id, "runtime_node", str(node.get("id") or ""), json.dumps(node, ensure_ascii=False, separators=(",", ":")), str(node.get("timestamp") or utc_now())),
            )
            return
        if kind == "runtime_state_saved":
            state = payload.get("state")
            if isinstance(state, dict):
                raw = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                connection.execute(
                    "INSERT INTO session_runtime(session_id,state_json,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at",
                    (session_id, raw, str(state.get("updated_at") or utc_now())),
                )
                connection.execute(
                    "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                    (session_id, "runtime_state", session_id, raw, str(state.get("updated_at") or utc_now())),
                )
            return
        if kind == "runtime_message_appended":
            connection.execute(
                "INSERT INTO runtime_messages(run_id,sequence,kind,message,data_json,created_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(run_id,sequence) DO UPDATE SET kind=excluded.kind,message=excluded.message,data_json=excluded.data_json,created_at=excluded.created_at",
                (
                    str(payload.get("run_id") or ""),
                    int(payload.get("sequence", 0)),
                    str(payload.get("kind") or ""),
                    str(payload.get("message") or ""),
                    json.dumps(payload.get("data") or {}, ensure_ascii=False, separators=(",", ":")),
                    str(payload.get("created_at") or utc_now()),
                ),
            )
            message_id = f"{payload.get('run_id') or ''}:{int(payload.get('sequence', 0))}"
            connection.execute(
                "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (session_id, "runtime_message", message_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), str(payload.get("created_at") or utc_now())),
            )
            return
        if kind == "checkpoint_recorded":
            checkpoint_id = f"{payload.get('run_id') or ''}:{payload.get('created_at') or ''}:{payload.get('reason') or ''}"
            connection.execute(
                "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(session_id,namespace,object_id) DO NOTHING",
                (session_id, "checkpoint", checkpoint_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), str(payload.get("created_at") or utc_now())),
            )
            return
        if kind == "session_metadata_updated":
            assignments: list[str] = []
            values: list[object] = []
            for field in ("title", "title_is_custom", "client_id", "archived_at", "deleted_at"):
                if field in payload:
                    assignments.append(f"{field}=?")
                    values.append(int(payload[field]) if field == "title_is_custom" else payload[field])
            if assignments:
                values.append(session_id)
                connection.execute(
                    f"UPDATE session_meta SET {', '.join(assignments)}, updated_at=? WHERE session_id=?",
                    [*values[:-1], str(payload.get("updated_at") or utc_now()), values[-1]],
                )
                existing = connection.execute(
                    "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace='session' AND object_id=?",
                    (session_id, session_id),
                ).fetchone()
                document = json.loads(str(existing[0])) if existing is not None else {"session_id": session_id}
                document.update(payload)
                connection.execute(
                    "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                    (session_id, "session", session_id, json.dumps(document, ensure_ascii=False, separators=(",", ":")), str(payload.get("updated_at") or utc_now())),
                )
            return

    def apply_remote_snapshot(self, item: dict[str, object], *, local_device_id: str) -> None:
        session_id = str(item.get("session_id") or "")
        owner_device_id = str(item.get("owner_device_id") or "")
        revision = int(item.get("revision", 0))
        snapshot = item.get("snapshot")
        if not session_id or not owner_device_id or revision < 1 or not isinstance(snapshot, dict):
            raise ValueError("Invalid remote session snapshot.")
        existing = self.get_session(session_id)
        if existing is not None:
            if existing.local_only:
                # Project conversations are deliberately outside cloud sync.
                # Defensively ignore a remote item that happens to reuse one.
                return
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
        snapshot_version = int(snapshot.get("schema_version", -1))
        if snapshot_version not in self._SUPPORTED_NODE_SNAPSHOT_VERSIONS or not isinstance(
            snapshot.get("nodes"), list
        ):
            raise ValueError("Remote baseline must use schema_version=8 and contain a nodes list.")
        if not all(isinstance(item, dict) for item in snapshot["nodes"]):
            raise ValueError("Remote snapshot nodes must be objects.")
        nodes = [TreeRuntimeState.from_dict(item) for item in snapshot["nodes"]]
        if any(node.session_id != session_id for node in nodes):
            raise ValueError("Remote snapshot contains a node from another session.")
        nodes = ensure_session_root(
            nodes,
            session_id,
            timestamp=str(meta.get("created_at") or utc_now()),
        )
        title, title_is_custom = self._snapshot_title_meta(meta, nodes)
        self.paths.ensure_session(session_id)
        with self._connection(session_id) as connection:
            self._clear_snapshot_tables(connection)
            connection.execute(
                "INSERT INTO session_meta(session_id,title,owner_device_id,remote_revision,read_only,"
                "schema_version,created_at,updated_at,client_id,archived_at,deleted_at,local_only,title_is_custom) "
                "VALUES (?,?,?,?,1,?,?,?,?,?,?,0,?)",
                (
                    session_id,
                    title,
                    owner_device_id,
                    revision,
                    SCHEMA_VERSION,
                    str(meta.get("created_at") or utc_now()),
                    str(meta.get("updated_at") or utc_now()),
                    str(meta["client_id"]) if meta.get("client_id") is not None else None,
                    str(meta["archived_at"]) if meta.get("archived_at") is not None else None,
                    str(meta["deleted_at"]) if meta.get("deleted_at") is not None else None,
                    int(title_is_custom),
                ),
            )
            for node in nodes:
                connection.execute(
                    """INSERT INTO runtime_nodes(
                        session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                        compaction_idx,user,provider_name,model_json,permission_mode,running_mode,
                        usage_json,cwd,timestamp,status,data_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    self._node_values(node),
                )
            self._restore_snapshot_tables(connection, snapshot, snapshot.get("runtime"))

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
            "json_objects",
            "json_events",
        ):
            connection.execute(f"DELETE FROM {table}")

    @staticmethod
    def _restore_snapshot_tables(
        connection: sqlite3.Connection,
        snapshot: dict[str, object],
        runtime: object,
    ) -> None:
        """Restore optional local execution rows carried by a v5 envelope.

        The helper intentionally does not synthesize or alter ``runtime_nodes``
        and is a no-op for the public compact node snapshot.  It exists so a
        resumed worker and the legacy fork/run index remain usable after a
        local-first snapshot round trip.
        """

        for table, columns in SQLiteSyncMixin._LEGACY_SNAPSHOT_TABLES.items():
            rows = snapshot.get(table, [])
            if rows is None:
                continue
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"Remote snapshot {table} must be a list of objects.")
            if not rows:
                continue
            placeholders = ",".join("?" for _ in columns)
            connection.executemany(
                f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
                [tuple(row.get(column) for column in columns) for row in rows],
            )

        if not isinstance(runtime, dict):
            return
        target_row = connection.execute("SELECT session_id FROM session_meta LIMIT 1").fetchone()
        target_session_id = str(target_row[0]) if target_row is not None else ""
        runtime_session_id = str(runtime.get("session_id") or target_session_id)
        if target_session_id and runtime_session_id != target_session_id:
            raise ValueError("Remote runtime state session id does not match session metadata.")
        connection.execute(
            "INSERT INTO session_runtime(session_id,state_json,updated_at) VALUES (?,?,?)",
            (
                runtime_session_id,
                json.dumps(runtime, ensure_ascii=False),
                str(runtime.get("updated_at") or utc_now()),
            ),
        )
        # Older producers may provide only a resumable runtime object.  Build
        # the minimal run index needed by resume/fork without making it the
        # conversation source.
        if connection.execute("SELECT 1 FROM session_runs LIMIT 1").fetchone() is not None:
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
    def _queue(
        cls,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        kind: str = "session_changed",
        payload: dict[str, object] | None = None,
        object_namespace: str | None = None,
        object_id: str | None = None,
    ) -> None:
        cls._append_event(
            connection,
            session_id,
            kind=kind,
            payload=payload or {"session_id": session_id},
            object_namespace=object_namespace,
            object_id=object_id,
        )

    @classmethod
    def _append_event(
        cls,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        kind: str,
        payload: dict[str, object],
        object_namespace: str | None = None,
        object_id: str | None = None,
    ) -> str | None:
        """Append one JSON event and optionally update its materialized object.

        This is deliberately independent of the legacy relational projections.
        The projections remain available to the current runtime APIs while all
        synchronization data is now represented by immutable JSON events.
        """
        meta = connection.execute("SELECT remote_revision,local_only FROM session_meta").fetchone()
        if meta is None:
            return None
        if int(meta[1]):
            return None
        created_at = utc_now()
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        event_id = f"event_{uuid4().hex}"
        next_row = connection.execute(
            "SELECT COALESCE(MAX(local_sequence), 0) + 1 FROM json_events WHERE session_id=?",
            (session_id,),
        ).fetchone()
        sequence = int(next_row[0]) if next_row else 1
        checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO json_events(session_id,local_sequence,event_id,base_revision,kind,payload_json,checksum,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id,
                sequence,
                event_id,
                int(meta[0]),
                kind,
                raw,
                checksum,
                created_at,
            ),
        )
        if object_namespace and object_id:
            connection.execute(
                "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET "
                "payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (session_id, object_namespace, object_id, raw, created_at),
            )
        elif kind == "session_metadata_updated":
            existing = connection.execute(
                "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace='session' AND object_id=?",
                (session_id, session_id),
            ).fetchone()
            document = json.loads(str(existing[0])) if existing is not None else {"session_id": session_id}
            document.update(payload)
            document["updated_at"] = str(payload.get("updated_at") or created_at)
            connection.execute(
                "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (session_id, "session", session_id, json.dumps(document, ensure_ascii=False, separators=(",", ":")), created_at),
            )
        return event_id

    @classmethod
    def _build_baseline(cls, connection: sqlite3.Connection, session_id: str) -> dict[str, object]:
        """Build the one-time bootstrap document for a newly created session.

        This is only used for the immutable ``baseline`` event.  Incremental
        writes never call this function, so a growing conversation cannot be
        copied into every pending operation.
        """
        meta = connection.execute(
            "SELECT title,owner_device_id,created_at,updated_at,client_id,archived_at,deleted_at,title_is_custom FROM session_meta"
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
                "title_is_custom": bool(meta[7]),
            },
            "nodes": [],
            "runtime": None,
        }
        rows = connection.execute(
            "SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,compaction_idx,user,provider_name,model_json,permission_mode,running_mode,usage_json,cwd,timestamp,status,data_json FROM runtime_nodes ORDER BY timestamp,id"
        ).fetchall()
        snapshot["nodes"] = [cls._node_from_row(row).to_dict() for row in rows]
        runtime_row = connection.execute(
            "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace='runtime_state' LIMIT 1",
            (session_id,),
        ).fetchone()
        if runtime_row is None:
            runtime_row = connection.execute("SELECT state_json FROM session_runtime LIMIT 1").fetchone()
        snapshot["runtime"] = json.loads(str(runtime_row[0])) if runtime_row is not None else None
        return snapshot
