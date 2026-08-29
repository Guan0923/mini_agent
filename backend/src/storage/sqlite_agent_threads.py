"""Normalized SQLite storage for logical Threads and persistent Agent nodes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
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


class SQLiteAgentThreadMixin:
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
            thread_task=str(row["thread_task"]),
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
            "INSERT INTO thread_nodes(session_id,thread_id,root_thread_id,parent_thread_id,thread_path,thread_task,thread_status,depth,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                item.session_id,
                item.thread_id,
                item.root_thread_id,
                item.parent_thread_id,
                item.thread_path,
                item.thread_task,
                item.thread_status,
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

    def create_agent_threads(self, session_id: str, items: Sequence[AgentThreadCreate]) -> list[ThreadNode]:
        if not items:
            return []
        if any(item.runtime.session_id != session_id or item.node.session_id != session_id for item in items):
            raise ValueError("Agent Thread batch must belong to one Session.")
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, session_id)
            for item in items:
                if item.runtime.thread_id != item.node.thread_id or item.context.thread_id != item.node.thread_id:
                    raise ValueError("Agent Thread batch identities do not match.")
                if item.turn is not None and (
                    item.turn.session_id != session_id
                    or item.turn.thread_id != item.node.thread_id
                    or item.runtime.current_turn_id != item.turn.id
                    or item.runtime.running_turn_id != item.turn.id
                ):
                    raise ValueError("Preallocated Agent Turn does not match its Thread record.")
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
                self._insert_runtime_thread(connection, item.runtime)
                self._insert_thread_node(connection, item.node)
                self._insert_thread_context(connection, item.context)
                if item.turn is not None:
                    parent = self._json_object(
                        connection,
                        item.turn.parent_session_id,
                        "runtime_node",
                        item.turn.parent_id,
                    )
                    if parent is None:
                        raise ValueError("Agent Turn parent does not exist.")
                    self._put_json_object(
                        connection,
                        session_id,
                        "runtime_node",
                        item.turn.id,
                        item.turn.to_dict(),
                        item.turn.timestamp,
                    )
            self._touch_session(connection, session_id, utc_now())
        return [item.node for item in items]

    def create_thread_turn_if_idle(self, node: RuntimeState, *, expected_head_id: str) -> RuntimeState:
        """CAS-create one running Turn without racing another mailbox worker."""

        if node.status != "running" or node.parent_id != expected_head_id:
            raise ValueError("Idle Thread Turn must be running and continue from the expected head.")
        with self._connection(node.session_id) as connection:
            self._assert_writable(connection)
            target = connection.execute(
                "SELECT thread_status FROM thread_nodes WHERE session_id=? AND thread_id=?",
                (node.session_id, node.thread_id),
            ).fetchone()
            if target is None:
                raise KeyError(node.thread_id)
            if str(target[0]) != "opening":
                raise ValueError("Target Agent Thread is closed.")
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
            self._touch_session(connection, node.session_id, node.timestamp)
        return node

    def has_canonical_delivery(self, session_id: str, delivery_id: str) -> bool:
        for node in self.load_nodes(session_id):
            if not isinstance(node, RuntimeState):
                continue
            if any(
                message.get("role") == "user" and message.get("delivery_id") == delivery_id
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
                "SELECT * FROM thread_nodes WHERE session_id=? ORDER BY depth,created_at,thread_id",
                (session_id,),
            ).fetchall()
        return [item for row in rows if (item := self._thread_node(row)) is not None]

    def get_thread_node(self, session_id: str, thread_id: str) -> ThreadNode | None:
        if not self.paths.session_db(session_id).exists():
            return None
        with self._connection(session_id) as connection:
            row = connection.execute("SELECT * FROM thread_nodes WHERE thread_id=?", (thread_id,)).fetchone()
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
                "SELECT * FROM thread_nodes WHERE session_id=? AND parent_thread_id=? ORDER BY created_at,thread_id",
                (session_id, parent_thread_id),
            ).fetchall()
        return [item for row in rows if (item := self._thread_node(row)) is not None]

    def update_thread_status(self, session_id: str, thread_id: str, status: str) -> ThreadNode:
        if status not in {"opening", "closed"}:
            raise ValueError("thread_status must be opening or closed.")
        timestamp = utc_now()
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            cursor = connection.execute(
                "UPDATE thread_nodes SET thread_status=?,updated_at=? WHERE session_id=? AND thread_id=?",
                (status, timestamp, session_id, thread_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(thread_id)
            row = connection.execute("SELECT * FROM thread_nodes WHERE thread_id=?", (thread_id,)).fetchone()
        result = self._thread_node(row)
        if result is None:
            raise KeyError(thread_id)
        return result

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
        task = ""
        current_turn_id = str(runtime["current_turn_id"] or "")
        if current_turn_id:
            payload = self._json_object(connection, session_id, "runtime_node", current_turn_id)
            try:
                content = payload["data"][int(payload.get("current_data_idx") or 0)][0]["content"]  # type: ignore[index,union-attr]
                task = next(
                    (str(item.get("text") or "") for item in content if item.get("type") == "text"),
                    "",
                )
            except (IndexError, KeyError, TypeError, ValueError):
                task = ""
        connection.execute(
            "INSERT INTO thread_nodes(session_id,thread_id,root_thread_id,parent_thread_id,thread_path,thread_task,thread_status,depth,created_at,updated_at) "
            "VALUES (?,?,?,NULL,'/root',?,'opening',0,?,?)",
            (session_id, thread_id, thread_id, task, timestamp, timestamp),
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


__all__ = ["AgentThreadCreate", "SQLiteAgentThreadMixin"]
