"""Planner contracts consumed by the runtime."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.domain import AssistantMessage
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
class SkillSelector(Protocol):
    def select_skills(self, runtime: AgentRuntime) -> SkillSelection: ...


@runtime_checkable
class OutputRepairReporter(Protocol):
    def consume_output_repairs(self) -> list[dict[str, str | int]]: ...


@runtime_checkable
class ContextCompactor(Protocol):
    def compact_context(self, runtime: AgentRuntime) -> ContextCompactionResult: ...
