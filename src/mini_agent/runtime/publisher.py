"""Enrich all runtime events with immutable run context before publishing them."""

from __future__ import annotations

from collections.abc import Callable

from mini_agent.domain import RunState

from .contracts import EventHandler
from .events import CHECKPOINT_EVENT_KINDS, RuntimeEvent


class RunEventPublisher:
    """Adds run metadata once at the runtime boundary, independent of event sinks."""

    def __init__(
        self,
        state: RunState,
        sink: EventHandler,
        checkpoint: Callable[[RunState, str], None] | None = None,
    ) -> None:
        self._state = state
        self._sink = sink
        self._checkpoint = checkpoint

    def __call__(self, event: RuntimeEvent) -> None:
        if self._checkpoint is not None and event.kind in CHECKPOINT_EVENT_KINDS:
            self._checkpoint(self._state, event.kind)
        context = {
            "run_id": self._state.run_id,
            "task": self._state.task,
            "mode": self._state.mode,
            "strategy": self._state.strategy,
            "status": self._state.status,
        }
        self._sink(RuntimeEvent(event.kind, event.message, {**event.data, **context}))
