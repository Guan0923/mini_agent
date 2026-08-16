"""Error formatting protocol and the default safe formatting policy.

The job core model never persists raw exceptions, command lines, environment
values, or credentials.  ``JobInfo.error`` only ever holds text produced by an
:class:`ErrorFormatter`; later runtime integration injects the existing
redaction pipeline through this same port.
"""

from __future__ import annotations

from typing import Protocol


class ErrorFormatter(Protocol):
    """Turns an exception into safe, persistable text."""

    def format_error(self, exception: BaseException) -> str: ...


class ClassNameErrorFormatter:
    """Default policy: emit only the exception class name.

    ``str(exception)`` may embed command lines, secrets, or file paths, so the
    default formatter deliberately discards the message entirely: a
    ``TimeoutError`` stays ``"TimeoutError"``.
    """

    def format_error(self, exception: BaseException) -> str:
        return type(exception).__name__
