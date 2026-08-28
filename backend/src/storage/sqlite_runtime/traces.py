"""Aggregated Turn Trace persistence on the Session JSON-object store."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from backend.domain.runtime_state import RuntimeState
from backend.domain.turn_trace import TurnTrace, TurnTraceItem


class SQLiteTurnTraceMixin:
    _TRACE_NAMESPACE = "turn_trace"

    @staticmethod
    def _validate_turn(trace: TurnTrace, turn: RuntimeState) -> None:
        if trace.data_idx < 0 or trace.data_idx >= len(turn.data):
            raise ValueError("Trace data_idx is out of range.")
        if turn.id != trace.turn_id or turn.thread_id != trace.thread_id:
            raise ValueError("Trace identity does not match its Turn.")

    def initialize_turn_trace(self, session_id: str, trace: TurnTrace) -> TurnTrace:
        """Create the immutable context once and return the stored aggregate."""

        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            node = self._json_object(connection, session_id, "runtime_node", trace.turn_id)
            if node is None:
                raise ValueError(f"Unknown Turn: {trace.turn_id}")
            turn = RuntimeState.from_dict(node)
            self._validate_turn(trace, turn)
            payload = self._json_object(connection, session_id, self._TRACE_NAMESPACE, trace.object_id)
            if payload is not None:
                stored = TurnTrace.from_dict(payload)
                self._validate_turn(stored, turn)
                return stored
            self._put_json_object(
                connection,
                session_id,
                self._TRACE_NAMESPACE,
                trace.object_id,
                trace.to_dict(),
                trace.updated_at,
            )
            return trace

    def append_turn_trace_item(
        self,
        session_id: str,
        turn_id: str,
        data_idx: int,
        *,
        message_idx: int,
        item_idx: int,
        role: str,
        item: dict[str, Any],
        completed_at: str,
    ) -> TurnTrace | None:
        """Append one terminal Item, or no-op before the Trace is initialized."""

        object_id = f"{turn_id}:{data_idx}"
        with self._connection_for_existing(session_id) as connection:
            self._assert_writable(connection)
            payload = self._json_object(connection, session_id, self._TRACE_NAMESPACE, object_id)
            if payload is None:
                return None
            trace = TurnTrace.from_dict(payload)
            node = self._json_object(connection, session_id, "runtime_node", turn_id)
            if node is None:
                raise ValueError(f"Unknown Turn: {turn_id}")
            turn = RuntimeState.from_dict(node)
            self._validate_turn(trace, turn)
            if not 0 <= message_idx < len(turn.data[data_idx]):
                raise ValueError("Trace message_idx is out of range.")
            message = turn.data[data_idx][message_idx]
            content = message.get("content", [])
            if not 0 <= item_idx < len(content):
                raise ValueError("Trace item_idx is out of range.")
            if message.get("role") != role:
                raise ValueError("Trace Item role does not match its Turn.")
            if content[item_idx].get("status") not in {"success", "failed"}:
                raise ValueError("Only terminal Turn Items can be traced.")
            if item.get("status") not in {"success", "failed"}:
                raise ValueError("Only terminal audit Items can be persisted.")

            coordinate = (message_idx, item_idx)
            existing = next((entry for entry in trace.items if entry.coordinate == coordinate), None)
            if existing is not None:
                if existing.role != role or existing.item != item:
                    raise ValueError("A traced Item coordinate cannot change content.")
                return trace

            sequence = trace.last_sequence + 1
            entry = TurnTraceItem(
                sequence=sequence,
                message_idx=message_idx,
                item_idx=item_idx,
                role=role,
                item=item,
                completed_at=completed_at,
            )
            updated = replace(
                trace,
                items=[*trace.items, entry],
                last_sequence=sequence,
                updated_at=completed_at,
            )
            self._put_json_object(
                connection,
                session_id,
                self._TRACE_NAMESPACE,
                object_id,
                updated.to_dict(),
                completed_at,
            )
            return updated

    def load_turn_trace(
        self,
        session_id: str,
        turn_id: str,
        data_idx: int,
        *,
        after_sequence: int | None = None,
    ) -> TurnTrace | None:
        if data_idx < 0:
            raise ValueError("Trace data_idx must be non-negative.")
        if after_sequence is not None and after_sequence < 0:
            raise ValueError("after_sequence must be non-negative.")
        object_id = f"{turn_id}:{data_idx}"
        with self._connection_for_existing(session_id) as connection:
            payload = self._json_object(connection, session_id, self._TRACE_NAMESPACE, object_id)
        if payload is None:
            return None
        trace = TurnTrace.from_dict(payload)
        if after_sequence is None:
            return trace
        return replace(trace, items=[item for item in trace.items if item.sequence > after_sequence])


__all__ = ["SQLiteTurnTraceMixin"]
