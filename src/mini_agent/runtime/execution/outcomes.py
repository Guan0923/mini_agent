"""Consistent state transitions for runtime outcomes."""

from __future__ import annotations

from typing import Literal

from mini_agent.domain import AssistantMessage, UserMessage

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent


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
    run.add_event("final", "Task completed")
    publish = runtime.services.publish or (lambda _event: None)
    kind = event_kind or ("plan" if run.mode == "plan" else "response")
    publish(RuntimeEvent(kind, run.final_answer, {"streamed": response_streamed}))


def fail_run(runtime: AgentRuntime, message: str, **data: object) -> None:
    run = runtime.run
    run.status = "failed"
    run.final_answer = message
    run.add_event("error", message, **data)
    publish = runtime.services.publish or (lambda _event: None)
    publish(RuntimeEvent("error", message, dict(data)))


def cancel_run(runtime: AgentRuntime) -> None:
    run = runtime.run
    run.status = "cancelled"
    run.add_event("cancelled", "Run cancelled by user")
    publish = runtime.services.publish or (lambda _event: None)
    publish(RuntimeEvent("cancelled", "cancelled"))


def record_plan_feedback(runtime: AgentRuntime, supplement: str | None) -> str | None:
    feedback = (supplement or "").strip()
    if not feedback:
        fail_run(runtime, "Supplement must contain plan feedback.")
        return None
    runtime.state.messages.append(UserMessage(content=f"[Plan feedback]\n{feedback}"))
    runtime.run.history = runtime.state.messages
    runtime.run.add_event("feedback_received", "Human plan feedback received", supplement=feedback)
    publish = runtime.services.publish or (lambda _event: None)
    publish(RuntimeEvent("feedback_received", feedback))
    runtime.save()
    return feedback
