"""Structured runtime events consumed by presentation adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RuntimeEventKind = Literal[
    "run_started",
    "thinking_start",
    "thinking_delta",
    "thinking_end",
    "strategy",
    "tool_call",
    "tool_result",
    "tool_failed",
    "retry",
    "replan_requested",
    "replan_applied",
    "response",
    "plan",
    "error",
    "run_finished",
    "approval_requested",
    "approval_granted",
    "feedback_received",
    "cancelled",
]

# Reasoning stream chunks are high-volume presentation data, not durable state
# transitions. Checkpoint only the events from which a run can be inspected or
# resumed safely.
CHECKPOINT_EVENT_KINDS: frozenset[RuntimeEventKind] = frozenset(
    {
        "run_started",
        "strategy",
        "approval_requested",
        "approval_granted",
        "feedback_received",
        "tool_call",
        "tool_result",
        "tool_failed",
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
