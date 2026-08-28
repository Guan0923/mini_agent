"""Consistent state transitions for runtime outcomes."""

from __future__ import annotations

from typing import Literal

from backend.domain import (
    AssistantMessage,
    RunStopReason,
    UserMessage,
)

from ...core.context import AgentRuntime
from ...core.events import RuntimeEvent


def planning_failure_data(error: Exception, planner: str) -> dict[str, object]:
    data: dict[str, object] = {"planner": planner}
    diagnostics = getattr(error, "diagnostics", None)
    if isinstance(diagnostics, dict) and diagnostics:
        data["provider_diagnostics"] = diagnostics
    return data


def complete_run(
    runtime: AgentRuntime,
    message: AssistantMessage,
    *,
    final_answer: str | None = None,
    event_kind: Literal["response", "plan"] | None = None,
    response_streamed: bool = False,
) -> None:
    run = runtime.run
    if not any(existing is message for existing in runtime.state.messages):
        runtime.state.messages.append(message)
    run.history = runtime.state.messages
    run.status = "completed"
    run.final_answer = (message.content or "") if final_answer is None else final_answer
    publish = runtime.services.publish or (lambda _event: None)
    kind = event_kind or ("plan" if run.mode == "plan" else "response")
    publish(RuntimeEvent(kind, run.final_answer, {"streamed": response_streamed}))


def fail_run(
    runtime: AgentRuntime,
    message: str,
    *,
    stop_reason: RunStopReason = "execution_failed",
    **data: object,
) -> None:
    run = runtime.run
    boundary = min(max(run.turn_start_index, 0), len(runtime.state.messages))
    already_recorded = any(
        isinstance(existing, AssistantMessage) and not existing.tool_messages and existing.content == message
        for existing in runtime.state.messages[boundary:]
    )
    if not already_recorded:
        runtime.state.messages.append(AssistantMessage(content=message))
    run.history = runtime.state.messages
    run.status = "failed"
    run.stop_reason = stop_reason
    run.final_answer = message
    publish = runtime.services.publish or (lambda _event: None)
    publish(RuntimeEvent("error", message, dict(data)))


def cancel_run(
    runtime: AgentRuntime,
    *,
    stop_reason: RunStopReason = "user_cancelled",
    message: str = "Run cancelled by user",
) -> None:
    run = runtime.run
    detail = message if message not in {"Run cancelled by user", "Run paused by user"} else ""
    reason = detail or ("The run was paused at the user's request." if stop_reason == "user_paused" else message)
    boundary = min(max(run.turn_start_index, 0), len(runtime.state.messages))
    assistant = next(
        (item for item in reversed(runtime.state.messages[boundary:]) if isinstance(item, AssistantMessage)),
        None,
    )
    if assistant is None:
        runtime.state.messages.append(AssistantMessage(content=reason))
    elif reason not in (assistant.content or ""):
        assistant.content = f"{assistant.content}\n\n{reason}".strip()
    run.status = "cancelled"
    run.stop_reason = stop_reason
    run.final_answer = reason
    run.history = runtime.state.messages
    publish = runtime.services.publish or (lambda _event: None)
    publish(RuntimeEvent("cancelled", reason, {"stop_reason": stop_reason}))


def pause_run(runtime: AgentRuntime) -> None:
    """Stop at a cooperative boundary while preserving a resumable checkpoint."""

    cancel_run(runtime, stop_reason="user_paused", message="Run paused by user")


def record_plan_feedback(runtime: AgentRuntime, supplement: str | None) -> str | None:
    feedback = (supplement or "").strip()
    if not feedback:
        fail_run(runtime, "Supplement must contain plan feedback.")
        return None
    runtime.state.messages.append(UserMessage(content=f"[Plan feedback]\n{feedback}"))
    runtime.run.history = runtime.state.messages
    publish = runtime.services.publish or (lambda _event: None)
    publish(RuntimeEvent("feedback_received", feedback))
    runtime.save()
    return feedback
