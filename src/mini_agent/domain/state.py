"""State owned by an Agent run, independent of how it is displayed or executed."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from .messages import (
    ChatMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dicts,
    tool_message_from_dict,
    tool_message_to_dict,
)
from .plans import ExecutionPlan, ExecutionStrategy, PlanStep
from .skills import SkillSnapshot

EventKind = Literal[
    "run_started",
    "skills_selected",
    "strategy",
    "context_cleaned",
    "context_compressed",
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
    "replan_requested",
    "replan_applied",
    "error",
    "final",
    "run_finished",
    "approval_requested",
    "approval_granted",
    "user_input_requested",
    "user_input_received",
    "feedback_received",
    "steering_applied",
    "handoff_created",
    "cancelled",
    "plan_progress",
]
RunMode = Literal["agent", "plan"]
RunStatus = Literal["running", "completed", "failed", "cancelled"]
StrategyPolicy = Literal["auto", "reactive", "dynamic_replan"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_run_id() -> str:
    return f"run_{uuid4().hex}"


@dataclass(frozen=True)
class RunHandoff:
    """A follow-up run requested by the completed current run."""

    mode: RunMode
    task: str
    new_session: bool = False
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
    strategy: ExecutionStrategy = "reactive"
    strategy_reason: str | None = None
    turn_start_index: int = 0
    history: list[ChatMessage] = field(default_factory=list)
    actions: list[ToolMessage] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)
    runtime_messages: list[RuntimeMessage] = field(default_factory=list)
    completed_steps: list[int] = field(default_factory=list)
    plan: ExecutionPlan | None = None
    plan_history: list[ExecutionPlan] = field(default_factory=list)
    replan_count: int = 0
    final_answer: str | None = None
    model_turns: int = 0
    status: RunStatus = "running"
    handoff: RunHandoff | None = None
    active_skills: list[SkillSnapshot] = field(default_factory=list)

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "mode": self.mode,
            "run_id": self.run_id,
            "strategy": self.strategy,
            "strategy_reason": self.strategy_reason,
            "turn_start_index": self.turn_start_index,
            "history": [message_to_dict(message) for message in self.history],
            "actions": [tool_message_to_dict(action) for action in self.actions],
            "events": [asdict(event) for event in self.events],
            "runtime_messages": [asdict(message) for message in self.runtime_messages],
            "completed_steps": self.completed_steps,
            "plan": self._plan_to_dict(self.plan) if self.plan else None,
            "plan_history": [self._plan_to_dict(plan) for plan in self.plan_history],
            "replan_count": self.replan_count,
            "final_answer": self.final_answer,
            "model_turns": self.model_turns,
            "status": self.status,
            "handoff": asdict(self.handoff) if self.handoff else None,
            "active_skills": [skill.to_dict() for skill in self.active_skills],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        """Rebuild persisted state without coupling the domain to a storage backend."""

        def tool(value: dict[str, Any], fallback_call_id: str) -> ToolMessage:
            if "type" in value:
                content = value.get("answer") if value.get("type") == "final_answer" else None
                return ToolMessage(
                    name=str(value.get("tool") or "legacy"),
                    call_id=fallback_call_id,
                    arguments=dict(value.get("arguments") or {}),
                    content=content if isinstance(content, str) else None,
                    status="succeeded" if isinstance(content, str) else "pending",
                )
            return tool_message_from_dict(value, fallback_call_id=fallback_call_id)

        def plan(value: dict[str, Any]) -> ExecutionPlan:
            return ExecutionPlan(
                goal=value["goal"],
                steps=[
                    PlanStep(
                        id=step["id"],
                        description=step["description"],
                        tool_message=tool(
                            step.get("tool_message") or step.get("action") or {},
                            f"call_legacy_plan_{index}",
                        ),
                        success_criteria=step.get("success_criteria", ""),
                        status=step.get("status", "pending"),
                        result=step.get("result"),
                    )
                    for index, step in enumerate(value.get("steps", []), start=1)
                ],
                final_answer=value.get("final_answer"),
                revision=value.get("revision", 1),
            )

        handoff_data = data.get("handoff")
        handoff = None
        if isinstance(handoff_data, dict):
            handoff = RunHandoff(
                mode=handoff_data.get("mode", "agent"),
                task=str(handoff_data.get("task") or ""),
                new_session=(
                    handoff_data["new_session"] if isinstance(handoff_data.get("new_session"), bool) else False
                ),
                active_skills=tuple(
                    SkillSnapshot.from_dict(dict(item))
                    for item in handoff_data.get("active_skills", [])
                    if isinstance(item, dict)
                ),
            )

        return cls(
            task=data["task"],
            mode=data["mode"],
            run_id=data["run_id"],
            strategy=data.get("strategy", "reactive"),
            strategy_reason=data.get("strategy_reason"),
            turn_start_index=int(data.get("turn_start_index", 0)),
            history=messages_from_dicts([dict(item) for item in data.get("history", [])]),
            actions=[
                tool(dict(item), f"call_legacy_action_{index}")
                for index, item in enumerate(data.get("actions", []), start=1)
            ],
            events=[TraceEvent(**item) for item in data.get("events", [])],
            runtime_messages=[
                RuntimeMessage(
                    sequence=int(item.get("sequence", index)),
                    kind=str(item.get("kind") or "unknown"),
                    message=str(item.get("message") or ""),
                    timestamp=str(item.get("timestamp") or utc_now()),
                    data=dict(item.get("data") or {}),
                )
                for index, item in enumerate(data.get("runtime_messages", []), start=1)
                if isinstance(item, dict)
            ],
            completed_steps=list(data.get("completed_steps", [])),
            plan=plan(data["plan"]) if data.get("plan") else None,
            plan_history=[plan(item) for item in data.get("plan_history", [])],
            replan_count=data.get("replan_count", 0),
            final_answer=data.get("final_answer"),
            model_turns=int(data.get("model_turns", 0)),
            status=data.get("status", "running"),
            active_skills=[
                SkillSnapshot.from_dict(dict(item)) for item in data.get("active_skills", []) if isinstance(item, dict)
            ],
            handoff=handoff,
        )

    @staticmethod
    def _plan_to_dict(plan: ExecutionPlan) -> dict[str, Any]:
        return {
            "goal": plan.goal,
            "steps": [
                {
                    "id": step.id,
                    "description": step.description,
                    "tool_message": tool_message_to_dict(step.tool_message),
                    "success_criteria": step.success_criteria,
                    "status": step.status,
                    "result": step.result,
                }
                for step in plan.steps
            ],
            "final_answer": plan.final_answer,
            "revision": plan.revision,
        }
