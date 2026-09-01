"""Shared errors and safe user-visible error projection."""

import re
from typing import Any

_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(api[\s_-]?(?:key|token)|authorization|cookie|password|secret|token)\b"
    r"[\"']?\s*([=:])\s*[\"']?([^\r\n,;&\"'}]+)"
)


def root_error(error: BaseException) -> BaseException:
    """Return the deepest active explicit or implicit cause without looping forever."""

    current = error
    seen = {id(current)}
    while True:
        candidate = current.__cause__
        if candidate is None and not current.__suppress_context__:
            candidate = current.__context__
        if candidate is None or id(candidate) in seen:
            return current
        seen.add(id(candidate))
        current = candidate


def redact_sensitive_text(value: str) -> str:
    """Redact credential-shaped values from one externally visible string."""

    return _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)


def safe_error_message(error: BaseException) -> str:
    """Project one exception chain to its redacted, unwrapped root message."""

    root = root_error(error)
    message = str(root)
    return redact_sensitive_text(message if message else root.__class__.__name__)


class PlanningError(RuntimeError):
    """A planner could not produce a valid decision or execution plan."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class TracePersistenceError(RuntimeError):
    """A required Turn audit snapshot could not be saved before transport."""


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
