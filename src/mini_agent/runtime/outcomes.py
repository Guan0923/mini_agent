"""State transitions for runtime run outcomes."""

from __future__ import annotations

from mini_agent.domain import RunState

from .contracts import EventHandler
from .events import RuntimeEvent


def complete_run(
    state: RunState,
    answer: str,
    conversation: list[dict[str, str]] | None,
    publish: EventHandler,
) -> RunState:
    """Mark a run complete and persist agent-mode conversation context."""
    state.status = "completed"
    state.final_answer = answer
    state.add_event("final", "Task completed")
    if state.mode == "agent" and conversation is not None:
        conversation.extend(
            [
                {"role": "user", "content": state.task},
                {"role": "assistant", "content": answer},
            ]
        )
    publish(RuntimeEvent("plan" if state.mode == "plan" else "response", answer))
    return state


def fail_run(state: RunState, publish: EventHandler, message: str, **data: object) -> RunState:
    """Mark a run failed through one consistent trace and presentation path."""
    state.status = "failed"
    state.final_answer = message
    state.add_event("error", message, **data)
    publish(RuntimeEvent("error", message, dict(data)))
    return state


def cancel_run(state: RunState, publish: EventHandler) -> RunState:
    """Mark a run as user-cancelled through the same event path as other outcomes."""
    state.status = "cancelled"
    state.add_event("cancelled", "Run cancelled by user")
    publish(RuntimeEvent("cancelled", "cancelled"))
    return state


def record_plan_feedback(
    state: RunState,
    history: list[dict[str, str]],
    supplement: str | None,
    publish: EventHandler,
) -> str | None:
    """Add valid human plan feedback to model context and the durable trace."""
    feedback = (supplement or "").strip()
    if not feedback:
        fail_run(state, publish, "Supplement must contain plan feedback.")
        return None
    history.append({"role": "user", "content": f"[Plan feedback]\n{feedback}"})
    state.add_event("feedback_received", "Human plan feedback received", supplement=feedback)
    publish(RuntimeEvent("feedback_received", feedback))
    return feedback
