"""Terminal run-state archival and completion event publication."""

from __future__ import annotations

from time import perf_counter

from backend.domain import RunState

from ...core.context import AgentRuntime, RunSummary
from ...core.events import RuntimeEvent


def finish_run(runtime: AgentRuntime, *, started_at: float) -> RunState:
    """Finalize usage, history, telemetry, and persistence for one attempt."""

    run = runtime.run
    if runtime.services.todo_store is not None and run.turn_id and run.status in {"completed", "failed"}:
        runtime.services.todo_store.expire_turn(runtime.state.session_id, run.turn_id)
    runtime.state.usage = runtime.state.turn_usage
    runtime.state.turn_usage = None
    runtime.state.status = "idle"
    if not any(summary.run_id == run.run_id for summary in runtime.state.run_history):
        runtime.state.run_history.append(
            RunSummary(
                run.run_id,
                run.task,
                run.status,
                run.mode,
                run.final_answer,
                run.provenance.workflow_id,
                run.provenance.attempt,
            )
        )
    if runtime.services.publish is not None:
        runtime.services.publish(
            RuntimeEvent(
                "run_finished",
                run.status,
                {
                    "schema_version": 2,
                    "final_answer": run.final_answer or "",
                    "duration_ms": round((perf_counter() - started_at) * 1000, 3),
                    "usage": runtime.state.usage,
                    "model_calls": run.model_calls,
                    "tool_calls": run.tool_calls,
                    "retries": run.retries,
                    "active_skills": [{"name": skill.name, "sha256": skill.sha256} for skill in run.active_skills],
                },
            )
        )
    runtime.save()
    return run
