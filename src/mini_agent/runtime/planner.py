"""Planner ports and legacy compatibility owned by the execution runtime."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mini_agent.domain import (
    AgentAction,
    AssistantMessage,
    ExecutionPlan,
    StepEvaluation,
    ToolMessage,
    message_to_dict,
)

from .context import AgentRuntime


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


def _uses_runtime(method: object) -> bool:
    if not callable(method):
        return False
    try:
        return len(inspect.signature(method).parameters) == 1
    except (TypeError, ValueError):
        return False


def _legacy_history(runtime: AgentRuntime) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for message in runtime.state.messages:
        if isinstance(message, AssistantMessage) and message.tool_messages:
            calls = ", ".join(f"{tool.name} {tool.arguments}" for tool in message.tool_messages)
            history.append({"role": "assistant", "content": _bounded(f"[Tool call] {calls}")})
            for tool in message.tool_messages:
                prefix = "[Tool result]" if tool.status == "succeeded" else "[Tool error]"
                history.append({"role": "tool", "content": _bounded(f"{prefix}\n{tool.content or ''}")})
        else:
            payload = message_to_dict(message)
            history.append({"role": str(payload["role"]), "content": str(payload.get("content") or "")})
    return history


def _bounded(value: str, limit: int = 2_000) -> str:
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}… ({omitted} characters omitted)"


def _assistant(runtime: AgentRuntime, result: object) -> AssistantMessage:
    if isinstance(result, AssistantMessage):
        return result
    if not isinstance(result, AgentAction):
        raise TypeError("Legacy planner decide() must return AgentAction or AssistantMessage.")
    if result.type == "final_answer":
        return AssistantMessage(content=result.answer or "", reasoning=result.reasoning)
    if not result.tool:
        raise TypeError("Legacy tool action is missing a tool name.")
    return AssistantMessage(
        reasoning=result.reasoning,
        tool_messages=[ToolMessage(name=result.tool, call_id=runtime.next_tool_call_id(), arguments=result.arguments)],
    )


class _LegacyPlannerAdapter:
    """Adapt the deprecated history/mode planner surface to ``AgentRuntime``."""

    def __init__(self, target: object) -> None:
        self.target = target
        self.name = getattr(target, "name", target.__class__.__name__)

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        result = self.target.decide(
            _legacy_history(runtime), runtime.run.mode, on_reasoning=runtime.exchange.on_reasoning
        )
        return _assistant(runtime, result)

    def create_plan(self, runtime: AgentRuntime):
        return self.target.create_plan(
            _legacy_history(runtime), runtime.run.mode, on_reasoning=runtime.exchange.on_reasoning
        )

    def create_dynamic_plan(self, runtime: AgentRuntime):
        return self.target.create_dynamic_plan(
            _legacy_history(runtime), runtime.run.mode, on_reasoning=runtime.exchange.on_reasoning
        )

    def replan(self, runtime: AgentRuntime):
        context = runtime.exchange.context
        return self.target.replan(
            _legacy_history(runtime),
            context.get("plan"),
            context.get("reason", ""),
            on_reasoning=runtime.exchange.on_reasoning,
        )

    def evaluate_step(self, runtime: AgentRuntime):
        context = runtime.exchange.context
        return self.target.evaluate_step(
            _legacy_history(runtime),
            context.get("plan"),
            context.get("step"),
            context.get("result", ""),
        )


@dataclass(frozen=True)
class PlannerCapabilities:
    name: str
    decision_planner: Planner | None
    plan_creator: PlanCreator | None
    dynamic_plan_creator: DynamicPlanCreator | None
    plan_replanner: PlanReplanner | None
    dynamic_replanner: DynamicReplanner | None
    output_repair_reporter: OutputRepairReporter | None

    @classmethod
    def from_planner(cls, planner: object) -> PlannerCapabilities:
        name = planner.name if isinstance(planner, NamedPlanner) else planner.__class__.__name__
        decide = getattr(planner, "decide", None)
        create = getattr(planner, "create_plan", None)
        dynamic_create = getattr(planner, "create_dynamic_plan", None)
        replan = getattr(planner, "replan", None)
        evaluate = getattr(planner, "evaluate_step", None)
        legacy = _LegacyPlannerAdapter(planner)
        return cls(
            name=name if isinstance(name, str) and name else planner.__class__.__name__,
            decision_planner=(planner if _uses_runtime(decide) else legacy) if callable(decide) else None,
            plan_creator=(planner if _uses_runtime(create) else legacy) if callable(create) else None,
            dynamic_plan_creator=(planner if _uses_runtime(dynamic_create) else legacy)
            if callable(dynamic_create)
            else None,
            plan_replanner=(planner if _uses_runtime(replan) else legacy) if callable(replan) else None,
            dynamic_replanner=(planner if _uses_runtime(evaluate) and _uses_runtime(replan) else legacy)
            if callable(evaluate) and callable(replan)
            else None,
            output_repair_reporter=planner if isinstance(planner, OutputRepairReporter) else None,
        )
