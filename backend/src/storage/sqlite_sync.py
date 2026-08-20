"""Incremental local JSON events and transactional remote replay."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from uuid import uuid4

from backend.domain.state import utc_now

from .sqlite_schema import SCHEMA_VERSION


class SQLiteSyncMixin:
    """Synchronize immutable JSON events; no snapshot/outbox payload exists."""

    def export_baseline(self, session_id: str) -> dict[str, object]:
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        if session.local_only:
            raise ValueError("Local-only sessions are excluded from cloud sync.")
        return self._build_baseline_for_runtime(session_id)

    def _build_baseline_for_runtime(self, session_id: str) -> dict[str, object]:
        with self._connection(session_id) as connection:
            rows = connection.execute(
                "SELECT namespace,object_id,payload_json,updated_at FROM json_objects WHERE session_id=? ORDER BY namespace,updated_at,object_id",
                (session_id,),
            ).fetchall()
        return {
            "schema_version": SCHEMA_VERSION,
            "objects": [
                {
                    "namespace": str(row[0]),
                    "object_id": str(row[1]),
                    "payload": json.loads(str(row[2])),
                    "updated_at": str(row[3]),
                }
                for row in rows
            ],
        }

    def apply_baseline(self, baseline: dict[str, object], *, local_device_id: str) -> None:
        if int(baseline.get("schema_version", -1)) != SCHEMA_VERSION:
            raise ValueError(f"Only JSON event baselines (schema_version={SCHEMA_VERSION}) are supported.")
        objects = baseline.get("objects")
        if not isinstance(objects, list) or not all(isinstance(item, dict) for item in objects):
            raise ValueError("JSON baseline must contain an objects list.")
        session_item = next((item for item in objects if item.get("namespace") == "session"), None)
        if session_item is None or not isinstance(session_item.get("payload"), dict):
            raise ValueError("JSON baseline is missing session metadata.")
        payload = dict(session_item["payload"])
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            raise ValueError("JSON baseline is missing session_id.")
        payload.setdefault("owner_device_id", local_device_id)
        payload["read_only"] = True
        self.paths.ensure_session(session_id)
        with self._connection(session_id) as connection:
            connection.execute("DELETE FROM json_objects")
            connection.execute("DELETE FROM json_events")
            for item in objects:
                namespace = str(item.get("namespace") or "")
                object_id = str(item.get("object_id") or "")
                value = item.get("payload")
                if not namespace or not object_id or not isinstance(value, dict):
                    raise ValueError("JSON baseline contains an invalid object.")
                if namespace == "session":
                    value = payload
                self._put_json_object(
                    connection,
                    session_id,
                    namespace,
                    object_id,
                    dict(value),
                    str(item.get("updated_at") or utc_now()),
                )
            now = utc_now()
            connection.execute(
                "INSERT INTO store_metadata(session_id,schema_version,created_at,updated_at) VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET remote_revision=0,local_revision=0,updated_at=excluded.updated_at",
                (session_id, SCHEMA_VERSION, str(payload.get("created_at") or now), now),
            )

    def pending_sync_operations(self) -> list[dict[str, object]]:
        operations: list[dict[str, object]] = []
        for summary in self.list_sessions(state="all"):
            if summary.local_only:
                continue
            with self._connection(summary.session_id) as connection:
                rows = connection.execute(
                    "SELECT event_id,local_sequence,base_revision,kind,payload_json,checksum,created_at FROM json_events WHERE session_id=? AND acknowledged_at IS NULL ORDER BY local_sequence",
                    (summary.session_id,),
                ).fetchall()
                meta = connection.execute(
                    "SELECT remote_revision FROM store_metadata WHERE session_id=?", (summary.session_id,)
                ).fetchone()
            if not rows:
                continue
            operations.append(
                {
                    "operation_id": f"batch_{rows[0][0]}",
                    "session_id": summary.session_id,
                    "base_revision": int(meta[0]) if meta else 0,
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
        for item in acknowledgements:
            session_id = str(item.get("session_id") or "")
            revision = item.get("revision")
            raw_ids = item.get("event_ids")
            if raw_ids is None:
                raw_ids = [item.get("event_id")] if item.get("event_id") else []
            if not session_id or not isinstance(revision, int) or not isinstance(raw_ids, list):
                continue
            event_ids = [str(value) for value in raw_ids if value]
            if not event_ids:
                continue
            with self._connection(session_id) as connection:
                placeholders = ",".join("?" for _ in event_ids)
                connection.execute(
                    f"UPDATE json_events SET acknowledged_at=? WHERE session_id=? AND event_id IN ({placeholders})",
                    [utc_now(), session_id, *event_ids],
                )
                connection.execute(
                    "UPDATE store_metadata SET remote_revision=MAX(remote_revision,?),updated_at=? WHERE session_id=?",
                    (int(revision), utc_now(), session_id),
                )

    def remote_revision(self, session_id: str) -> int:
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT remote_revision FROM store_metadata WHERE session_id=?", (session_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def event_head(self, session_id: str) -> int:
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT local_revision FROM store_metadata WHERE session_id=?", (session_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def apply_sync_events(self, item: dict[str, object], *, local_device_id: str) -> int:
        session_id = str(item.get("session_id") or "")
        revision = int(item.get("revision", 0))
        parent_revision = int(item.get("parent_revision", max(revision - 1, 0)))
        events = item.get("events")
        if (
            not session_id
            or revision < 1
            or not isinstance(events, list)
            or not all(isinstance(event, dict) for event in events)
        ):
            raise ValueError("Invalid remote event batch.")
        if not events:
            return self.remote_revision(session_id) if self.paths.session_db(session_id).exists() else 0
        if not self.paths.session_db(session_id).exists():
            existing_revision = 0
        else:
            existing_revision = self.remote_revision(session_id)
        if revision <= existing_revision:
            return existing_revision
        if parent_revision != existing_revision:
            raise ValueError("Remote event parent revision does not match the local head.")
        previous_sequence = 0
        for event in events:
            sequence = int(event.get("sequence", 0))
            if sequence <= previous_sequence:
                raise ValueError("Remote events are out of order.")
            previous_sequence = sequence
            payload_raw = json.dumps(event.get("payload"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if hashlib.sha256(payload_raw.encode("utf-8")).hexdigest() != str(event.get("checksum") or ""):
                raise ValueError("Remote event checksum mismatch.")
        first = events[0]
        if not self.paths.session_db(session_id).exists():
            if str(first.get("kind")) != "baseline":
                raise ValueError("A new session must begin with a baseline event.")
            payload = first.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("Baseline event payload is invalid.")
            objects = payload.get("objects")
            if not isinstance(objects, list):
                raise ValueError("Baseline event is missing objects.")
            self.paths.ensure_session(session_id)
        with self._connection(session_id) as connection:
            if existing_revision == 0 and self._json_object(connection, session_id, "session", session_id) is None:
                self._apply_event_payload(
                    connection,
                    session_id,
                    "baseline",
                    dict(first.get("payload") or {}),
                    local_device_id=local_device_id,
                )
            for event in events:
                event_id = str(event.get("event_id") or "")
                if not event_id:
                    raise ValueError("Remote event is missing event_id.")
                if connection.execute("SELECT 1 FROM json_events WHERE event_id=?", (event_id,)).fetchone() is not None:
                    continue
                self._apply_event_payload(
                    connection,
                    session_id,
                    str(event.get("kind") or ""),
                    dict(event.get("payload") or {}),
                    local_device_id=local_device_id,
                )
                raw = json.dumps(event.get("payload"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                next_sequence = connection.execute(
                    "SELECT COALESCE(MAX(local_sequence),0)+1 FROM json_events WHERE session_id=?", (session_id,)
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO json_events(session_id,local_sequence,event_id,base_revision,applied_revision,kind,payload_json,checksum,created_at,acknowledged_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        int(next_sequence),
                        event_id,
                        parent_revision,
                        revision,
                        str(event.get("kind") or ""),
                        raw,
                        str(event.get("checksum") or ""),
                        str(event.get("created_at") or utc_now()),
                        utc_now(),
                    ),
                )
            now = utc_now()
            connection.execute(
                "UPDATE store_metadata SET remote_revision=?,updated_at=? WHERE session_id=?",
                (revision, now, session_id),
            )
        return revision

    def _apply_event_payload(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        kind: str,
        payload: dict[str, object],
        *,
        local_device_id: str,
    ) -> None:
        if kind == "baseline":
            objects = payload.get("objects")
            if not isinstance(objects, list):
                raise ValueError("Baseline event is missing objects.")
            for item in objects:
                if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                    raise ValueError("Baseline contains an invalid object.")
                namespace = str(item.get("namespace") or "")
                object_id = str(item.get("object_id") or "")
                if not namespace or not object_id:
                    raise ValueError("Baseline contains an invalid object key.")
                value = dict(item["payload"])
                if namespace == "session":
                    value.setdefault("owner_device_id", local_device_id)
                    value["read_only"] = True
                    if str(value.get("session_id") or session_id) != session_id:
                        raise ValueError("Baseline session id does not match its envelope.")
                self._put_json_object(
                    connection, session_id, namespace, object_id, value, str(item.get("updated_at") or utc_now())
                )
            connection.execute(
                "INSERT OR IGNORE INTO store_metadata(session_id,schema_version,created_at,updated_at) VALUES (?,?,?,?)",
                (session_id, SCHEMA_VERSION, utc_now(), utc_now()),
            )
            return
        if kind in {"node_upserted", "node_finalized"}:
            node = payload.get("node")
            if not isinstance(node, dict) or str(node.get("session_id") or session_id) != session_id:
                raise ValueError("Node event payload is invalid.")
            self._put_json_object(
                connection,
                session_id,
                "runtime_node",
                str(node.get("id") or ""),
                node,
                str(node.get("timestamp") or utc_now()),
            )
            return
        if kind == "run_upserted":
            run = payload.get("run")
            if not isinstance(run, dict) or not str(run.get("run_id") or ""):
                raise ValueError("Run event payload is invalid.")
            self._put_json_object(
                connection, session_id, "run", str(run["run_id"]), run, str(run.get("updated_at") or utc_now())
            )
            return
        if kind in {"runtime_state_saved", "checkpoint_recorded"}:
            state = payload.get("state")
            if isinstance(state, dict):
                namespace = "checkpoint" if kind == "checkpoint_recorded" else "runtime_state"
                object_id = (
                    f"{payload.get('run_id') or session_id}:{payload.get('created_at') or utc_now()}:{payload.get('reason') or ''}"
                    if namespace == "checkpoint"
                    else session_id
                )
                if namespace == "runtime_state":
                    existing_state = self._json_object(connection, session_id, namespace, object_id) or {}
                    existing_run = existing_state.get("current_run")
                    incoming_run = (
                        payload.get("state", {}).get("current_run") if isinstance(payload.get("state"), dict) else None
                    )
                    if isinstance(existing_run, dict) and isinstance(incoming_run, dict):
                        merged_run = dict(existing_run)
                        merged_run.update(incoming_run)
                        merged_state = dict(existing_state)
                        merged_state.update(state)
                        merged_state["current_run"] = merged_run
                        state = merged_state
                self._put_json_object(
                    connection,
                    session_id,
                    namespace,
                    object_id,
                    payload if namespace == "checkpoint" else state,
                    str(payload.get("created_at") or utc_now()),
                )
            return
        if kind == "runtime_state_delta":
            run_id = str(payload.get("run_id") or "")
            document = self._json_object(connection, session_id, "runtime_state", session_id) or {
                "session_id": session_id,
                "current_run": None,
                "run_history": [],
            }
            run = document.get("current_run") if isinstance(document.get("current_run"), dict) else None
            if run is not None and (not run_id or str(run.get("run_id") or "") == run_id):
                for field in ("history", "actions", "events"):
                    additions = payload.get(f"{field}_values")
                    start = payload.get(f"{field}_from", 0)
                    if isinstance(additions, list) and isinstance(start, int) and start >= 0:
                        values = run.get(field) if isinstance(run.get(field), list) else []
                        values[start:] = [item for item in additions if isinstance(item, dict)]
                        run[field] = values
                updates = payload.get("subagent_batches_upsert")
                if isinstance(updates, dict):
                    batches = run.get("subagent_batches") if isinstance(run.get("subagent_batches"), dict) else {}
                    batches.update({str(key): dict(value) for key, value in updates.items() if isinstance(value, dict)})
                    run["subagent_batches"] = batches
            run_history_additions = payload.get("run_history_append")
            if isinstance(run_history_additions, list):
                history = document.get("run_history") if isinstance(document.get("run_history"), list) else []
                history.extend(item for item in run_history_additions if isinstance(item, dict))
                document["run_history"] = history
            if run is not None:
                document["current_run"] = run
            self._put_json_object(connection, session_id, "runtime_state", session_id, document, utc_now())
            return
        if kind in {"runtime_message_appended", "turn_input_appended", "turn_started", "turn_finished"}:
            run_id = str(payload.get("run_id") or "")
            if kind == "runtime_message_appended":
                object_id = f"{run_id}:{int(payload.get('sequence', 0))}"
                self._put_json_object(
                    connection,
                    session_id,
                    "runtime_message",
                    object_id,
                    payload,
                    str(payload.get("created_at") or utc_now()),
                )
            elif run_id and payload.get("content") is not None:
                object_id = f"{run_id}:{kind}:{payload.get('created_at') or utc_now()}"
                self._put_json_object(
                    connection,
                    session_id,
                    "turn_message",
                    object_id,
                    payload,
                    str(payload.get("created_at") or utc_now()),
                )
            return
        if kind == "session_metadata_updated":
            document = self._json_object(connection, session_id, "session", session_id) or {
                "session_id": session_id,
                "owner_device_id": local_device_id,
                "read_only": True,
            }
            document.update(payload)
            document["read_only"] = True
            self._write_session_document(connection, session_id, document)
            return
        raise ValueError(f"Unsupported remote event kind: {kind}")

    @staticmethod
    def _put_json_object(
        connection: sqlite3.Connection,
        session_id: str,
        namespace: str,
        object_id: str,
        payload: dict[str, object],
        updated_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO json_objects(session_id,namespace,object_id,payload_json,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(session_id,namespace,object_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (
                session_id,
                namespace,
                object_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                updated_at,
            ),
        )

    @staticmethod
    def _json_object(
        connection: sqlite3.Connection, session_id: str, namespace: str, object_id: str
    ) -> dict[str, object] | None:
        row = connection.execute(
            "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace=? AND object_id=?",
            (session_id, namespace, object_id),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row[0]))
        return dict(value) if isinstance(value, dict) else None

    @staticmethod
    def _write_session_document(connection: sqlite3.Connection, session_id: str, payload: dict[str, object]) -> None:
        timestamp = str(payload.get("updated_at") or utc_now())
        payload["updated_at"] = timestamp
        SQLiteSyncMixin._put_json_object(connection, session_id, "session", session_id, payload, timestamp)

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection, session_id: str, *, kind: str, payload: dict[str, object]
    ) -> str | None:
        if SQLiteSyncMixin._json_object(connection, session_id, "session", session_id) is None:
            return None
        meta = connection.execute(
            "SELECT remote_revision,local_revision FROM store_metadata WHERE session_id=?", (session_id,)
        ).fetchone()
        if meta is None:
            return None
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        event_id = f"event_{uuid4().hex}"
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(local_sequence),0)+1 FROM json_events WHERE session_id=?", (session_id,)
            ).fetchone()[0]
        )
        checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        created_at = utc_now()
        connection.execute(
            "INSERT INTO json_events(session_id,local_sequence,event_id,base_revision,kind,payload_json,checksum,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, sequence, event_id, int(meta[0]), kind, raw, checksum, created_at),
        )
        connection.execute(
            "UPDATE store_metadata SET local_revision=?,updated_at=? WHERE session_id=?",
            (sequence, created_at, session_id),
        )
        return event_id


__all__ = ["SQLiteSyncMixin"]
