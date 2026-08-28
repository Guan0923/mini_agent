"""Shared JSON-object primitives for local runtime persistence."""

from __future__ import annotations

import json
import sqlite3

from backend.domain.runtime_state import RuntimeNode, runtime_node_from_dict


class SQLiteJsonObjectMixin:
    def _touch_session(self, connection: sqlite3.Connection, session_id: str, timestamp: str) -> None:
        document = self._session_document(connection, session_id)
        document["updated_at"] = timestamp
        self._write_session_document(connection, session_id, document)

    @staticmethod
    def _objects(connection: sqlite3.Connection, session_id: str, namespace: str) -> list[RuntimeNode]:
        values = SQLiteJsonObjectMixin._json_values(connection, session_id, namespace)
        return [runtime_node_from_dict(value) for value in values]

    @staticmethod
    def _json_values(connection: sqlite3.Connection, session_id: str, namespace: str) -> list[dict[str, object]]:
        rows = connection.execute(
            "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace=?", (session_id, namespace)
        ).fetchall()
        return [dict(value) for row in rows if isinstance(value := json.loads(str(row[0])), dict)]

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
    def _assert_writable(_connection: sqlite3.Connection) -> None:
        """All v12 sessions are local and writable after lifecycle checks."""
