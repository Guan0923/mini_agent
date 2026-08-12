"""Runtime event sink that captures run_finished telemetry and per-request usage."""

from __future__ import annotations

from typing import Any

from backend.runtime.core.events import RuntimeEvent
from backend.runtime.persistence.recording import persistent_event


def _token_value(usage: Any, key: str) -> int:
    """Read one token count from a mapping or a dataclass usage object."""
    if usage is None:
        return 0
    if isinstance(usage, dict):
        value = usage.get(key)
    else:
        value = getattr(usage, key, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


class EventCollector:
    """Collect metrics from the events published by one agent run.

    `run_finished` carries the authoritative duration and action counts; the
    aggregate token totals are summed per `model_response` event because the
    `usage` on `run_finished` only reflects the final model call.
    """

    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []
        self.run_finished: dict[str, Any] | None = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.tool_calls_by_name: dict[str, int] = {}
        self.subagent_completed = 0
        self.subagent_failed = 0

    def __call__(self, event: RuntimeEvent) -> None:
        self.events.append(event)
        if event.kind == "run_finished":
            self.run_finished = event.data
        elif event.kind == "model_response":
            usage = event.data.get("usage")
            self.prompt_tokens += _token_value(usage, "prompt_tokens")
            self.completion_tokens += _token_value(usage, "completion_tokens")
            self.total_tokens += _token_value(usage, "total_tokens")
        elif event.kind == "tool_call" and event.message:
            name = event.message
            self.tool_calls_by_name[name] = self.tool_calls_by_name.get(name, 0) + 1
        elif event.kind == "subagent_completed":
            self.subagent_completed += 1
        elif event.kind == "subagent_failed":
            self.subagent_failed += 1

    def trace(self) -> list[dict[str, Any]]:
        """Return the full, recursively redacted event stream for diagnostics."""

        trace: list[dict[str, Any]] = []
        for event in sorted(self.events, key=lambda item: item.timestamp):
            message, data = persistent_event(event, include_full_messages=True)
            trace.append(
                {
                    "kind": event.kind,
                    "timestamp": event.timestamp,
                    "message": message,
                    "data": data,
                }
            )
        return trace
