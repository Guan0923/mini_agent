"""Execution plan and strategy values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from .messages import ToolMessage

ExecutionStrategy = Literal["reactive", "dynamic_replan"]
PlanStepStatus = Literal["pending", "running", "completed", "failed", "superseded"]
ReplanDecision = Literal["continue", "replan"]
ActionType = Literal["tool_call", "final_answer"]


@dataclass(frozen=True)
class AgentAction:
    """Deprecated input adapter for planners written before ToolMessage."""

    type: ActionType
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    reasoning: str | None = None


@dataclass(init=False)
class PlanStep:
    """One validated, executable tool operation in a precomputed plan."""

    id: str
    description: str
    tool_message: ToolMessage
    success_criteria: str = ""
    status: PlanStepStatus = "pending"
    result: str | None = None

    def __init__(
        self,
        id: str,
        description: str,
        tool_message: ToolMessage | None = None,
        *,
        action: AgentAction | ToolMessage | None = None,
        success_criteria: str = "",
        status: PlanStepStatus = "pending",
        result: str | None = None,
    ) -> None:
        selected = tool_message or action
        if isinstance(selected, AgentAction):
            if selected.type != "tool_call" or not selected.tool:
                raise ValueError("PlanStep compatibility actions must be tool calls.")
            selected = ToolMessage(
                name=selected.tool,
                call_id=f"call_{uuid4().hex}",
                arguments=selected.arguments,
            )
        if not isinstance(selected, ToolMessage):
            raise ValueError("PlanStep requires a ToolMessage.")
        self.id = id
        self.description = description
        self.tool_message = selected
        self.success_criteria = success_criteria
        self.status = status
        self.result = result

    @property
    def action(self) -> AgentAction:
        return AgentAction(type="tool_call", tool=self.tool_message.name, arguments=self.tool_message.arguments)


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


