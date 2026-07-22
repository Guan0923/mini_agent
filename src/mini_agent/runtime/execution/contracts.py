"""Execution port used by application services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from mini_agent.domain import ChatMessage, RunState

from ..core.context import AgentRuntime, RuntimeStore

if TYPE_CHECKING:
    from mini_agent.planning.context_management import ContextCompactionResult


class RuntimeRunner(Protocol):
    """Minimal execution contract required by conversation orchestration."""

    def empty_runtime(
        self,
        *,
        session_id: str,
        messages: list[ChatMessage] | None = None,
        runtime_store: RuntimeStore | None = None,
    ) -> AgentRuntime: ...

    def bind(self, runtime: AgentRuntime) -> AgentRuntime: ...

    def compact_context(self, runtime: AgentRuntime) -> ContextCompactionResult: ...

    def run(self, runtime: AgentRuntime) -> RunState: ...
