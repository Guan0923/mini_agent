"""Turn Trace persistence on the Session JSON-object store."""

from __future__ import annotations

import json

from backend.domain.runtime_state import RuntimeState
from backend.domain.turn_trace import TurnTraceRequest


class SQLiteTurnTraceMixin:
    _TRACE_NAMESPACE = "turn_trace"

    def save_turn_trace(self, session_id: str, trace: TurnTraceRequest) -> None:
        if trace.data_idx < 0:
            raise ValueError("Trace data_idx must be non-negative.")
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            node = self._json_object(connection, session_id, "runtime_node", trace.turn_id)
            if node is None:
                raise ValueError(f"Unknown Turn: {trace.turn_id}")
            turn = RuntimeState.from_dict(node)
            if turn.thread_id != trace.thread_id:
                raise ValueError("Trace thread_id does not match its Turn.")
            if trace.data_idx >= len(turn.data):
                raise ValueError("Trace data_idx is out of range.")
            existing = connection.execute(
                "SELECT 1 FROM json_objects WHERE session_id=? AND namespace=? AND object_id=?",
                (session_id, self._TRACE_NAMESPACE, trace.object_id),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"Duplicate model exchange: {trace.exchange_id}")
            self._put_json_object(
                connection,
                session_id,
                self._TRACE_NAMESPACE,
                trace.object_id,
                trace.to_dict(),
                trace.timestamp,
            )

    def next_turn_trace_sequence(self, session_id: str, turn_id: str, data_idx: int) -> int:
        with self._connection_for_existing(session_id) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace=?",
                (session_id, self._TRACE_NAMESPACE),
            ).fetchall()
        sequences = [
            int(payload.get("sequence") or 0)
            for row in rows
            if isinstance(payload := json.loads(str(row[0])), dict)
            and payload.get("turn_id") == turn_id
            and payload.get("data_idx") == data_idx
        ]
        return max(sequences, default=0) + 1

    def load_turn_trace(self, session_id: str, turn_id: str, data_idx: int) -> list[TurnTraceRequest]:
        with self._connection_for_existing(session_id) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM json_objects WHERE session_id=? AND namespace=?",
                (session_id, self._TRACE_NAMESPACE),
            ).fetchall()
        traces = [
            TurnTraceRequest.from_dict(payload)
            for row in rows
            if isinstance(payload := json.loads(str(row[0])), dict)
            and payload.get("turn_id") == turn_id
            and payload.get("data_idx") == data_idx
        ]
        return sorted(traces, key=lambda item: (item.sequence, item.timestamp, item.exchange_id))


__all__ = ["SQLiteTurnTraceMixin"]
