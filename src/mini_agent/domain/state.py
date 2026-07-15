"""State owned by an Agent run, independent of how it is displayed or executed."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

EventKind = Literal[
    "run_started",
    "strategy",
    "model",
    "reasoning",
    "plan",
    "tool_call",
    "tool_result",
    "tool_failed",
    "retry",
    "replan_requested",
    "replan_applied",
    "error",
    "final",
    "run_finished",
    "approval_requested",
    "approval_granted",
    "feedback_received",
    "cancelled",
]
ActionType = Literal["tool_call", "final_answer"]
RunMode = Literal["agent", "plan"]
RunStatus = Literal["running", "completed", "failed", "cancelled"]
ExecutionStrategy = Literal["reactive", "plan_execute", "dynamic_replan"]
StrategyPolicy = Literal["auto", "reactive", "plan_execute", "dynamic_replan"]
PlanStepStatus = Literal["pending", "running", "completed", "failed", "superseded"]
ReplanDecision = Literal["continue", "replan"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_run_id() -> str:
    return f"run_{uuid4().hex}"


@dataclass(frozen=True)
class AgentAction:
    """The model's next atomic decision in either runtime mode."""

    type: ActionType
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    reasoning: str | None = None


@dataclass
class PlanStep:
    """One validated, executable tool operation in a precomputed plan."""

    id: str
    description: str
    action: AgentAction
    success_criteria: str = ""
    status: PlanStepStatus = "pending"
    result: str | None = None


@dataclass
class ExecutionPlan:
    """A fixed plan generated before the runner starts executing tools."""

    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    final_answer: str | None = None
    revision: int = 1


@dataclass(frozen=True)
class StepEvaluation:
    """Whether a successful step still leaves the active plan valid."""

    decision: ReplanDecision
    reason: str


@dataclass(frozen=True)
class StrategySelection:
    """A validated execution strategy selected before a run starts."""

    strategy: ExecutionStrategy
    reason: str


@dataclass
class TraceEvent:
    kind: EventKind
    message: str
    timestamp: str = field(default_factory=utc_now)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunState:
    task: str
    mode: RunMode
    run_id: str = field(default_factory=new_run_id)
    strategy: ExecutionStrategy = "reactive"
    strategy_reason: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)
    actions: list[AgentAction] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)
    completed_steps: list[int] = field(default_factory=list)
    plan: ExecutionPlan | None = None
    plan_history: list[ExecutionPlan] = field(default_factory=list)
    replan_count: int = 0
    final_answer: str | None = None
    status: RunStatus = "running"

    def add_event(self, kind: EventKind, message: str, **data: Any) -> None:
        self.events.append(TraceEvent(kind=kind, message=message, data=data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "mode": self.mode,
            "run_id": self.run_id,
            "strategy": self.strategy,
            "strategy_reason": self.strategy_reason,
            "history": self.history,
            "actions": [asdict(action) for action in self.actions],
            "events": [asdict(event) for event in self.events],
            "completed_steps": self.completed_steps,
            "plan": asdict(self.plan) if self.plan else None,
            "plan_history": [asdict(plan) for plan in self.plan_history],
            "replan_count": self.replan_count,
            "final_answer": self.final_answer,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        """Rebuild persisted state without coupling the domain to a storage backend."""

        def action(value: dict[str, Any]) -> AgentAction:
            return AgentAction(**value)

        def plan(value: dict[str, Any]) -> ExecutionPlan:
            return ExecutionPlan(
                goal=value["goal"],
                steps=[
                    PlanStep(
                        id=step["id"],
                        description=step["description"],
                        action=action(step["action"]),
                        success_criteria=step.get("success_criteria", ""),
                        status=step.get("status", "pending"),
                        result=step.get("result"),
                    )
                    for step in value.get("steps", [])
                ],
                final_answer=value.get("final_answer"),
                revision=value.get("revision", 1),
            )

        return cls(
            task=data["task"],
            mode=data["mode"],
            run_id=data["run_id"],
            strategy=data.get("strategy", "reactive"),
            strategy_reason=data.get("strategy_reason"),
            history=[dict(item) for item in data.get("history", [])],
            actions=[action(item) for item in data.get("actions", [])],
            events=[TraceEvent(**item) for item in data.get("events", [])],
            completed_steps=list(data.get("completed_steps", [])),
            plan=plan(data["plan"]) if data.get("plan") else None,
            plan_history=[plan(item) for item in data.get("plan_history", [])],
            replan_count=data.get("replan_count", 0),
            final_answer=data.get("final_answer"),
            status=data.get("status", "running"),
        )
