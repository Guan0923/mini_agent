"""Execution port used by application services."""

from __future__ import annotations

from typing import Protocol

from mini_agent.domain import ChatMessage, RunState

from ..core.context import AgentRuntime, RuntimeStore


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

    def run(self, runtime: AgentRuntime) -> RunState: ...
