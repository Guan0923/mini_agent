"""Execution port used by application services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from backend.domain import ChatMessage, RunState

from ..core.context import AgentRuntime
from ..core.ports import RuntimeStore

if TYPE_CHECKING:
    from backend.planning.context_management import ContextCompactionResult


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

    def generate_title(self, runtime: AgentRuntime, first_user_text: str) -> str: ...

    def run(self, runtime: AgentRuntime) -> RunState: ...

    def resume(self, runtime: AgentRuntime) -> RunState: ...
