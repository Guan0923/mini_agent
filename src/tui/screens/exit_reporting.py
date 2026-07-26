"""Classify how an interactive Textual view stopped."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TuiExitReason = Literal["normal", "textual_exception", "task_exception", "unexpected_exit"]


@dataclass(frozen=True)
class TuiExitReport:
    """The durable facts needed to report one TUI exit."""

    reason: TuiExitReason
    exit_code: int
    textual_return_code: int | None
    error: BaseException | None
    snapshot: dict[str, object]


def classify_tui_exit(
    view: object,
    *,
    task_error: BaseException | None,
    view_ended_early: bool,
    normal_exit_requested: bool,
) -> TuiExitReport:
    """Classify a stopped view without relying on Textual to re-raise errors."""

    unhandled = getattr(view, "unhandled_exception", None)
    return_code = getattr(view, "return_code", None)
    textual_return_code = return_code if isinstance(return_code, int) else None
    snapshot_method = getattr(view, "diagnostic_snapshot", None)
    snapshot: dict[str, object]
    if callable(snapshot_method):
        try:
            value = snapshot_method()
            snapshot = dict(value) if isinstance(value, dict) else {}
        except Exception as error:
            snapshot = {
                "snapshot_error_type": type(error).__name__,
                "snapshot_error_message": str(error),
            }
    else:
        snapshot = {}

    if isinstance(unhandled, (EOFError, KeyboardInterrupt)):
        return TuiExitReport("normal", 0, textual_return_code, None, snapshot)
    if isinstance(unhandled, BaseException):
        return TuiExitReport("textual_exception", 1, textual_return_code, unhandled, snapshot)
    if task_error is not None:
        return TuiExitReport("task_exception", 1, textual_return_code, task_error, snapshot)
    if textual_return_code not in {None, 0}:
        return TuiExitReport("unexpected_exit", 1, textual_return_code, None, snapshot)
    if view_ended_early and not normal_exit_requested:
        return TuiExitReport("unexpected_exit", 1, textual_return_code, None, snapshot)
    return TuiExitReport("normal", 0, textual_return_code, None, snapshot)
