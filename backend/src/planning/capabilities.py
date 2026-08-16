"""Planner capability discovery with an isolated legacy adapter."""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from backend.domain import AgentAction, AssistantMessage, ToolMessage, message_to_dict
from backend.runtime.core.context import AgentRuntime

from .base import NamedPlanner, OutputRepairReporter, Planner, RunFinalizer, SkillSelector


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


class _LegacyDecision:
    def __init__(self, target: object) -> None:
        self.target = target
        self.name = getattr(target, "name", target.__class__.__name__)

    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        result = self.target.decide(
            _legacy_history(runtime), runtime.run.mode, on_reasoning=runtime.exchange.on_reasoning
        )
        return _assistant(runtime, result)


@dataclass(frozen=True)
class PlannerCapabilities:
    name: str
    decision_planner: Planner | None
    skill_selector: SkillSelector | None
    output_repair_reporter: OutputRepairReporter | None
    run_finalizer: RunFinalizer | None

    @classmethod
    def from_planner(cls, planner: object) -> PlannerCapabilities:
        name = planner.name if isinstance(planner, NamedPlanner) else planner.__class__.__name__
        decide = getattr(planner, "decide", None)
        select_skills = getattr(planner, "select_skills", None)
        finalize = getattr(planner, "finalize", None)
        return cls(
            name=name if isinstance(name, str) and name else planner.__class__.__name__,
            decision_planner=(planner if _uses_runtime(decide) else _LegacyDecision(planner))
            if callable(decide)
            else None,
            skill_selector=planner if _uses_runtime(select_skills) else None,
            output_repair_reporter=planner if isinstance(planner, OutputRepairReporter) else None,
            run_finalizer=planner if callable(finalize) else None,
        )
