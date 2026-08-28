"""Legacy run/checkpoint and runtime-message persistence."""

from __future__ import annotations

import json
import sqlite3

from backend.domain import RunProvenance, RunStatus, RuntimeMessage
from backend.domain.state import utc_now
from backend.runtime.core.context import RuntimeState

from ..codec import assistant_content, decode_runtime_state, normalize_session_title


class SQLiteCheckpointMixin:
    def start_turn(
        self,
        session_id: str,
        run_id: str,
        task: str,
        provenance: RunProvenance | None = None,
        *,
        append_user_message: bool = True,
        delivery_id: str | None = None,
    ) -> None:
        timestamp = utc_now()
        origin = provenance or RunProvenance(workflow_id=run_id, trigger="legacy")
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            document = self._session_document(connection, session_id)
            if not bool(document.get("title_is_custom")) and not self._session_has_turn(connection):
                document["title"] = normalize_session_title(task)
            run = {
                "run_id": run_id,
                "task": task,
                "status": "running",
                "workflow_id": origin.workflow_id,
                "attempt": origin.attempt,
                "origin_kind": origin.trigger,
                "source_session_id": origin.source_session_id,
                "source_run_id": origin.source_run_id,
                "started_at": timestamp,
                "updated_at": timestamp,
            }
            self._put_json_object(connection, session_id, "run", run_id, run, timestamp)
            if append_user_message:
                self._append_turn_message(
                    connection, session_id, run_id, "user", task, timestamp, delivery_id=delivery_id
                )
            self._write_session_document(connection, session_id, document)

    @staticmethod
    def _session_has_turn(connection: sqlite3.Connection) -> bool:
        """Return whether the Session already contains a real Turn."""

        return (
            connection.execute(
                "SELECT 1 FROM json_objects "
                "WHERE (namespace='runtime_node' "
                "AND json_extract(payload_json,'$.data[0][0].role')='user') "
                "OR (namespace='turn_message' AND json_extract(payload_json,'$.role')='user') "
                "LIMIT 1"
            ).fetchone()
            is not None
        )

    def append_turn_input(self, session_id: str, run_id: str, content: str, *, delivery_id: str | None = None) -> None:
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            if self._json_object(connection, session_id, "run", run_id) is None:
                raise ValueError(f"Unknown session run: {run_id}")
            timestamp = utc_now()
            self._append_turn_message(
                connection, session_id, run_id, "user", content, timestamp, delivery_id=delivery_id
            )
            self._touch_session(connection, session_id, timestamp)

    def finish_turn(self, session_id: str, run_id: str, status: RunStatus, answer: str | None) -> None:
        timestamp = utc_now()
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            run = self._json_object(connection, session_id, "run", run_id)
            if run is None:
                raise ValueError(f"Unknown session run: {run_id}")
            content = assistant_content(status, answer)
            run.update({"status": str(status), "updated_at": timestamp})
            self._put_json_object(connection, session_id, "run", run_id, run, timestamp)
            self._append_turn_message(
                connection, session_id, run_id, "assistant", content, timestamp, data={"status": str(status)}
            )
            self._touch_session(connection, session_id, timestamp)

    def save(self, runtime, reason: str) -> None:
        self._save_state(runtime.state, reason)

    def save_runtime(self, state: RuntimeState) -> None:
        self._save_state(state, "runtime")

    def _save_state(self, state: RuntimeState, reason: str) -> None:
        timestamp = utc_now()
        full_payload = state.to_dict(include_runtime_messages=False)
        reduced_payload = state.to_dict(include_runtime_messages=False)
        # Messages and runtime messages have independent local objects, so a
        # checkpoint does not duplicate the growing transcript.
        reduced_payload.pop("messages", None)
        reduced_payload.pop("run_history", None)
        if reduced_payload.get("current_run"):
            for key in ("history", "actions", "events", "runtime_messages", "subagent_batches"):
                reduced_payload["current_run"].pop(key, None)
        with self._connection(state.session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, state.session_id)
            self._put_json_object(
                connection, state.session_id, "runtime_state", state.session_id, full_payload, timestamp
            )
            run = state.current_run
            if run is not None:
                run_payload = run.to_dict(include_runtime_messages=False)
                run_payload.update(
                    {"run_id": run.run_id, "task": run.task, "status": run.status, "updated_at": timestamp}
                )
                self._put_json_object(connection, state.session_id, "run", run.run_id, run_payload, timestamp)
                for message in run.runtime_messages:
                    self._put_runtime_message(connection, state.session_id, run.run_id, message)
            self._touch_session(connection, state.session_id, timestamp)
            if run is not None:
                checkpoint = {"run_id": run.run_id, "reason": reason, "state": reduced_payload, "created_at": timestamp}
                self._put_json_object(
                    connection,
                    state.session_id,
                    "checkpoint",
                    f"{run.run_id}:{timestamp}:{reason}",
                    checkpoint,
                    timestamp,
                )

    def load_runtime(self, session_id: str) -> RuntimeState | None:
        with self._connection(session_id) as connection:
            payload = self._json_object(connection, session_id, "runtime_state", session_id)
        if payload is None:
            return None
        state = decode_runtime_state(json.dumps(payload, ensure_ascii=False))
        if state.current_run is not None:
            state.current_run.runtime_messages = self.load_runtime_messages(session_id, state.current_run.run_id)
        return state

    def append_runtime_message(self, session_id: str, run_id: str, message: RuntimeMessage) -> None:
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            if self._json_object(connection, session_id, "run", run_id) is None:
                raise ValueError(f"Unknown session run: {run_id}")
            self._put_runtime_message(connection, session_id, run_id, message)
            self._touch_session(connection, session_id, message.timestamp)

    def load_runtime_messages(self, session_id: str, run_id: str | None = None) -> list[RuntimeMessage]:
        with self._connection(session_id) as connection:
            values = [
                value
                for value in self._json_values(connection, session_id, "runtime_message")
                if run_id is None or str(value.get("run_id") or "") == run_id
            ]
        values.sort(
            key=lambda item: (
                str(item.get("run_id") or ""),
                int(item.get("sequence", 0)),
                str(item.get("created_at") or ""),
            )
        )
        return [
            RuntimeMessage(
                int(item.get("sequence", 0)),
                str(item.get("kind") or ""),
                str(item.get("message") or ""),
                str(item.get("created_at") or utc_now()),
                dict(item.get("data") or {}),
            )
            for item in values
        ]

    def resume_runtime(self, source: RuntimeState, resumed: RuntimeState) -> None:
        self._save_state(source, f"run_{source.current_run.status}" if source.current_run else "resume")
        self._save_state(resumed, "run_resumed")

    def _append_turn_message(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        run_id: str,
        role: str,
        content: str,
        timestamp: str,
        *,
        data: dict[str, object] | None = None,
        delivery_id: str | None = None,
    ) -> None:
        existing = self._json_values(connection, session_id, "turn_message")
        if delivery_id:
            delivered = next((item for item in existing if item.get("delivery_id") == delivery_id), None)
            if delivered is not None:
                if (
                    delivered.get("run_id") == run_id
                    and delivered.get("role") == role
                    and delivered.get("content") == content
                ):
                    return
                raise ValueError("delivery_id already belongs to a different Turn message.")
        run_messages = [item for item in existing if str(item.get("run_id") or "") == run_id]
        prior_assistant = next(
            (item for item in run_messages if role == "assistant" and item.get("role") == "assistant"),
            None,
        )
        sequence = (
            int(prior_assistant.get("sequence", 0))
            if prior_assistant is not None
            else 1 + max((int(item.get("sequence", 0)) for item in run_messages), default=0)
        )
        payload = {
            "run_id": run_id,
            "sequence": sequence,
            "role": role,
            "content": content,
            "data": data or {},
            "created_at": timestamp,
            **({"delivery_id": delivery_id} if delivery_id else {}),
        }
        self._put_json_object(connection, session_id, "turn_message", f"{run_id}:{sequence}", payload, timestamp)

    def has_turn_delivery(self, session_id: str, delivery_id: str) -> bool:
        with self._connection(session_id) as connection:
            return any(
                item.get("delivery_id") == delivery_id
                for item in self._json_values(connection, session_id, "turn_message")
            )

    def running_run_id(self, session_id: str) -> str | None:
        """Return the sole running legacy Run when crash repair is unambiguous."""

        with self._connection(session_id) as connection:
            matches = [
                str(item.get("run_id") or "")
                for item in self._json_values(connection, session_id, "run")
                if item.get("status") == "running" and item.get("run_id")
            ]
        return matches[0] if len(matches) == 1 else None

    def _put_runtime_message(
        self, connection: sqlite3.Connection, session_id: str, run_id: str, message: RuntimeMessage
    ) -> None:
        payload = {
            "run_id": run_id,
            "sequence": message.sequence,
            "kind": message.kind,
            "message": message.message,
            "data": message.data,
            "created_at": message.timestamp,
        }
        object_id = f"{run_id}:{message.sequence}"
        existing = self._json_object(connection, session_id, "runtime_message", object_id)
        if existing is not None:
            if existing == payload:
                return
            raise ValueError("Runtime messages are immutable and cannot be replaced.")
        self._put_json_object(connection, session_id, "runtime_message", object_id, payload, message.timestamp)
