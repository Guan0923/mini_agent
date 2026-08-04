"""Structured runtime events consumed by presentation adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from backend.domain.state import utc_now

RuntimeEventKind = Literal[
    "run_started",
    "run_suspended",
    "run_resumed",
    "run_interrupted",
    "run_terminated",
    "skills_selected",
    "thinking_start",
    "thinking_delta",
    "thinking_end",
    "response_start",
    "response_delta",
    "response_end",
    "assistant_message",
    "model_request",
    "model_response",
    "model_error",
    "model_retry",
    "hook_started",
    "hook_completed",
    "hook_failed",
    "strategy",
    "context_compaction_started",
    "context_compaction_completed",
    "context_compaction_failed",
    "context_usage",
    "model_repair",
    "tool_call",
    "tool_result",
    "tool_failed",
    "tool_indeterminate",
    "retry",
    "tool_recovery",
    "replan_requested",
    "replan_applied",
    "plan_progress",
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
    "subagent_queued",
    "subagent_started",
    "subagent_write_requested",
    "subagent_completed",
    "subagent_failed",
    "subagent_indeterminate",
]

# Text stream chunks are high-volume presentation data, not durable state
# transitions. Checkpoint only events from which a run can be inspected or
# resumed safely.
CHECKPOINT_EVENT_KINDS: frozenset[RuntimeEventKind] = frozenset(
    {
        "run_started",
        "run_suspended",
        "run_resumed",
        "run_interrupted",
        "run_terminated",
        "skills_selected",
        "strategy",
        "context_compaction_started",
        "context_compaction_completed",
        "context_compaction_failed",
        "approval_requested",
        "approval_granted",
        "user_input_requested",
        "user_input_received",
        "feedback_received",
        "steering_applied",
        "handoff_created",
        "tool_call",
        "tool_result",
        "tool_failed",
        "tool_indeterminate",
        "replan_applied",
        "plan_progress",
        "response",
        "plan",
        "error",
        "cancelled",
        "subagent_queued",
        "subagent_started",
        "subagent_write_requested",
        "subagent_completed",
        "subagent_failed",
        "subagent_indeterminate",
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
