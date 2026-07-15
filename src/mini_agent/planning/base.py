"""Planner contract consumed by the runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from mini_agent.domain import AgentAction, ExecutionPlan, PlanStep, RunMode, StepEvaluation, StrategySelection


class PlanningError(RuntimeError):
    """A plan could not be produced or failed validation."""


class Planner(Protocol):
    name: str

    def decide(
        self,
        history: list[dict[str, str]],
        mode: RunMode,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> AgentAction: ...


class ExecutionPlanner(Planner, Protocol):
    """Optional planner capability for strategies that execute a fixed plan."""

    def create_plan(
        self,
        history: list[dict[str, str]],
        mode: RunMode,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ExecutionPlan: ...


class StrategySelector(Protocol):
    """Optional planner capability used by the automatic execution-policy router."""

    def select_strategy(self, history: list[dict[str, str]], mode: RunMode) -> StrategySelection: ...


class DynamicReplanner(ExecutionPlanner, Protocol):
    """Optional planner capability for evaluating and repairing an active plan."""

    def evaluate_step(
        self,
        history: list[dict[str, str]],
        plan: ExecutionPlan,
        step: PlanStep,
        result: str,
    ) -> StepEvaluation: ...

    def replan(
        self,
        history: list[dict[str, str]],
        plan: ExecutionPlan,
        reason: str,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ExecutionPlan: ...
