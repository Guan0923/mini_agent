"""Transactional Runtime frame outbox stored inside the existing JSON-object schema."""

from __future__ import annotations

import sqlite3
from typing import Any

from backend.domain.runtime_state import NodeFrame, RuntimeState, runtime_node_from_dict, utc_iso


class SQLiteRuntimeEventMixin:
    def _put_runtime_event(
        self,
        connection: sqlite3.Connection,
        node: RuntimeState,
        frame: NodeFrame,
    ) -> int:
        state_row = connection.execute(
            "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace='runtime_event_state' AND object_id=?",
            (node.session_id, node.id),
        ).fetchone()
        last_sequence = 0
        if state_row is not None:
            import json

            value = json.loads(str(state_row[0]))
            if isinstance(value, dict):
                last_sequence = int(value.get("last_sequence") or 0)
        sequence = last_sequence + 1
        payload: dict[str, Any] = {
            "event_id": frame.event_id,
            "session_id": node.session_id,
            "thread_id": node.thread_id,
            "turn_id": node.id,
            "sequence": sequence,
            "frame": frame.to_dict(),
            "current": node.to_dict(),
        }
        self._put_json_object(
            connection,
            node.session_id,
            "runtime_event_outbox",
            frame.event_id,
            payload,
            utc_iso(),
        )
        self._put_json_object(
            connection,
            node.session_id,
            "runtime_event_state",
            node.id,
            {"last_sequence": sequence},
            utc_iso(),
        )
        return sequence

    def runtime_stream_snapshot(self, session_id: str, turn_id: str) -> tuple[RuntimeState | None, int]:
        if not self.paths.session_db(session_id).exists():
            return None, 0
        with self._connection(session_id) as connection:
            node_payload = self._json_object(connection, session_id, "runtime_node", turn_id)
            state = self._json_object(connection, session_id, "runtime_event_state", turn_id) or {}
        if node_payload is None:
            return None, int(state.get("last_sequence") or 0)
        node = runtime_node_from_dict(node_payload)
        return (node if isinstance(node, RuntimeState) else None), int(state.get("last_sequence") or 0)

    def pending_runtime_events(self, session_id: str) -> list[dict[str, object]]:
        if not self.paths.session_db(session_id).exists():
            return []
        with self._connection(session_id) as connection:
            values = self._json_values(connection, session_id, "runtime_event_outbox")
        return sorted(values, key=lambda item: (int(item.get("sequence") or 0), str(item.get("event_id") or "")))

    def runtime_event(self, session_id: str, event_id: str) -> dict[str, object] | None:
        if not self.paths.session_db(session_id).exists():
            return None
        with self._connection(session_id) as connection:
            return self._json_object(connection, session_id, "runtime_event_outbox", event_id)

    def ack_runtime_event(self, session_id: str, event_id: str) -> None:
        with self._connection(session_id) as connection:
            connection.execute(
                "DELETE FROM json_objects WHERE session_id=? AND namespace='runtime_event_outbox' AND object_id=?",
                (session_id, event_id),
            )


__all__ = ["SQLiteRuntimeEventMixin"]
