"""Neutral output formatting and result-message shaping for job carriers.

This module gives job carriers (notably :class:`SubprocessJob`) the same
``stdout:``/``stderr:``-sectioned, budget-truncated output rendering that
``tools/command.WorkspaceCommand`` produces, re-implemented independently so
the jobs package never imports the tools layer. It also defines the
result-message dictionary any carrier uses to describe how a process ended, and
an :class:`ErrorFormatter` that passes those crafted messages through verbatim.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["CommandError", "ErrorFormatter", "MessageErrorFormatter", "format_command_output"]

#: Hardware-independent default: a per-stream budget shared by both sections.
_DEFAULT_MAX_CHARS = 20_000


class ErrorFormatter(Protocol):
    """Turns an exception into safe, persistable text."""

    def format_error(self, exception: BaseException) -> str: ...


class CommandError(RuntimeError):
    """A domain exception describing how a subprocess ended.

    The message is neutral result text ("Command exited with code N.",
    "Command timed out after N seconds.") and never includes the command line,
    environment, or raw captured output. ``JobInfo.error`` only ever holds the
    text an :class:`ErrorFormatter` derives from this exception.
    """


class MessageErrorFormatter:
    """Pass crafted command-result messages through; everything else stays safe.

    Intended for job result messages produced by :class:`CommandError`, so a
    caller can align ``JobInfo.error`` with the exact strings
    :class:`~backend.tools.command.WorkspaceCommand` emits. For any other
    exception it falls back to emitting only the class name, so raw launch
    ``OSError``/``FileNotFoundError`` strings (which embed command lines and
    executable paths — global constraint 4) never leak into ``JobInfo.error``.
    """

    def format_error(self, exception: BaseException) -> str:
        if isinstance(exception, CommandError):
            return str(exception)
        return type(exception).__name__


def format_command_output(
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """Render captured output in ``tools/command``-compatible shape.

    Builds ``stdout:``/``stderr:`` sections for each non-empty stream and, when
    the combined length exceeds ``max_chars``, truncates each stream against a
    shared payload budget, appending an "… output truncated (N characters
    omitted)" marker. The algorithm mirrors
    :meth:`tools.command.WorkspaceCommand._format_output`.
    """
    streams: list[tuple[str, str]] = []
    if stdout:
        streams.append(("stdout", _as_text(stdout)))
    if stderr:
        streams.append(("stderr", _as_text(stderr)))
    if not streams:
        return ""

    complete = "\n".join(f"{label}:\n{value}" for label, value in streams)
    if len(complete) <= max_chars:
        return complete

    marker = ""
    allocations = [0] * len(streams)
    for _ in range(8):
        fixed_chars = sum(len(label) + 2 for label, _value in streams) + len(streams) - 1 + len(marker)
        payload_budget = max(0, max_chars - fixed_chars)
        allocations = _allocate_payload([len(value) for _label, value in streams], payload_budget)
        omitted = sum(len(value) - allocation for (_label, value), allocation in zip(streams, allocations))
        updated_marker = f"\n… output truncated ({omitted} characters omitted)"
        if updated_marker == marker:
            break
        marker = updated_marker

    parts = [f"{label}:\n{value[:allocation]}" for (label, value), allocation in zip(streams, allocations)]
    return "\n".join(parts) + marker


def _allocate_payload(lengths: list[int], budget: int) -> list[int]:
    allocations = [min(length, budget // len(lengths)) for length in lengths]
    remaining = budget - sum(allocations)
    for index, length in enumerate(lengths):
        extra = min(length - allocations[index], remaining)
        allocations[index] += extra
        remaining -= extra
        if remaining == 0:
            break
    return allocations


def _as_text(value: str | bytes) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
