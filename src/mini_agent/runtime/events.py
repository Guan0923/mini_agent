"""Structured runtime events consumed by presentation adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from mini_agent.domain.state import utc_now

RuntimeEventKind = Literal[
    "run_started",
    "thinking_start",
    "thinking_delta",
    "thinking_end",
    "model_request",
    "model_response",
    "model_error",
    "model_retry",
    "hook_started",
    "hook_completed",
    "hook_failed",
    "strategy",
    "context_cleaned",
    "context_compressed",
    "model_repair",
    "tool_call",
    "tool_result",
    "tool_failed",
    "retry",
    "tool_recovery",
    "replan_requested",
    "replan_applied",
    "response",
    "plan",
    "error",
    "run_finished",
    "approval_requested",
    "approval_granted",
    "user_input_requested",
    "user_input_received",
    "feedback_received",
    "steering_received",
    "steering_applied",
    "handoff_created",
    "cancelled",
]

# Reasoning stream chunks are high-volume presentation data, not durable state
# transitions. Checkpoint only the events from which a run can be inspected or
# resumed safely.
CHECKPOINT_EVENT_KINDS: frozenset[RuntimeEventKind] = frozenset(
    {
        "run_started",
        "model_request",
        "model_response",
        "model_error",
        "model_retry",
        "hook_failed",
        "strategy",
        "context_cleaned",
        "context_compressed",
        "model_repair",
        "approval_requested",
        "approval_granted",
        "user_input_requested",
        "user_input_received",
        "feedback_received",
        "steering_received",
        "steering_applied",
        "handoff_created",
        "tool_call",
        "tool_result",
        "tool_failed",
        "tool_recovery",
        "replan_requested",
        "replan_applied",
        "response",
        "plan",
        "error",
        "cancelled",
        "run_finished",
    }
)


@dataclass(frozen=True)
class RuntimeEvent:
    """A renderable event emitted by the application runtime."""

    kind: RuntimeEventKind
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=utc_now, compare=False)
