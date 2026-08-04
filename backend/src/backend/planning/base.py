"""Planner contracts consumed by the runtime."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.domain import AssistantMessage, ExecutionPlan, StepEvaluation, StrategySelection
from backend.domain.skills import SkillSelection
from backend.runtime.core.context import AgentRuntime

from .context_management import ContextCompactionResult


@runtime_checkable
class NamedPlanner(Protocol):
    name: str


@runtime_checkable
class Planner(NamedPlanner, Protocol):
    def decide(self, runtime: AgentRuntime) -> AssistantMessage: ...


@runtime_checkable
class RunFinalizer(NamedPlanner, Protocol):
    def finalize(self, runtime: AgentRuntime, reason: str) -> AssistantMessage: ...


@runtime_checkable
class PlanCreator(NamedPlanner, Protocol):
    def create_plan(self, runtime: AgentRuntime) -> ExecutionPlan: ...


@runtime_checkable
class StrategySelector(Protocol):
    def select_strategy(self, runtime: AgentRuntime) -> StrategySelection: ...


@runtime_checkable
class SkillSelector(Protocol):
    def select_skills(self, runtime: AgentRuntime) -> SkillSelection: ...


@runtime_checkable
class DynamicPlanCreator(NamedPlanner, Protocol):
    def create_dynamic_plan(self, runtime: AgentRuntime) -> ExecutionPlan: ...


@runtime_checkable
class PlanReplanner(NamedPlanner, Protocol):
    def replan(self, runtime: AgentRuntime) -> ExecutionPlan: ...


@runtime_checkable
class DynamicReplanner(PlanReplanner, Protocol):
    def evaluate_step(self, runtime: AgentRuntime) -> StepEvaluation: ...


@runtime_checkable
class OutputRepairReporter(Protocol):
    def consume_output_repairs(self) -> list[dict[str, str | int]]: ...


@runtime_checkable
class ContextCompactor(Protocol):
    def compact_context(self, runtime: AgentRuntime) -> ContextCompactionResult: ...


class ExecutionPlanner(Planner, PlanCreator, Protocol):
    """Backward-compatible composite protocol for fixed-plan planners."""
