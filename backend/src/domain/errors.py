"""Shared errors that can cross application-layer boundaries safely."""

from typing import Any


class PlanningError(RuntimeError):
    """A planner could not produce a valid decision or execution plan."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class ModelOutputError(PlanningError):
    """A model response was received but did not satisfy the required contract."""

    _MAX_PREVIEW_CHARS = 2_000

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        invalid_output: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.operation = operation
        self.validation_error = message
        self.invalid_output_preview = (invalid_output or "")[: self._MAX_PREVIEW_CHARS]
