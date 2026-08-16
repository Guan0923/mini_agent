"""Enrich runtime events and checkpoint stable state transitions."""

from __future__ import annotations

from backend.domain import RecoveryCheckpoint

from ...core.context import AgentRuntime
from ...core.events import CHECKPOINT_EVENT_KINDS, RuntimeEvent
from ...persistence.recording import persistent_event

_HIDDEN_RECOVERABLE_EVENTS = frozenset({"tool_failed", "tool_recovery", "model_repair", "model_retry"})


class RunEventPublisher:
    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime
        self._thinking_started_at: str | None = None
        self._thinking_data: dict[str, object] = {}
        self._thinking_chunks: list[str] = []

    def __call__(self, event: RuntimeEvent) -> None:
        runtime = self._runtime
        run = runtime.run
        context = {
            "session_id": runtime.state.session_id,
            "run_id": run.run_id,
            "workflow_id": run.provenance.workflow_id,
            "workflow_attempt": run.provenance.attempt,
            "workflow_trigger": run.provenance.trigger,
            "workspace_root": run.provenance.workspace_root or runtime.state.workspace_root,
            "source_session_id": run.provenance.source_session_id,
            "source_run_id": run.provenance.source_run_id,
            "task": run.task,
            "mode": run.mode,
            "status": run.status,
        }
        enriched = RuntimeEvent(
            event.kind,
            event.message,
            {**event.data, **context},
            timestamp=event.timestamp,
        )
        self._record(enriched)
        checkpoint = runtime.services.checkpoint_store
        if checkpoint is not None and event.kind in CHECKPOINT_EVENT_KINDS:
            run.checkpoint = RecoveryCheckpoint(
                reason=event.kind,
                timestamp=event.timestamp,
                call_id=str(event.data["call_id"]) if event.data.get("call_id") else None,
                exchange_id=str(event.data["exchange_id"]) if event.data.get("exchange_id") else None,
                interruption=(
                    str(event.data["stop_reason"])
                    if event.kind == "cancelled" and event.data.get("stop_reason")
                    else None
                ),
            )
            checkpoint.save(runtime, event.kind)
            if checkpoint is not runtime.services.runtime_store:
                runtime.save()
        if runtime.services.on_event is not None and event.kind not in _HIDDEN_RECOVERABLE_EVENTS:
            runtime.services.on_event(enriched)

    def _record(self, event: RuntimeEvent) -> None:
        if event.kind in {"response_start", "response_delta", "response_end"}:
            return
        if event.kind == "assistant_message":
            self._record_non_stream_reasoning(event)
            return
        if event.kind == "thinking_start":
            self._thinking_started_at = event.timestamp
            self._thinking_data = dict(event.data)
            self._thinking_chunks = []
            return
        if event.kind == "thinking_delta":
            if self._thinking_started_at is None:
                self._thinking_started_at = event.timestamp
            self._thinking_chunks.append(event.message)
            return
        if event.kind == "thinking_end":
            self._flush_thinking(completed=True, closing_data=event.data)
            return
        self._flush_thinking(completed=False)
        message, data = persistent_event(event, self._runtime.state.runner_settings.log_full_messages)
        self._append(event.kind, message, event.timestamp, data)

    def _record_non_stream_reasoning(self, event: RuntimeEvent) -> None:
        """Keep reasoning audit records without replaying non-stream UI events."""

        if event.data.get("reasoning_streamed"):
            return
        message = event.data.get("message")
        if not isinstance(message, dict):
            return
        reasoning = message.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning:
            return
        data = {key: event.data[key] for key in ("run_id", "task", "mode", "status") if key in event.data}
        data["streamed"] = False
        text, data = persistent_event(
            RuntimeEvent("thinking_delta", reasoning, data),
            self._runtime.state.runner_settings.log_full_messages,
        )
        self._append("thinking", text, event.timestamp, data)

    def _flush_thinking(self, *, completed: bool, closing_data: dict[str, object] | None = None) -> None:
        if self._thinking_started_at is None:
            return
        source_data = dict(closing_data or {}) if completed else self._thinking_data
        data = {"streamed": True, **source_data}
        if not completed:
            data["interrupted"] = True
        message, persistent_data = persistent_event(
            RuntimeEvent("thinking_delta", "".join(self._thinking_chunks), data),
            self._runtime.state.runner_settings.log_full_messages,
        )
        self._append("thinking", message, self._thinking_started_at, persistent_data)
        self._thinking_started_at = None
        self._thinking_data = {}
        self._thinking_chunks = []

    def _append(self, kind: str, message: str, timestamp: str, data: dict[str, object]) -> None:
        runtime = self._runtime
        durable = runtime.run.add_runtime_message(kind, message, timestamp=timestamp, data=data)
        store = runtime.services.runtime_store
        if store is runtime.services.checkpoint_store and kind in CHECKPOINT_EVENT_KINDS:
            # The shared PostgreSQL store writes this message with the checkpoint,
            # session runtime, and run status in one transaction.
            return
        append = getattr(store, "append_runtime_message", None)
        if callable(append):
            append(runtime.state.session_id, runtime.run.run_id, durable)
