"""PostgreSQL persistence for durable runtime snapshots and resume transitions."""

from __future__ import annotations

import json

from backend.domain.runtime_state import RuntimeState as TreeRuntimeState
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState

from ..codec import decode_runtime_state, encode_runtime_state


class PostgresRuntimeMixin:
    """Persist current runtime state and atomic workflow transitions."""

    def create_node(self, node: TreeRuntimeState) -> None:
        if node.status != "failed":
            raise ValueError("A runtime node must be created with status='failed'.")
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM sessions WHERE session_id = %s", (node.session_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Unknown session: {node.session_id}")
            if node.parent_id:
                parent = connection.execute(
                    "SELECT 1 FROM runtime_nodes WHERE session_id=%s AND id=%s",
                    (node.parent_session_id, node.parent_id),
                ).fetchone()
                if parent is None:
                    raise ValueError("A runtime node parent must be present in the store.")
            connection.execute(
                """INSERT INTO runtime_nodes (
                    session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                    compaction_idx,"user",provider,cwd,timestamp,status,data
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                self._node_values(node),
            )

    def get_node(self, session_id: str, node_id: str) -> TreeRuntimeState | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                    compaction_idx,"user",provider,cwd,timestamp,status,data
                    FROM runtime_nodes WHERE session_id=%s AND id=%s""",
                (session_id, node_id),
            ).fetchone()
        return self._node_from_row(row) if row is not None else None

    def list_children(self, parent_session_id: str, parent_id: str) -> list[TreeRuntimeState]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                    compaction_idx,"user",provider,cwd,timestamp,status,data
                    FROM runtime_nodes WHERE parent_session_id=%s AND parent_id=%s ORDER BY timestamp,id""",
                (parent_session_id, parent_id),
            ).fetchall()
        return [self._node_from_row(row) for row in rows]

    def load_nodes(self, session_id: str) -> list[TreeRuntimeState]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                    compaction_idx,"user",provider,cwd,timestamp,status,data
                    FROM runtime_nodes WHERE session_id=%s ORDER BY timestamp,id""",
                (session_id,),
            ).fetchall()
        result = {node.key: node for node in (self._node_from_row(row) for row in rows)}
        pending = list(result.values())
        while pending:
            node = pending.pop()
            if not node.parent_id:
                continue
            key = (node.parent_session_id, node.parent_id)
            if key in result:
                continue
            parent = self.get_node(*key)
            if parent is not None:
                result[key] = parent
                pending.append(parent)
        return sorted(result.values(), key=lambda item: (item.timestamp, item.id))

    def export_runtime_node_snapshot(self, session_id: str) -> dict[str, object]:
        """Export only session metadata and canonical nodes for sync."""

        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return {
            "schema_version": 3,
            "session": {
                "session_id": session.session_id,
                "title": session.title,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "client_id": session.client_id,
                "archived_at": session.archived_at,
                "deleted_at": session.deleted_at,
            },
            "nodes": [node.to_dict() for node in self.load_nodes(session_id) if node.session_id == session_id],
        }

    def apply_runtime_node_snapshot(self, snapshot: dict[str, object]) -> None:
        """Replace one session's node set after validating schema 3."""

        if int(snapshot.get("schema_version", -1)) != 3:
            raise ValueError("Only RuntimeState node snapshots (schema_version=3) are supported.")
        meta = snapshot.get("session")
        raw_nodes = snapshot.get("nodes")
        if not isinstance(meta, dict) or not isinstance(raw_nodes, list):
            raise ValueError("Node snapshot must contain session metadata and nodes.")
        if not all(isinstance(item, dict) for item in raw_nodes):
            raise ValueError("Node snapshot nodes must be objects.")
        session_id = str(meta.get("session_id") or "")
        if not session_id:
            raise ValueError("Node snapshot is missing session_id.")
        nodes = [TreeRuntimeState.from_dict(item) for item in raw_nodes]
        if any(node.session_id != session_id for node in nodes):
            raise ValueError("Node snapshot contains a node from another session.")
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM sessions WHERE session_id=%s", (session_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Unknown session: {session_id}")
            connection.execute("DELETE FROM runtime_nodes WHERE session_id=%s", (session_id,))
            for node in nodes:
                connection.execute(
                    """INSERT INTO runtime_nodes(
                        session_id,parent_session_id,id,parent_id,version,first_kept_entry_id,
                        compaction_idx,"user",provider,cwd,timestamp,status,data
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                    self._node_values(node),
                )
            connection.execute(
                """UPDATE sessions SET title=%s,created_at=%s,updated_at=%s,client_id=%s,
                    archived_at=%s,deleted_at=%s WHERE session_id=%s""",
                (
                    str(meta.get("title") or "New session"),
                    str(meta.get("created_at") or utc_now()),
                    str(meta.get("updated_at") or utc_now()),
                    meta.get("client_id"),
                    meta.get("archived_at"),
                    meta.get("deleted_at"),
                    session_id,
                ),
            )

    def finalize_node(self, node: TreeRuntimeState) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM runtime_nodes WHERE session_id=%s AND id=%s FOR UPDATE",
                (node.session_id, node.id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown runtime node: {node.session_id}/{node.id}")
            if str(row[0]) != "failed":
                raise ValueError("Sealed runtime nodes are read-only.")
            child = connection.execute(
                "SELECT 1 FROM runtime_nodes WHERE parent_session_id=%s AND parent_id=%s LIMIT 1",
                (node.session_id, node.id),
            ).fetchone()
            if child is not None:
                raise ValueError("Only a leaf runtime node can be finalized.")
            connection.execute(
                """UPDATE runtime_nodes SET parent_session_id=%s,parent_id=%s,version=%s,
                    first_kept_entry_id=%s,compaction_idx=%s,"user"=%s,provider=%s,cwd=%s,
                    timestamp=%s,status=%s,data=%s::jsonb WHERE session_id=%s AND id=%s""",
                (
                    node.parent_session_id,
                    node.parent_id,
                    node.version,
                    node.firstKeptEntryId,
                    node.compactionIdx,
                    node.user,
                    node.provider,
                    node.cwd,
                    node.timestamp,
                    node.status,
                    json.dumps(node.data, ensure_ascii=False, separators=(",", ":")),
                    node.session_id,
                    node.id,
                ),
            )

    @staticmethod
    def _node_values(node: TreeRuntimeState) -> tuple[object, ...]:
        return (
            node.session_id,
            node.parent_session_id,
            node.id,
            node.parent_id,
            node.version,
            node.firstKeptEntryId,
            node.compactionIdx,
            node.user,
            node.provider,
            node.cwd,
            node.timestamp,
            node.status,
            json.dumps(node.data, ensure_ascii=False, separators=(",", ":")),
        )

    @staticmethod
    def _node_from_row(row) -> TreeRuntimeState:
        data = row[12]
        if isinstance(data, str):
            data = json.loads(data)
        return TreeRuntimeState.from_dict(
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
                "data": data,
            }
        )

    def save(self, runtime, reason: str) -> None:
        state = runtime.state
        run = runtime.run
        payload = self._snapshot_payload(state)
        timestamp = utc_now()
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM sessions WHERE session_id = %s", (state.session_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Unknown session: {state.session_id}")
            connection.execute(
                """INSERT INTO runs (run_id, status, state_json, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status, state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at
                """,
                (run.run_id, run.status, payload, timestamp),
            )
            connection.execute(
                "INSERT INTO checkpoints (run_id, reason, state_json, created_at) VALUES (%s, %s, %s, %s)",
                (run.run_id, reason, payload, timestamp),
            )
            connection.execute(
                """INSERT INTO session_runtime (session_id, state_json, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at
                """,
                (state.session_id, payload, timestamp),
            )
            connection.execute(
                "UPDATE session_runs SET status = %s, updated_at = %s WHERE run_id = %s AND session_id = %s",
                (run.status, timestamp, run.run_id, state.session_id),
            )
            self._save_latest_runtime_message(connection, state.session_id, run.run_id, run.runtime_messages)
            connection.execute(
                "UPDATE sessions SET updated_at = %s WHERE session_id = %s", (timestamp, state.session_id)
            )

    def save_runtime(self, state: RuntimeState) -> None:
        payload = self._snapshot_payload(state)
        timestamp = utc_now()
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM sessions WHERE session_id = %s", (state.session_id,)).fetchone()
            if exists is None:
                raise ValueError(f"Unknown session: {state.session_id}")
            connection.execute(
                """INSERT INTO session_runtime (session_id, state_json, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at
                """,
                (state.session_id, payload, timestamp),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = %s WHERE session_id = %s", (timestamp, state.session_id)
            )

    def load_runtime(self, session_id: str) -> RuntimeState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM session_runtime WHERE session_id = %s", (session_id,)
            ).fetchone()
        if row is None:
            return None
        state = decode_runtime_state(row[0])
        if state.current_run is not None:
            state.current_run.runtime_messages = self.load_runtime_messages(session_id, state.current_run.run_id)
        return state

    def resume_runtime(self, source: RuntimeState, resumed: RuntimeState) -> None:
        """Atomically archive one attempt and install its resumed successor."""

        if source.session_id != resumed.session_id or source.current_run is None or resumed.current_run is None:
            raise ValueError("Resume transition must contain two attempts from the same session.")
        source_run = source.current_run
        resumed_run = resumed.current_run
        timestamp = utc_now()
        source_payload = self._snapshot_payload(source)
        resumed_payload = self._snapshot_payload(resumed)
        origin = resumed_run.provenance
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE session_runs SET status = %s, updated_at = %s WHERE run_id = %s AND session_id = %s",
                (source_run.status, timestamp, source_run.run_id, source.session_id),
            )
            if updated.rowcount == 0:
                raise ValueError(f"Unknown session run: {source_run.run_id}")
            connection.execute(
                """INSERT INTO runs (run_id, status, state_json, updated_at) VALUES (%s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status, state_json = EXCLUDED.state_json,
                updated_at = EXCLUDED.updated_at""",
                (source_run.run_id, source_run.status, source_payload, timestamp),
            )
            connection.execute(
                "INSERT INTO checkpoints (run_id, reason, state_json, created_at) VALUES (%s, %s, %s, %s)",
                (source_run.run_id, f"run_{source_run.status}", source_payload, timestamp),
            )
            self._save_runtime_messages(connection, source.session_id, source_run.run_id, source_run.runtime_messages)
            connection.execute(
                """INSERT INTO session_runs (
                    run_id, session_id, task, status, workflow_id, attempt, origin_kind,
                    source_session_id, source_run_id, started_at, updated_at
                ) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s, %s, %s, %s)""",
                (
                    resumed_run.run_id,
                    resumed.session_id,
                    resumed_run.task,
                    origin.workflow_id,
                    origin.attempt,
                    origin.trigger,
                    origin.source_session_id,
                    origin.source_run_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO runs (run_id, status, state_json, updated_at) VALUES (%s, %s, %s, %s)",
                (resumed_run.run_id, resumed_run.status, resumed_payload, timestamp),
            )
            connection.execute(
                "INSERT INTO checkpoints (run_id, reason, state_json, created_at) VALUES (%s, %s, %s, %s)",
                (resumed_run.run_id, "run_resumed", resumed_payload, timestamp),
            )
            self._save_runtime_messages(
                connection, resumed.session_id, resumed_run.run_id, resumed_run.runtime_messages
            )
            connection.execute(
                """INSERT INTO session_runtime (session_id, state_json, updated_at) VALUES (%s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at""",
                (resumed.session_id, resumed_payload, timestamp),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = %s WHERE session_id = %s", (timestamp, resumed.session_id)
            )

    @staticmethod
    def _snapshot_payload(state: RuntimeState) -> str:
        return encode_runtime_state(state)
