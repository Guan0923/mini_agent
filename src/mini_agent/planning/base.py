"""Planner contracts consumed by the runtime."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mini_agent.domain import AssistantMessage, ExecutionPlan, StepEvaluation, StrategySelection
from mini_agent.runtime.core.context import AgentRuntime


@runtime_checkable
class NamedPlanner(Protocol):
    name: str


@runtime_checkable
class Planner(NamedPlanner, Protocol):
    def decide(self, runtime: AgentRuntime) -> AssistantMessage: ...


@runtime_checkable
class PlanCreator(NamedPlanner, Protocol):
    def create_plan(self, runtime: AgentRuntime) -> ExecutionPlan: ...


@runtime_checkable
class StrategySelector(Protocol):
    def select_strategy(self, runtime: AgentRuntime) -> StrategySelection: ...


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


class ExecutionPlanner(Planner, PlanCreator, Protocol):
    """Backward-compatible composite protocol for fixed-plan planners."""
