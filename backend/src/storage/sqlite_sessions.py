"""Session metadata and transcript operations for SQLite storage."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from backend.domain import (
    Session,
    SessionSummary,
    message_from_dict,
    new_session_id,
)
from backend.domain.state import utc_now

from .codec import is_default_session_title, normalize_session_title


class SQLiteSessionMixin:
    def create_session(
        self,
        title: str | None = None,
        *,
        client_id: str | None = None,
        local_only: bool = False,
        root_parent: tuple[str, str] | None = None,
        title_is_custom: bool | None = None,
    ) -> Session:
        title_is_custom = (
            not is_default_session_title(normalize_session_title(title))
            if title_is_custom is None
            else title_is_custom
        )
        session = Session(
            new_session_id(),
            normalize_session_title(title),
            utc_now(),
            utc_now(),
            client_id=client_id,
            local_only=local_only,
            title_is_custom=title_is_custom,
        )
        root_path = self.paths.session_root(session.session_id)
        root_existed = root_path.exists()
        root = self.paths.ensure_session(session.session_id)
        try:
            with self._connection(session.session_id) as connection:
                connection.execute(
                    "INSERT INTO session_meta(session_id,title,owner_device_id,created_at,updated_at,client_id,local_only,title_is_custom) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session.session_id,
                        session.title,
                        self.device_id,
                        session.created_at,
                        session.updated_at,
                        session.client_id,
                        int(session.local_only),
                        int(session.title_is_custom),
                    ),
                )
                self._insert_session_root(
                    connection,
                    session.session_id,
                    timestamp=session.created_at,
                    parent=root_parent,
                )
                # The first event is the only complete baseline.  All later
                # writes append small JSON deltas and never re-export this
                # document into the local outbox.
                baseline = self._build_baseline(connection, session.session_id)
                self._queue(
                    connection,
                    session.session_id,
                    kind="baseline",
                    payload=baseline,
                    object_namespace="session",
                    object_id=session.session_id,
                )
        except Exception:
            # A failed schema/metadata initialization must not leave a
            # session directory that later looks like a valid conversation.
            if not root_existed:
                shutil.rmtree(root, ignore_errors=True)
            raise
        return session

    def get_session(self, session_id: str) -> Session | None:
        path = self.paths.session_db(session_id)
        if not path.exists():
            return None
        with self._connection(session_id) as connection:
            row = connection.execute(
                "SELECT session_id, title, created_at, updated_at, client_id, archived_at, deleted_at, local_only, title_is_custom FROM session_meta"
            ).fetchone()
        return self._session(row) if row else None

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        path = self.paths.session_db(session_id)
        if not path.exists():
            return None
        with self._connection(session_id) as connection:
            row = connection.execute(
                """SELECT m.session_id, m.title, m.created_at, m.updated_at,
                COALESCE(
                    NULLIF((SELECT COUNT(*) FROM runtime_nodes
                        WHERE json_extract(data_json, '$.type') = 'message'
                        AND json_extract(data_json, '$.message.role') IN ('user', 'assistant')), 0),
                    (SELECT COUNT(*) FROM session_messages
                        WHERE role IN ('user', 'assistant'))
                ),
                (SELECT n.id FROM runtime_nodes AS n
                    WHERE n.session_id = m.session_id
                    AND NOT EXISTS (
                        SELECT 1 FROM runtime_nodes AS child
                        WHERE child.parent_session_id = n.session_id AND child.parent_id = n.id
                    )
                    ORDER BY n.timestamp DESC, n.id DESC LIMIT 1),
                (SELECT run_id FROM session_runs ORDER BY updated_at DESC, run_id DESC LIMIT 1),
                (SELECT status FROM session_runs ORDER BY updated_at DESC, run_id DESC LIMIT 1),
                m.client_id, m.archived_at, m.deleted_at, m.local_only, m.title_is_custom
                FROM session_meta AS m"""
            ).fetchone()
        if row is None:
            return None
        return SessionSummary(
            session_id=str(row[0]),
            title=str(row[1]),
            created_at=str(row[2]),
            updated_at=str(row[3]),
            message_count=int(row[4]),
            last_node_id=str(row[5]) if row[5] is not None else None,
            last_run_id=str(row[6]) if row[6] is not None else None,
            last_run_status=str(row[7]) if row[7] is not None else None,
            client_id=str(row[8]) if row[8] is not None else None,
            archived_at=str(row[9]) if row[9] is not None else None,
            deleted_at=str(row[10]) if row[10] is not None else None,
            local_only=bool(row[11]),
            title_is_custom=bool(row[12]),
        )

    def list_sessions(self, *, state: str = "active") -> list[SessionSummary]:
        if state not in {"active", "archived", "deleted", "all"}:
            raise ValueError(f"Unknown session state: {state}")
        summaries = [
            summary
            for directory in self.paths.runtime_dir.iterdir()
            if (
                directory.is_dir()
                and not directory.is_symlink()
                and not (directory / "state.db").is_symlink()
                and (directory / "state.db").is_file()
            )
            if (summary := self.get_session_summary(directory.name)) is not None
            if state == "all"
            or (state == "active" and summary.is_active)
            or (state == "archived" and summary.is_archived)
            or (state == "deleted" and summary.is_deleted)
        ]
        return sorted(summaries, key=lambda item: (item.updated_at, item.session_id), reverse=True)

    def latest_session(self) -> Session | None:
        summaries = self.list_sessions()
        return self.get_session(summaries[0].session_id) if summaries else None

    def load_conversation(self, session_id: str) -> list[dict[str, str]]:
        nodes = getattr(self, "load_nodes", lambda _session_id: [])(session_id)
        # RuntimeState nodes are the only durable conversation source in
        # protocol v0.3.  Keep the legacy projection as a compatibility
        # fallback for callers that still use start_turn/finish_turn directly;
        # a real node tree remains authoritative whenever it has messages.
        projected = self._node_messages(nodes)
        if projected:
            return projected
        with self._connection(session_id) as connection:
            rows = connection.execute("SELECT role,content FROM session_messages ORDER BY id").fetchall()
        return [{"role": str(row[0]), "content": str(row[1])} for row in rows]

    def load_conversation_records(self, session_id: str) -> list[dict[str, str | int | None]]:
        """Return the durable transcript with stable row and run identifiers."""

        nodes = getattr(self, "load_nodes", lambda _session_id: [])(session_id)
        projected = self._node_records(nodes)
        if projected:
            return projected
        with self._connection(session_id) as connection:
            rows = connection.execute(
                "SELECT id,run_id,role,content,created_at FROM session_messages ORDER BY id"
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "run_id": None if str(row[1]).startswith("import_") else str(row[1]),
                "role": str(row[2]),
                "content": str(row[3]),
                "created_at": str(row[4]),
            }
            for row in rows
        ]

    def find_session_by_client_id(self, client_id: str, *, include_deleted: bool = False) -> Session | None:
        """Find a Web-owned session without assuming a global metadata database."""

        if not client_id:
            return None
        for summary in self.list_sessions(state="all"):
            if summary.deleted_at is not None and not include_deleted:
                continue
            session = self.get_session(summary.session_id)
            if session is not None and session.client_id == client_id:
                return session
        return None

    def set_client_id(self, session_id: str, client_id: str | None) -> Session:
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            row = connection.execute("SELECT deleted_at FROM session_meta").fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[0] is not None:
                raise ValueError("Deleted sessions cannot change their client binding.")
            timestamp = utc_now()
            connection.execute("UPDATE session_meta SET client_id=?, updated_at=?", (client_id, timestamp))
            self._queue(
                connection,
                session_id,
                kind="session_metadata_updated",
                payload={"client_id": client_id, "updated_at": timestamp},
            )
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def mark_local_only(self, session_id: str) -> Session:
        """Make a newly-created session local-only before any remote sync."""

        with self._connection_for_existing(session_id) as connection:
            connection.execute("UPDATE session_meta SET local_only=1 WHERE session_id=?", (session_id,))
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def import_conversation(
        self,
        title: str | None,
        messages: list[dict[str, str]],
        *,
        client_id: str | None = None,
        force_new: bool = False,
        local_only: bool = False,
        title_is_custom: bool | None = None,
    ) -> Session:
        """Create an idle session seeded with text-only legacy Web history.

        Without an explicit custom title, the default placeholder is replaced
        by the first imported user message; the imported session then keeps
        automatic naming until the user renames it.
        """

        if client_id and not force_new:
            existing = self.find_session_by_client_id(client_id)
            if existing is not None:
                return existing
        parsed = [message_from_dict(item) for item in messages if item.get("role") in {"user", "assistant"}]
        if title_is_custom is None:
            title_is_custom = not is_default_session_title(normalize_session_title(title))
        if not title_is_custom:
            first_user = next(
                (message for message in parsed if str(getattr(message, "role", "")) == "user"),
                None,
            )
            if first_user is not None:
                title = normalize_session_title(str(getattr(first_user, "content", "") or ""))
        session = self.create_session(
            title,
            client_id=client_id,
            local_only=local_only,
            title_is_custom=title_is_custom,
        )
        try:
            if parsed:
                from backend.domain.runtime_state import NodeWriter, message_payload

                writer = NodeWriter(self)
                parent = self.get_session_root(session.session_id)
                if parent is None:
                    raise RuntimeError("Session root was not created.")
                for message in parsed:
                    role = "assistant" if getattr(message, "role", "") == "assistant" else "user"
                    node = writer.create(
                        session_id=session.session_id,
                        parent=parent,
                        data=message_payload(role, str(getattr(message, "content", "") or "")),
                    )
                    parent = writer.delete(node.session_id, node.id)
        except Exception:
            shutil.rmtree(self.paths.session_root(session.session_id), ignore_errors=True)
            raise
        return session

    def rename_session(
        self,
        session_id: str,
        title: str,
        *,
        title_is_custom: bool | None = None,
    ) -> Session:
        """Rename a non-deleted session and preserve the update in sync history.

        A manual rename locks the title so automatic naming never overwrites
        it.  Internal renames (rewind inheritance) may pass the source flag
        explicitly to keep automatic naming alive.
        """

        if not title.strip():
            raise ValueError("Session title cannot be empty.")
        cleaned = normalize_session_title(title)
        if title_is_custom is None:
            title_is_custom = True
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            row = connection.execute("SELECT deleted_at FROM session_meta").fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[0] is not None:
                raise ValueError("Deleted sessions cannot be renamed.")
            timestamp = utc_now()
            connection.execute(
                "UPDATE session_meta SET title=?, title_is_custom=?, updated_at=?",
                (cleaned, int(title_is_custom), timestamp),
            )
            self._queue(
                connection,
                session_id,
                kind="session_metadata_updated",
                payload={"title": cleaned, "title_is_custom": bool(title_is_custom), "updated_at": timestamp},
            )
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def archive_session(self, session_id: str) -> Session:
        return self._set_lifecycle(session_id, archived_at=utc_now())

    def restore_session(self, session_id: str) -> Session:
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            row = connection.execute("SELECT archived_at, deleted_at FROM session_meta").fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[1] is not None:
                raise ValueError("Deleted sessions cannot be restored.")
            timestamp = utc_now()
            connection.execute("UPDATE session_meta SET archived_at=NULL, updated_at=?", (timestamp,))
            self._queue(
                connection,
                session_id,
                kind="session_metadata_updated",
                payload={"archived_at": None, "updated_at": timestamp},
            )
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    def delete_session(self, session_id: str) -> Session:
        """Soft-delete a session; its SQLite database remains available for audit."""

        return self._set_lifecycle(session_id, deleted_at=utc_now())

    def purge_session(self, session_id: str) -> None:
        """Permanently remove a deleted, idle session and its file payload."""

        with self._connection_for_existing(session_id) as connection:
            self._assert_not_running(connection)
            row = connection.execute("SELECT deleted_at FROM session_meta").fetchone()
            if row is None or row[0] is None:
                raise ValueError("Only deleted sessions can be permanently removed.")
        local_only = self._is_local_only(session_id)
        shutil.rmtree(self.paths.session_root(session_id), ignore_errors=False)
        if self._sync_listener is not None and not local_only:
            self._sync_listener()

    def _set_lifecycle(
        self,
        session_id: str,
        *,
        archived_at: str | None = None,
        deleted_at: str | None = None,
    ) -> Session:
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            row = connection.execute("SELECT deleted_at FROM session_meta").fetchone()
            if row is None:
                raise ValueError(f"Unknown session: {session_id}")
            if row[0] is not None and deleted_at is None:
                raise ValueError("Deleted sessions cannot be changed.")
            assignments: list[str] = ["updated_at=?"]
            values: list[object] = [utc_now()]
            if archived_at is not None:
                assignments.append("archived_at=?")
                values.append(archived_at)
            if deleted_at is not None:
                assignments.append("deleted_at=?")
                values.append(deleted_at)
            values.append(session_id)
            connection.execute(
                f"UPDATE session_meta SET {', '.join(assignments)} WHERE session_id=?",
                values,
            )
            self._queue(
                connection,
                session_id,
                kind="session_metadata_updated",
                payload={
                    "archived_at": archived_at,
                    "deleted_at": deleted_at,
                    "updated_at": values[0],
                },
            )
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session: {session_id}")
        return session

    @contextmanager
    def _connection_for_existing(self, session_id: str) -> Iterator[sqlite3.Connection]:
        if not self.paths.session_db(session_id).exists():
            raise ValueError(f"Unknown session: {session_id}")
        with self._connection(session_id) as connection:
            yield connection

    @staticmethod
    def _assert_not_running(connection: sqlite3.Connection) -> None:
        if connection.execute("SELECT 1 FROM session_runs WHERE status='running' LIMIT 1").fetchone() is not None:
            raise RuntimeError("The session has a running turn.")

    @staticmethod
    def _node_messages(nodes: list) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for node in nodes:
            data = getattr(node, "data", {})
            if not isinstance(data, Mapping) or data.get("type") != "message":
                continue
            message = data.get("message")
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "")
            if role not in {"user", "assistant", "tool_result", "bash"}:
                continue
            content = message.get("content", [])
            text = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, Mapping) and block.get("type") in {"text", "reasoning", "bash"}
            )
            result.append({"role": "assistant" if role == "tool_result" else role, "content": text})
        return result

    @classmethod
    def _node_records(cls, nodes: list) -> list[dict[str, str | int | None]]:
        result: list[dict[str, str | int | None]] = []
        for node in nodes:
            data = getattr(node, "data", {})
            if not isinstance(data, Mapping) or data.get("type") != "message":
                continue
            message = data.get("message")
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "")
            if role not in {"user", "assistant", "tool_result", "bash"}:
                continue
            content = message.get("content", [])
            text = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, Mapping) and block.get("type") in {"text", "reasoning", "bash"}
            )
            result.append(
                {
                    "id": f"{node.session_id}:{node.id}",
                    "run_id": None,
                    "role": "assistant" if role == "tool_result" else role,
                    "content": text,
                    "created_at": node.timestamp,
                }
            )
        return result

    def load_conversation_page(
        self, session_id: str, *, before_id: int | None = None, limit: int = 100
    ) -> tuple[list[dict[str, str]], int | None]:
        if limit < 1:
            raise ValueError("limit must be positive")
        records = self.load_conversation_records(session_id)
        # Node ids are opaque strings in v0.3; retain a simple offset cursor
        # for the old endpoint shape without consulting session_messages.
        if before_id is not None:
            records = records[: max(0, before_id)]
        page = records[-limit:]
        next_before = max(0, len(records) - limit) if len(records) > limit else None
        return ([{"role": str(item["role"]), "content": str(item["content"])} for item in page], next_before)
