"""Provider-specific failures, separate from UI and runtime implementations."""

from typing import Any

from mini_agent.domain import PlanningError


class ModelConfigurationError(ValueError):
    """The local model configuration is incomplete."""


class ModelRequestError(PlanningError):
    """The model endpoint could not provide a usable response."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message, diagnostics=diagnostics)
