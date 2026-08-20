"""Session metadata and transcript operations backed by JSON objects."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from backend.domain import Session, SessionSummary, message_from_dict, new_session_id
from backend.domain.runtime_state import create_root_node
from backend.domain.state import utc_now

from .codec import is_default_session_title, normalize_session_title
from .sqlite_schema import SCHEMA_VERSION


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
        cleaned = normalize_session_title(title)
        custom = not is_default_session_title(cleaned) if title_is_custom is None else title_is_custom
        timestamp = utc_now()
        session = Session(
            new_session_id(),
            cleaned,
            timestamp,
            timestamp,
            client_id=client_id,
            local_only=local_only,
            title_is_custom=custom,
        )
        root_path = self.paths.session_root(session.session_id)
        root_existed = root_path.exists()
        root = self.paths.ensure_session(session.session_id)
        document = self._session_payload(session, owner_device_id=self.device_id, read_only=False)
        root_node = create_root_node(session.session_id, parent=root_parent, timestamp=timestamp)
        try:
            with self._connection(session.session_id) as connection:
                connection.execute(
                    "INSERT INTO store_metadata(session_id,schema_version,created_at,updated_at) VALUES (?,?,?,?)",
                    (session.session_id, SCHEMA_VERSION, timestamp, timestamp),
                )
                self._put_json_object(
                    connection, session.session_id, "session", session.session_id, document, timestamp
                )
                self._put_json_object(
                    connection,
                    session.session_id,
                    "runtime_node",
                    root_node.id,
                    root_node.to_dict(),
                    root_node.timestamp,
                )
                # The initial batch is the baseline.  Every later mutation is
                # represented by a small immutable event.
                self._append_event(
                    connection,
                    session.session_id,
                    kind="baseline",
                    payload={
                        "schema_version": SCHEMA_VERSION,
                        "objects": [
                            {
                                "namespace": "session",
                                "object_id": session.session_id,
                                "payload": document,
                                "updated_at": timestamp,
                            },
                            {
                                "namespace": "runtime_node",
                                "object_id": root_node.id,
                                "payload": root_node.to_dict(),
                                "updated_at": root_node.timestamp,
                            },
                        ],
                    },
                )
        except Exception:
            if not root_existed:
                shutil.rmtree(root, ignore_errors=True)
            raise
        return session

    @staticmethod
    def _session_payload(session: Session, *, owner_device_id: str, read_only: bool) -> dict[str, object]:
        return {
            "session_id": session.session_id,
            "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "client_id": session.client_id,
            "archived_at": session.archived_at,
            "deleted_at": session.deleted_at,
            "local_only": session.local_only,
            "title_is_custom": session.title_is_custom,
            "owner_device_id": owner_device_id,
            "read_only": read_only,
        }

    @staticmethod
    def _session_from_payload(payload: Mapping[str, object]) -> Session:
        return Session(
            str(payload.get("session_id") or ""),
            str(payload.get("title") or "New session"),
            str(payload.get("created_at") or ""),
            str(payload.get("updated_at") or payload.get("created_at") or ""),
            str(payload["client_id"]) if payload.get("client_id") is not None else None,
            str(payload["archived_at"]) if payload.get("archived_at") is not None else None,
            str(payload["deleted_at"]) if payload.get("deleted_at") is not None else None,
            bool(payload.get("local_only", False)),
            bool(payload.get("title_is_custom", False)),
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

    def _session_document(self, connection: sqlite3.Connection, session_id: str) -> dict[str, object]:
        payload = self._json_object(connection, session_id, "session", session_id)
        if payload is None:
            raise ValueError(f"Unknown session: {session_id}")
        return payload

    def _write_session_document(
        self, connection: sqlite3.Connection, session_id: str, payload: dict[str, object]
    ) -> None:
        timestamp = str(payload.get("updated_at") or utc_now())
        payload["updated_at"] = timestamp
        self._put_json_object(connection, session_id, "session", session_id, payload, timestamp)
        connection.execute("UPDATE store_metadata SET updated_at=? WHERE session_id=?", (timestamp, session_id))

    def get_session(self, session_id: str) -> Session | None:
        if not self.paths.session_db(session_id).is_file():
            return None
        with self._connection(session_id) as connection:
            payload = self._json_object(connection, session_id, "session", session_id)
        return self._session_from_payload(payload) if payload is not None else None

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        nodes = [node for node in self.load_nodes(session_id) if node.session_id == session_id]
        messages = self._node_records(nodes)
        meaningful = [node for node in nodes if getattr(node, "data_type", "") != "root"]
        last_node = max(meaningful, key=lambda item: (item.timestamp, item.id), default=None)
        with self._connection(session_id) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace='run' ORDER BY updated_at DESC,object_id DESC",
                (session_id,),
            ).fetchall()
        runs = [dict(json.loads(str(row[0]))) for row in rows]
        last_run = runs[0] if runs else None
        return SessionSummary(
            session_id=session.session_id,
            title=session.title,
            created_at=session.created_at,
            updated_at=session.updated_at,
            message_count=sum(1 for item in messages if item["role"] in {"user", "assistant"}),
            last_run_id=str(last_run.get("run_id")) if last_run else None,
            last_run_status=str(last_run.get("status")) if last_run else None,
            client_id=session.client_id,
            archived_at=session.archived_at,
            deleted_at=session.deleted_at,
            last_node_id=last_node.id if last_node is not None else None,
            local_only=session.local_only,
            title_is_custom=session.title_is_custom,
        )

    def list_sessions(self, *, state: str = "active") -> list[SessionSummary]:
        if state not in {"active", "archived", "deleted", "all"}:
            raise ValueError(f"Unknown session state: {state}")
        summaries = [
            summary
            for directory in self.paths.runtime_dir.iterdir()
            if directory.is_dir()
            and not directory.is_symlink()
            and not (directory / "state.db").is_symlink()
            and (directory / "state.db").is_file()
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
        return self._node_messages(self.load_nodes(session_id))

    def load_conversation_records(self, session_id: str) -> list[dict[str, str | int | None]]:
        return self._node_records(self.load_nodes(session_id))

    def find_session_by_client_id(self, client_id: str, *, include_deleted: bool = False) -> Session | None:
        if not client_id:
            return None
        for summary in self.list_sessions(state="all"):
            if summary.deleted_at is not None and not include_deleted:
                continue
            session = self.get_session(summary.session_id)
            if session is not None and session.client_id == client_id:
                return session
        return None

    def _update_session(self, connection: sqlite3.Connection, session_id: str, **values: object) -> None:
        document = self._session_document(connection, session_id)
        document.update(values)
        timestamp = str(values.get("updated_at") or utc_now())
        document["updated_at"] = timestamp
        self._write_session_document(connection, session_id, document)
        self._append_event(
            connection,
            session_id,
            kind="session_metadata_updated",
            payload={**values, "updated_at": timestamp},
        )

    def set_client_id(self, session_id: str, client_id: str | None) -> Session:
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            document = self._session_document(connection, session_id)
            if document.get("deleted_at") is not None:
                raise ValueError("Deleted sessions cannot change their client binding.")
            self._update_session(connection, session_id, client_id=client_id)
        return self._required_session(session_id)

    def mark_local_only(self, session_id: str) -> Session:
        with self._connection_for_existing(session_id) as connection:
            document = self._session_document(connection, session_id)
            document["local_only"] = True
            self._write_session_document(connection, session_id, document)
        return self._required_session(session_id)

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
        if client_id and not force_new:
            existing = self.find_session_by_client_id(client_id)
            if existing is not None:
                return existing
        parsed = [message_from_dict(item) for item in messages if item.get("role") in {"user", "assistant"}]
        if title_is_custom is None:
            title_is_custom = not is_default_session_title(normalize_session_title(title))
        if not title_is_custom:
            first_user = next((item for item in parsed if str(getattr(item, "role", "")) == "user"), None)
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

    def rename_session(self, session_id: str, title: str, *, title_is_custom: bool | None = None) -> Session:
        if not title.strip():
            raise ValueError("Session title cannot be empty.")
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            document = self._session_document(connection, session_id)
            if document.get("deleted_at") is not None:
                raise ValueError("Deleted sessions cannot be renamed.")
            self._update_session(
                connection,
                session_id,
                title=normalize_session_title(title),
                title_is_custom=True if title_is_custom is None else title_is_custom,
            )
        return self._required_session(session_id)

    def archive_session(self, session_id: str) -> Session:
        return self._set_lifecycle(session_id, archived_at=utc_now())

    def restore_session(self, session_id: str) -> Session:
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            document = self._session_document(connection, session_id)
            if document.get("deleted_at") is not None:
                raise ValueError("Deleted sessions cannot be restored.")
            self._update_session(connection, session_id, archived_at=None)
        return self._required_session(session_id)

    def delete_session(self, session_id: str) -> Session:
        return self._set_lifecycle(session_id, deleted_at=utc_now())

    def purge_session(self, session_id: str) -> None:
        with self._connection_for_existing(session_id) as connection:
            self._assert_not_running(connection)
            document = self._session_document(connection, session_id)
            if document.get("deleted_at") is None:
                raise ValueError("Only deleted sessions can be permanently removed.")
        local_only = self._is_local_only(session_id)
        shutil.rmtree(self.paths.session_root(session_id), ignore_errors=False)
        if self._sync_listener is not None and not local_only:
            self._sync_listener()

    def _set_lifecycle(
        self, session_id: str, *, archived_at: str | None = None, deleted_at: str | None = None
    ) -> Session:
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            self._assert_not_running(connection)
            document = self._session_document(connection, session_id)
            if document.get("deleted_at") is not None and deleted_at is None:
                raise ValueError("Deleted sessions cannot be changed.")
            values: dict[str, object] = {}
            if archived_at is not None:
                values["archived_at"] = archived_at
            if deleted_at is not None:
                values["deleted_at"] = deleted_at
            self._update_session(connection, session_id, **values)
        return self._required_session(session_id)

    def _required_session(self, session_id: str) -> Session:
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
        row = connection.execute(
            "SELECT 1 FROM json_objects WHERE namespace='run' AND json_extract(payload_json,'$.status')='running' LIMIT 1"
        ).fetchone()
        if row is not None:
            raise RuntimeError("The session has a running turn.")

    @staticmethod
    def _node_messages(nodes: list) -> list[dict[str, str]]:
        return [
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in SQLiteSessionMixin._node_records(nodes)
        ]

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
        if before_id is not None:
            records = records[: max(0, before_id)]
        page = records[-limit:]
        next_before = max(0, len(records) - limit) if len(records) > limit else None
        return ([{"role": str(item["role"]), "content": str(item["content"])} for item in page], next_before)


__all__ = ["SQLiteSessionMixin"]
