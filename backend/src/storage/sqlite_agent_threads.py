"""Normalized SQLite storage for logical Threads and persistent Agent nodes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from backend.domain import RuntimeThread, ThreadContext, ThreadNode
from backend.domain.runtime_state import RuntimeState
from backend.domain.state import utc_now


@dataclass(frozen=True, slots=True)
class AgentThreadCreate:
    runtime: RuntimeThread
    node: ThreadNode
    context: ThreadContext
    turn: RuntimeState | None = None
    report_recipient_thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTurnReport:
    session_id: str
    turn_id: str
    agent_thread_id: str
    recipient_thread_id: str
    thread_status: str | None
    reply_content: str
    delivery_id: str
    state: str
    created_at: str
    updated_at: str


_THREAD_NODE_SELECT = (
    "SELECT node.session_id,node.thread_id,node.root_thread_id,node.parent_thread_id,node.thread_path,"
    "node.depth,node.created_at,runtime.updated_at AS updated_at,"
    "json_extract(turn.payload_json,'$.status') AS thread_status "
    "FROM thread_nodes AS node "
    "JOIN runtime_threads AS runtime ON runtime.thread_id=node.thread_id "
    "JOIN json_objects AS turn ON turn.session_id=node.session_id "
    "AND turn.namespace='runtime_node' AND turn.object_id=runtime.current_turn_id "
)


class SQLiteAgentThreadMixin:
    @staticmethod
    def _agent_turn_report(row: sqlite3.Row) -> AgentTurnReport:
        return AgentTurnReport(
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]),
            agent_thread_id=str(row["agent_thread_id"]),
            recipient_thread_id=str(row["recipient_thread_id"]),
            thread_status=str(row["thread_status"]) if row["thread_status"] is not None else None,
            reply_content=str(row["reply_content"]),
            delivery_id=str(row["delivery_id"]),
            state=str(row["state"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _report_delivery_id(turn_id: str, recipient_thread_id: str) -> str:
        digest = hashlib.sha256(f"{turn_id}\0{recipient_thread_id}".encode()).hexdigest()
        return f"agent_report_{digest}"

    @classmethod
    def _insert_agent_turn_report(
        cls,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        turn_id: str,
        agent_thread_id: str,
        recipient_thread_id: str,
        timestamp: str,
    ) -> None:
        if agent_thread_id == recipient_thread_id:
            raise ValueError("An Agent cannot register itself as a report recipient.")
        recipient = connection.execute(
            "SELECT 1 FROM runtime_threads WHERE session_id=? AND thread_id=?",
            (session_id, recipient_thread_id),
        ).fetchone()
        if recipient is None:
            raise ValueError("The report recipient is not a Thread in this Session.")
        connection.execute(
            "INSERT INTO agent_turn_reports(session_id,turn_id,agent_thread_id,recipient_thread_id,"
            "thread_status,reply_content,delivery_id,state,created_at,updated_at) "
            "VALUES (?,?,?,?,NULL,'',?,'waiting',?,?) ON CONFLICT(turn_id,recipient_thread_id) DO NOTHING",
            (
                session_id,
                turn_id,
                agent_thread_id,
                recipient_thread_id,
                cls._report_delivery_id(turn_id, recipient_thread_id),
                timestamp,
                timestamp,
            ),
        )

    @staticmethod
    def _runtime_thread(row: sqlite3.Row | None) -> RuntimeThread | None:
        if row is None:
            return None
        return RuntimeThread(
            session_id=str(row["session_id"]),
            thread_id=str(row["thread_id"]),
            origin_kind=str(row["origin_kind"]),  # type: ignore[arg-type]
            current_turn_id=str(row["current_turn_id"]) if row["current_turn_id"] is not None else None,
            running_turn_id=str(row["running_turn_id"]) if row["running_turn_id"] is not None else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _thread_node(row: sqlite3.Row | None) -> ThreadNode | None:
        if row is None:
            return None
        return ThreadNode(
            session_id=str(row["session_id"]),
            thread_id=str(row["thread_id"]),
            root_thread_id=str(row["root_thread_id"]),
            parent_thread_id=str(row["parent_thread_id"]) if row["parent_thread_id"] is not None else None,
            thread_path=str(row["thread_path"]),
            thread_status=str(row["thread_status"]),  # type: ignore[arg-type]
            depth=int(row["depth"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _thread_context(row: sqlite3.Row | None) -> ThreadContext | None:
        if row is None:
            return None
        snapshot = json.loads(str(row["snapshot_json"])) if row["snapshot_json"] is not None else None
        return ThreadContext(
            thread_id=str(row["thread_id"]),
            requested_strategy=str(row["requested_strategy"]),  # type: ignore[arg-type]
            effective_strategy=str(row["effective_strategy"]),  # type: ignore[arg-type]
            source_turn_id=str(row["source_turn_id"]),
            source_data_idx=int(row["source_data_idx"]),
            snapshot=snapshot if isinstance(snapshot, list) else None,
            summary=str(row["summary"]) if row["summary"] is not None else None,
        )

    @staticmethod
    def _insert_runtime_thread(connection: sqlite3.Connection, item: RuntimeThread) -> None:
        connection.execute(
            "INSERT INTO runtime_threads(session_id,thread_id,origin_kind,current_turn_id,running_turn_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                item.session_id,
                item.thread_id,
                item.origin_kind,
                item.current_turn_id,
                item.running_turn_id,
                item.created_at,
                item.updated_at,
            ),
        )

    @staticmethod
    def _insert_thread_node(connection: sqlite3.Connection, item: ThreadNode) -> None:
        connection.execute(
            "INSERT INTO thread_nodes(session_id,thread_id,root_thread_id,parent_thread_id,thread_path,depth,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                item.session_id,
                item.thread_id,
                item.root_thread_id,
                item.parent_thread_id,
                item.thread_path,
                item.depth,
                item.created_at,
                item.updated_at,
            ),
        )

    @staticmethod
    def _insert_thread_context(connection: sqlite3.Connection, item: ThreadContext) -> None:
        connection.execute(
            "INSERT INTO thread_contexts(thread_id,requested_strategy,effective_strategy,source_turn_id,source_data_idx,snapshot_json,summary) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                item.thread_id,
                item.requested_strategy,
                item.effective_strategy,
                item.source_turn_id,
                item.source_data_idx,
                json.dumps(item.snapshot, ensure_ascii=False, separators=(",", ":"))
                if item.snapshot is not None
                else None,
                item.summary,
            ),
        )

    def create_agent_thread(self, session_id: str, item: AgentThreadCreate) -> ThreadNode:
        if item.runtime.session_id != session_id or item.node.session_id != session_id:
            raise ValueError("Agent Thread must belong to the current Session.")
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, session_id)
            if item.runtime.thread_id != item.node.thread_id or item.context.thread_id != item.node.thread_id:
                raise ValueError("Agent Thread identities do not match.")
            if item.turn is None or (
                item.turn.session_id != session_id
                or item.turn.thread_id != item.node.thread_id
                or item.runtime.current_turn_id != item.turn.id
                or item.runtime.running_turn_id != item.turn.id
            ):
                raise ValueError("A new Agent Thread requires one matching running Turn.")
            parent = connection.execute(
                "SELECT root_thread_id,depth FROM thread_nodes WHERE session_id=? AND thread_id=?",
                (session_id, item.node.parent_thread_id),
            ).fetchone()
            if (
                parent is None
                or item.node.depth != int(parent["depth"]) + 1
                or item.node.root_thread_id != str(parent["root_thread_id"])
            ):
                raise ValueError("Agent Thread parent and root ownership do not match.")
            turn_parent = self._json_object(
                connection,
                item.turn.parent_session_id,
                "runtime_node",
                item.turn.parent_id,
            )
            if turn_parent is None:
                raise ValueError("Agent Turn parent does not exist.")
            self._insert_runtime_thread(connection, item.runtime)
            self._insert_thread_node(connection, item.node)
            self._insert_thread_context(connection, item.context)
            self._put_json_object(
                connection,
                session_id,
                "runtime_node",
                item.turn.id,
                item.turn.to_dict(),
                item.turn.timestamp,
            )
            if item.report_recipient_thread_id:
                self._insert_agent_turn_report(
                    connection,
                    session_id=session_id,
                    turn_id=item.turn.id,
                    agent_thread_id=item.node.thread_id,
                    recipient_thread_id=item.report_recipient_thread_id,
                    timestamp=item.turn.timestamp,
                )
            self._touch_session(connection, session_id, utc_now())
        return item.node

    def create_thread_turn_if_idle(
        self,
        node: RuntimeState,
        *,
        expected_head_id: str,
        report_recipient_thread_id: str | None = None,
    ) -> RuntimeState:
        """CAS-create one running Turn without racing another mailbox worker."""

        if node.status != "running" or node.parent_id != expected_head_id:
            raise ValueError("Idle Thread Turn must be running and continue from the expected head.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            target = connection.execute(
                "SELECT 1 FROM thread_nodes WHERE session_id=? AND thread_id=?",
                (node.session_id, node.thread_id),
            ).fetchone()
            if target is None:
                raise KeyError(node.thread_id)
            cursor = connection.execute(
                "UPDATE runtime_threads SET current_turn_id=?,running_turn_id=?,updated_at=? "
                "WHERE session_id=? AND thread_id=? AND current_turn_id=? AND running_turn_id IS NULL",
                (
                    node.id,
                    node.id,
                    node.timestamp,
                    node.session_id,
                    node.thread_id,
                    expected_head_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Agent Thread is no longer idle at the expected head.")
            parent = self._json_object(connection, node.parent_session_id, "runtime_node", node.parent_id)
            if parent is None:
                raise ValueError("Agent Turn parent does not exist.")
            self._put_json_object(
                connection,
                node.session_id,
                "runtime_node",
                node.id,
                node.to_dict(),
                node.timestamp,
            )
            if report_recipient_thread_id:
                self._insert_agent_turn_report(
                    connection,
                    session_id=node.session_id,
                    turn_id=node.id,
                    agent_thread_id=node.thread_id,
                    recipient_thread_id=report_recipient_thread_id,
                    timestamp=node.timestamp,
                )
            self._touch_session(connection, node.session_id, node.timestamp)
        return node

    def register_agent_turn_report(
        self,
        session_id: str,
        turn_id: str,
        agent_thread_id: str,
        recipient_thread_id: str,
    ) -> AgentTurnReport:
        timestamp = utc_now()
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            turn = self._json_object(connection, session_id, "runtime_node", turn_id)
            if turn is None or str(turn.get("thread_id") or "") != agent_thread_id:
                raise ValueError("The report Turn does not belong to the target Agent Thread.")
            self._insert_agent_turn_report(
                connection,
                session_id=session_id,
                turn_id=turn_id,
                agent_thread_id=agent_thread_id,
                recipient_thread_id=recipient_thread_id,
                timestamp=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM agent_turn_reports WHERE turn_id=? AND recipient_thread_id=?",
                (turn_id, recipient_thread_id),
            ).fetchone()
            assert row is not None
        return self._agent_turn_report(row)

    def finalize_agent_turn_reports(
        self,
        session_id: str,
        turn_id: str,
        *,
        thread_status: str,
        reply_content: str,
    ) -> list[AgentTurnReport]:
        if thread_status not in {"success", "failed"}:
            raise ValueError("Agent report status must be success or failed.")
        timestamp = utc_now()
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            rows = connection.execute(
                "SELECT * FROM agent_turn_reports WHERE session_id=? AND turn_id=? ORDER BY created_at,delivery_id",
                (session_id, turn_id),
            ).fetchall()
            for row in rows:
                existing_status = row["thread_status"]
                existing_content = str(row["reply_content"])
                if existing_status is not None and (
                    str(existing_status) != thread_status or existing_content != reply_content
                ):
                    raise ValueError("The finalized Agent report content is immutable.")
            connection.execute(
                "UPDATE agent_turn_reports SET thread_status=?,reply_content=?,updated_at=? "
                "WHERE session_id=? AND turn_id=? AND state='waiting'",
                (thread_status, reply_content, timestamp, session_id, turn_id),
            )
            result = connection.execute(
                "SELECT * FROM agent_turn_reports WHERE session_id=? AND turn_id=? ORDER BY created_at,delivery_id",
                (session_id, turn_id),
            ).fetchall()
        return [self._agent_turn_report(row) for row in result]

    def list_agent_turn_reports(
        self,
        session_id: str,
        *,
        states: tuple[str, ...] = ("waiting", "queued", "delivered"),
    ) -> list[AgentTurnReport]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._connection(session_id) as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_turn_reports WHERE session_id=? AND state IN ({placeholders}) "
                "ORDER BY created_at,delivery_id",
                (session_id, *states),
            ).fetchall()
        return [self._agent_turn_report(row) for row in rows]

    def agent_report_statuses(self, session_id: str, delivery_ids: set[str]) -> dict[str, str]:
        """Project persisted child execution status without parsing report text."""

        if not delivery_ids:
            return {}
        ordered = sorted(delivery_ids)
        placeholders = ",".join("?" for _ in ordered)
        with self._connection(session_id) as connection:
            rows = connection.execute(
                f"SELECT delivery_id,thread_status FROM agent_turn_reports "
                f"WHERE session_id=? AND delivery_id IN ({placeholders}) AND thread_status IS NOT NULL",
                (session_id, *ordered),
            ).fetchall()
        return {str(row["delivery_id"]): str(row["thread_status"]) for row in rows}

    def mark_agent_turn_report_state(self, session_id: str, delivery_id: str, state: str) -> AgentTurnReport:
        if state not in {"queued", "delivered"}:
            raise ValueError("Agent report state must be queued or delivered.")
        timestamp = utc_now()
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            row = connection.execute(
                "SELECT * FROM agent_turn_reports WHERE session_id=? AND delivery_id=?",
                (session_id, delivery_id),
            ).fetchone()
            if row is None:
                raise KeyError(delivery_id)
            current = str(row["state"])
            allowed = current == state or (current == "waiting" and state == "queued") or state == "delivered"
            if not allowed:
                raise ValueError(f"Cannot change Agent report state from {current} to {state}.")
            connection.execute(
                "UPDATE agent_turn_reports SET state=?,updated_at=? WHERE session_id=? AND delivery_id=?",
                (state, timestamp, session_id, delivery_id),
            )
            updated = connection.execute(
                "SELECT * FROM agent_turn_reports WHERE session_id=? AND delivery_id=?",
                (session_id, delivery_id),
            ).fetchone()
            assert updated is not None
        return self._agent_turn_report(updated)

    def append_agent_report(
        self,
        session_id: str,
        recipient_thread_id: str,
        *,
        delivery_id: str,
        reply_content: str,
    ) -> RuntimeState:
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            runtime = connection.execute(
                "SELECT current_turn_id FROM runtime_threads WHERE session_id=? AND thread_id=?",
                (session_id, recipient_thread_id),
            ).fetchone()
            if runtime is None or runtime["current_turn_id"] is None:
                raise ValueError("The report recipient has no canonical current Turn.")
            turn_id = str(runtime["current_turn_id"])
            payload = self._json_object(connection, session_id, "runtime_node", turn_id)
            if payload is None:
                raise ValueError("The report recipient Turn is unavailable.")
            node = RuntimeState.from_dict(payload)
            duplicate = any(
                item.get("type") == "subagent" and item.get("delivery_id") == delivery_id
                for version in node.data
                for message in version
                for item in message.get("content", [])
            )
            if not duplicate:
                node.data[node.current_data_idx].append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "subagent",
                                "event": "agent_report",
                                "status": "success",
                                "text": reply_content,
                                "delivery_id": delivery_id,
                            }
                        ],
                    }
                )
                node = RuntimeState.from_dict(node.to_dict())
                self._put_json_object(
                    connection,
                    session_id,
                    "runtime_node",
                    node.id,
                    node.to_dict(),
                    utc_now(),
                )
                self._touch_session(connection, session_id, utc_now())
        return node

    def has_canonical_delivery(self, session_id: str, delivery_id: str) -> bool:
        for node in self.load_nodes(session_id):
            if not isinstance(node, RuntimeState):
                continue
            if any(
                (message.get("role") == "user" and message.get("delivery_id") == delivery_id)
                or any(item.get("delivery_id") == delivery_id for item in message.get("content", []))
                for version in node.data
                for message in version
            ):
                return True
        return False

    def get_runtime_thread(self, session_id: str, thread_id: str) -> RuntimeThread | None:
        if not self.paths.session_db(session_id).exists():
            return None
        with self._connection(session_id) as connection:
            row = connection.execute("SELECT * FROM runtime_threads WHERE thread_id=?", (thread_id,)).fetchone()
        return self._runtime_thread(row)

    def list_runtime_threads(self, session_id: str) -> list[RuntimeThread]:
        if not self.paths.session_db(session_id).exists():
            return []
        with self._connection(session_id) as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_threads WHERE session_id=? ORDER BY created_at,thread_id", (session_id,)
            ).fetchall()
        return [item for row in rows if (item := self._runtime_thread(row)) is not None]

    def list_thread_nodes(self, session_id: str) -> list[ThreadNode]:
        if not self.paths.session_db(session_id).exists():
            return []
        with self._connection(session_id) as connection:
            rows = connection.execute(
                _THREAD_NODE_SELECT + "WHERE node.session_id=? ORDER BY node.depth,node.created_at,node.thread_id",
                (session_id,),
            ).fetchall()
        return [item for row in rows if (item := self._thread_node(row)) is not None]

    def get_thread_node(self, session_id: str, thread_id: str) -> ThreadNode | None:
        if not self.paths.session_db(session_id).exists():
            return None
        with self._connection(session_id) as connection:
            row = connection.execute(
                _THREAD_NODE_SELECT + "WHERE node.session_id=? AND node.thread_id=?",
                (session_id, thread_id),
            ).fetchone()
        return self._thread_node(row)

    def get_thread_context(self, session_id: str, thread_id: str) -> ThreadContext | None:
        if not self.paths.session_db(session_id).exists():
            return None
        with self._connection(session_id) as connection:
            row = connection.execute("SELECT * FROM thread_contexts WHERE thread_id=?", (thread_id,)).fetchone()
        return self._thread_context(row)

    def list_child_thread_nodes(self, session_id: str, parent_thread_id: str) -> list[ThreadNode]:
        with self._connection(session_id) as connection:
            rows = connection.execute(
                _THREAD_NODE_SELECT
                + "WHERE node.session_id=? AND node.parent_thread_id=? ORDER BY node.created_at,node.thread_id",
                (session_id, parent_thread_id),
            ).fetchall()
        return [item for row in rows if (item := self._thread_node(row)) is not None]

    def list_descendant_thread_nodes(self, session_id: str, ancestor_thread_id: str) -> list[ThreadNode]:
        with self._connection(session_id) as connection:
            rows = connection.execute(
                "WITH RECURSIVE descendants(thread_id) AS ("
                "SELECT thread_id FROM thread_nodes WHERE session_id=? AND parent_thread_id=? "
                "UNION ALL SELECT child.thread_id FROM thread_nodes AS child "
                "JOIN descendants AS parent ON child.parent_thread_id=parent.thread_id "
                "WHERE child.session_id=?"
                ") " + _THREAD_NODE_SELECT + "JOIN descendants ON descendants.thread_id=node.thread_id "
                "ORDER BY node.depth,node.created_at,node.thread_id",
                (session_id, ancestor_thread_id, session_id),
            ).fetchall()
        return [item for row in rows if (item := self._thread_node(row)) is not None]

    def _ensure_agent_tree_root_record(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        timestamp: str,
    ) -> None:
        runtime = connection.execute(
            "SELECT origin_kind,current_turn_id FROM runtime_threads WHERE session_id=? AND thread_id=?",
            (session_id, thread_id),
        ).fetchone()
        if runtime is None or str(runtime["origin_kind"]) not in {"main", "fork"}:
            raise ValueError("A Sidebar Thread requires a main or fork Runtime Thread.")
        existing = connection.execute(
            "SELECT root_thread_id,parent_thread_id,depth FROM thread_nodes WHERE session_id=? AND thread_id=?",
            (session_id, thread_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["root_thread_id"]) != thread_id
                or existing["parent_thread_id"] is not None
                or int(existing["depth"]) != 0
            ):
                raise ValueError("Sidebar Thread Agent-tree root is inconsistent.")
            return
        connection.execute(
            "INSERT INTO thread_nodes(session_id,thread_id,root_thread_id,parent_thread_id,thread_path,depth,created_at,updated_at) "
            "VALUES (?,?,?,NULL,'/root',0,?,?)",
            (session_id, thread_id, thread_id, timestamp, timestamp),
        )

    @staticmethod
    def _ensure_runtime_thread_record(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        origin_kind: str,
        timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO runtime_threads(session_id,thread_id,origin_kind,current_turn_id,running_turn_id,created_at,updated_at) "
            "VALUES (?,?,?,NULL,NULL,?,?) ON CONFLICT(thread_id) DO NOTHING",
            (session_id, thread_id, origin_kind, timestamp, timestamp),
        )

    @staticmethod
    def _claim_thread_turn(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        timestamp: str,
    ) -> None:
        cursor = connection.execute(
            "UPDATE runtime_threads SET current_turn_id=?,running_turn_id=?,updated_at=? "
            "WHERE session_id=? AND thread_id=? AND (running_turn_id IS NULL OR running_turn_id=?)",
            (turn_id, turn_id, timestamp, session_id, thread_id, turn_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("A thread may have only one running Turn.")

    @staticmethod
    def _set_thread_head(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        timestamp: str,
        clear_running: bool = False,
    ) -> None:
        running = (
            ",running_turn_id=CASE WHEN running_turn_id=? THEN NULL ELSE running_turn_id END" if clear_running else ""
        )
        parameters: tuple[object, ...] = (
            (turn_id, timestamp, turn_id, session_id, thread_id)
            if clear_running
            else (turn_id, timestamp, session_id, thread_id)
        )
        cursor = connection.execute(
            f"UPDATE runtime_threads SET current_turn_id=?,updated_at=?{running} WHERE session_id=? AND thread_id=?",
            parameters,
        )
        if cursor.rowcount != 1:
            raise ValueError("Unknown runtime Thread.")


__all__ = ["AgentThreadCreate", "AgentTurnReport", "SQLiteAgentThreadMixin"]
