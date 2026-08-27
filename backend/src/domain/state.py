"""State owned by an Agent run, independent of how it is displayed or executed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from .messages import ChatMessage, ToolMessage
from .skills import SkillSnapshot

EventKind = Literal[
    "run_started",
    "skills_selected",
    "context_compaction_started",
    "context_compaction_completed",
    "context_compaction_failed",
    "model",
    "model_repair",
    "model_retry",
    "reasoning",
    "plan",
    "tool_call",
    "tool_result",
    "tool_failed",
    "retry",
    "tool_recovery",
    "error",
    "final",
    "run_finished",
    "run_suspended",
    "run_resumed",
    "run_interrupted",
    "run_terminated",
    "approval_requested",
    "approval_granted",
    "user_input_requested",
    "user_input_received",
    "feedback_received",
    "steering_applied",
    "handoff_created",
    "cancelled",
    "tool_indeterminate",
    "subagent_queued",
    "subagent_started",
    "subagent_write_requested",
    "subagent_completed",
    "subagent_failed",
    "subagent_indeterminate",
]
RunMode = Literal["agent", "plan"]
RunStatus = Literal[
    "running",
    "completed",
    "failed",
    "cancelled",
]
RunStopReason = Literal[
    "execution_failed",
    "user_cancelled",
    "user_paused",
    "user_terminated",
    "process_interrupted",
]
RunTrigger = Literal["embedding", "handoff", "resume", "legacy"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_run_id() -> str:
    return f"run_{uuid4().hex}"


def new_workflow_id() -> str:
    return f"workflow_{uuid4().hex}"


@dataclass(frozen=True)
class RunProvenance:
    """Immutable identity and ancestry for one workflow attempt."""

    workflow_id: str = field(default_factory=new_workflow_id)
    attempt: int = 1
    trigger: RunTrigger = "embedding"
    workspace_root: str | None = None
    source_session_id: str | None = None
    source_run_id: str | None = None


@dataclass(frozen=True)
class RecoveryCheckpoint:
    """The last durable boundary from which an interrupted run is inspected."""

    reason: str
    timestamp: str
    call_id: str | None = None
    exchange_id: str | None = None
    interruption: str | None = None
    indeterminate_call_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunHandoff:
    """A follow-up run requested by the completed current run."""

    mode: RunMode
    task: str
    compact_before: bool = False
    active_skills: tuple[SkillSnapshot, ...] = ()


@dataclass
class TraceEvent:
    kind: EventKind
    message: str
    timestamp: str = field(default_factory=utc_now)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeMessage:
    """One normalized runtime event retained for audit and replay."""

    sequence: int
    kind: str
    message: str
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunState:
    task: str
    mode: RunMode
    run_id: str = field(default_factory=new_run_id)
    turn_start_index: int = 0
    history: list[ChatMessage] = field(default_factory=list)
    actions: list[ToolMessage] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)
    runtime_messages: list[RuntimeMessage] = field(default_factory=list)
    completed_steps: list[int] = field(default_factory=list)
    final_answer: str | None = None
    model_turns: int = 0
    skill_selection_calls: int = 0
    status: RunStatus = "running"
    stop_reason: RunStopReason | None = None
    handoff: RunHandoff | None = None
    active_skills: list[SkillSnapshot] = field(default_factory=list)
    provenance: RunProvenance = field(default_factory=RunProvenance)
    checkpoint: RecoveryCheckpoint | None = None
    subagent_batches: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_event(self, kind: EventKind, message: str, **data: Any) -> None:
        self.events.append(TraceEvent(kind=kind, message=message, data=data))

    def add_runtime_message(
        self,
        kind: str,
        message: str,
        *,
        timestamp: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> RuntimeMessage:
        """Append a presentation-independent event in its durable order."""

        runtime_message = RuntimeMessage(
            sequence=len(self.runtime_messages) + 1,
            kind=kind,
            message=message,
            timestamp=timestamp or utc_now(),
            data=dict(data or {}),
        )
        self.runtime_messages.append(runtime_message)
        return runtime_message

    def to_dict(self, *, include_runtime_messages: bool = True) -> dict[str, Any]:
        from .state_codec import run_state_to_dict

        return run_state_to_dict(self, include_runtime_messages=include_runtime_messages)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        """Rebuild persisted state without coupling the domain to a storage backend."""

        from .state_codec import run_state_from_dict

        return run_state_from_dict(data)


# New message-tree state is intentionally kept in its own dependency-free
# module.  Re-export it here for callers that historically imported all state
# values from ``backend.domain.state``.
from .runtime_state import RuntimeState  # noqa: E402  (compatibility export)

RuntimeStateNode = RuntimeState
