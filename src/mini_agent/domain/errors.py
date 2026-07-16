"""Shared errors that can cross application-layer boundaries safely."""

from typing import Any


class PlanningError(RuntimeError):
    """A planner could not produce a valid decision or execution plan."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}
