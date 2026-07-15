"""Composable event sinks used by interfaces and persistent logging."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from mini_agent.runtime.events import RuntimeEvent


class EventFanout:
    """Deliver every runtime event to each configured independent sink."""

    def __init__(self, sinks: Iterable[Callable[[RuntimeEvent], None]]) -> None:
        self._sinks = tuple(sinks)

    def __call__(self, event: RuntimeEvent) -> None:
        for sink in self._sinks:
            sink(event)
