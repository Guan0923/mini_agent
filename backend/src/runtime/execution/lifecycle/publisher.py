"""Enrich transient runtime events and checkpoint stable state transitions."""

from __future__ import annotations

from backend.domain import RecoveryCheckpoint

from ...core.context import AgentRuntime
from ...core.events import CHECKPOINT_EVENT_KINDS, RuntimeEvent

_HIDDEN_RECOVERABLE_EVENTS = frozenset({"tool_failed", "tool_recovery", "model_repair", "model_retry"})


class RunEventPublisher:
    """Forward transient execution signals without creating a parallel audit log."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    def __call__(self, event: RuntimeEvent) -> None:
        runtime = self._runtime
        run = runtime.run
        if event.kind == "tool_call":
            run.tool_calls += 1
        elif event.kind in {"retry", "model_retry"}:
            run.retries += 1
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
        enriched = RuntimeEvent(event.kind, event.message, {**event.data, **context}, timestamp=event.timestamp)
        internal = runtime.services.runtime_node_event
        if event.kind in _HIDDEN_RECOVERABLE_EVENTS and callable(internal):
            internal(enriched)
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
