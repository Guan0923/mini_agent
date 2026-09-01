"""Internal ports and persistence adapter for Subagent coordination."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol

from backend.domain import TurnTrace
from backend.domain.runtime_state import NodeFrame, RuntimeState

from ..core.context import AgentRuntime


class ChildRunner(Protocol):
    tools: object
    subagents: object | None

    def new_runtime(self, *, task: str, session_id: str | None = None, **kwargs: object) -> AgentRuntime: ...

    def run(self, runtime: AgentRuntime) -> object: ...


class AgentThreadEvents(Protocol):
    def start_turn(self, turn: RuntimeState) -> None: ...

    def publish_frame(self, thread_id: str, frame: NodeFrame, current: RuntimeState) -> None: ...

    def finish_turn(self, thread_id: str, turn: RuntimeState) -> None: ...


@dataclass(frozen=True, slots=True)
class _SessionBinding:
    runner_factory: Callable[[], ChildRunner]
    workspace: Path
    project_workspace: Path | None = None


@dataclass(slots=True)
class _StatusControl:
    token: str
    requested_status: str
    settled: Event
    claimed: bool = False


class _CanonicalRuntimeStore:
    """Runner persistence adapter; the canonical node bridge owns every message."""

    def __init__(self, store: object, session_id: str) -> None:
        self.store = store
        self.session_id = session_id

    def save_runtime(self, _state: object) -> None:
        return None

    def append_turn_input(
        self,
        _session_id: str,
        _run_id: str,
        _content: str,
        *,
        delivery_id: str | None = None,
    ) -> None:
        del delivery_id

    def has_turn_delivery(self, _session_id: str, delivery_id: str) -> bool:
        return bool(getattr(self.store, "has_canonical_delivery")(self.session_id, delivery_id))

    def get_node(self, _session_id: str, turn_id: str) -> object | None:
        return getattr(self.store, "get_node")(self.session_id, turn_id)

    def initialize_turn_trace(self, _session_id: str, trace: TurnTrace) -> TurnTrace:
        return getattr(self.store, "initialize_turn_trace")(self.session_id, trace)

    def register_turn_report(self, turn_id: str, agent_thread_id: str, recipient_thread_id: str) -> object:
        return getattr(self.store, "register_agent_turn_report")(
            self.session_id,
            turn_id,
            agent_thread_id,
            recipient_thread_id,
        )


__all__ = ["AgentThreadEvents", "ChildRunner"]
