"""Model and tool budget enforcement."""

from __future__ import annotations

from mini_agent.domain import AssistantMessage, ExecutionPlan, PlanningError
from mini_agent.planning import PlannerCapabilities

from ...core.context import AgentRuntime
from ...core.events import RuntimeEvent
from ..lifecycle.outcomes import fail_run
from .common import (
    _fail_pending_tools,
    _finish_assistant,
    _publish,
    _publish_repairs,
    _start_assistant,
    _truncate,
)


def _budget_fallback(runtime: AgentRuntime, reason: str) -> str:
    recent = ", ".join(f"{tool.name} ({tool.status})" for tool in runtime.run.actions[-3:])
    details = f" Recent tool calls: {recent}." if recent else ""
    return (
        f"Execution budget exhausted: {reason} "
        f"The run completed {len(runtime.run.actions)} tool calls before stopping.{details} "
        "Continue the task in a new turn if more work is required."
    )


def _fail_for_budget(runtime: AgentRuntime, limit_type: str, reason: str) -> None:
    settings = runtime.state.runner_settings
    limit = settings.max_model_turns if limit_type == "model_turns" else settings.max_tool_calls
    capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
    answer = ""
    source = "fallback"
    finalization_error: str | None = None
    if capabilities.run_finalizer is not None:
        try:
            message = capabilities.run_finalizer.finalize(runtime, reason)
            if message.tool_messages or not (message.content and message.content.strip()):
                raise PlanningError("Budget finalizer returned invalid output.")
            runtime.state.messages.append(message)
            runtime.run.history = runtime.state.messages
            runtime.save()
            answer = message.content.strip()
            source = "planner"
        except Exception as exc:
            finalization_error = _truncate(str(exc) or exc.__class__.__name__)
            _publish_repairs(runtime, capabilities)
    if not answer:
        answer = _budget_fallback(runtime, reason)
    data: dict[str, object] = {
        "limit_type": limit_type,
        "limit": limit,
        "model_turns": runtime.run.model_turns,
        "tool_calls": len(runtime.run.actions),
        "finalizer": source,
    }
    if finalization_error is not None:
        data["finalization_error"] = finalization_error
    fail_run(runtime, answer, **data)


def _claim_model_turn(runtime: AgentRuntime, operation: str) -> bool:
    limit = runtime.state.runner_settings.max_model_turns
    if runtime.run.model_turns >= limit:
        _fail_for_budget(
            runtime,
            "model_turns",
            f"the maximum of {limit} model turns was reached before a final answer.",
        )
        return False
    runtime.run.model_turns += 1
    runtime.run.add_event(
        "model",
        "Logical model turn started",
        operation=operation,
        model_turn=runtime.run.model_turns,
        limit=limit,
    )
    runtime.save()
    return True


def _tool_batch_fits(runtime: AgentRuntime, response: AssistantMessage) -> bool:
    return len(runtime.run.actions) + len(response.tool_messages) <= runtime.state.runner_settings.max_tool_calls


def _reject_over_budget_tools(runtime: AgentRuntime, response: AssistantMessage) -> None:
    limit = runtime.state.runner_settings.max_tool_calls
    remaining = max(0, limit - len(runtime.run.actions))
    reason = (
        f"the model requested {len(response.tool_messages)} tool calls, but only "
        f"{remaining} of {limit} tool calls remained."
    )
    _start_assistant(runtime, response)
    _fail_pending_tools(runtime, response, f"Not executed because {reason}")
    _finish_assistant(runtime)
    _fail_for_budget(runtime, "tool_calls", reason)


def _ensure_tool_budget(runtime: AgentRuntime) -> bool:
    limit = runtime.state.runner_settings.max_tool_calls
    if len(runtime.run.actions) < limit:
        return True
    _fail_for_budget(
        runtime,
        "tool_calls",
        f"the maximum of {limit} tool calls was reached before a final answer.",
    )
    return False


def _plan_step_snapshots(plan: ExecutionPlan) -> list[dict[str, object]]:
    """Return presentation-independent state for every plan step."""

    return [
        {
            "index": index,
            "id": step.id,
            "description": step.description,
            "status": step.status,
            "result": step.result,
        }
        for index, step in enumerate(plan.steps, start=1)
    ]


def _publish_plan_progress(
    runtime: AgentRuntime,
    plan: ExecutionPlan,
    *,
    trigger: str,
    changed_step_id: str | None = None,
) -> None:
    data = {
        "revision": plan.revision,
        "trigger": trigger,
        "steps": _plan_step_snapshots(plan),
        "changed_step_id": changed_step_id,
    }
    runtime.run.add_event("plan_progress", "Plan step status updated", **data)
    _publish(runtime, RuntimeEvent("plan_progress", "Plan step status updated", data))
    runtime.save()
