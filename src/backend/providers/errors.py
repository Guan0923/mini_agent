"""Provider-specific failures, separate from UI and runtime implementations."""

from typing import Any

from backend.domain import ModelOutputError, PlanningError


class ModelConfigurationError(ValueError):
    """The local model configuration is incomplete."""


class ModelRequestError(PlanningError):
    """The model endpoint could not provide a usable response."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message, diagnostics=diagnostics)


class ModelTransportError(ModelRequestError):
    """A transport failure with enough metadata to apply a retry policy."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        retry_after: float | None = None,
        stream_started: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, diagnostics=diagnostics)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.stream_started = stream_started


class ProviderOutputError(ModelOutputError, ModelRequestError):
    """Provider response validation failed while preserving request-error compatibility."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        invalid_output: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        ModelOutputError.__init__(
            self,
            message,
            operation=operation,
            invalid_output=invalid_output,
            diagnostics=diagnostics,
        )
