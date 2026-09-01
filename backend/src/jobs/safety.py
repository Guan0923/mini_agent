"""Error formatting protocol and the default safe root-message policy."""

from __future__ import annotations

from typing import Protocol

from backend.domain import safe_error_message


class ErrorFormatter(Protocol):
    """Turns an exception into safe, persistable text."""

    def format_error(self, exception: BaseException) -> str: ...


class ClassNameErrorFormatter:
    """Project the redacted root message, using its class only when empty."""

    def format_error(self, exception: BaseException) -> str:
        return safe_error_message(exception)
