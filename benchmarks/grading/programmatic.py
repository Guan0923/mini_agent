"""Programmatic checkers and reusable checker factories.

Each checker is ``Callable[[CheckContext], CheckerVerdict]``. A verifier may
return diagnostics for several conditions, but the suite aggregates them as a
strict binary result: every required condition must pass for a score of 1.0.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..model import BenchmarkTask, CheckContext, CheckerVerdict


def run_checkers(task: BenchmarkTask, context: CheckContext) -> list[CheckerVerdict]:
    """Evaluate every checker of a task, never letting one crash the run."""
    verdicts: list[CheckerVerdict] = []
    for checker in task.checkers:
        try:
            verdict = checker(context)
        except Exception as exc:
            verdict = CheckerVerdict(0.0, detail=f"checker raised {type(exc).__name__}: {exc}")
        if not isinstance(verdict, CheckerVerdict):
            verdict = CheckerVerdict(0.0, detail="checker returned a non-verdict value")
        verdicts.append(verdict)
    return verdicts


def predicate(check: Callable[[CheckContext], Any], detail: str = "") -> Callable[[CheckContext], CheckerVerdict]:
    """Wrap a boolean predicate as a pass/fail checker."""

    def _check(context: CheckContext) -> CheckerVerdict:
        try:
            passed = bool(check(context))
        except Exception as exc:
            return CheckerVerdict(0.0, detail=f"predicate raised {type(exc).__name__}: {exc}")
        return CheckerVerdict(1.0 if passed else 0.0, detail=detail)

    return _check


def files_exist(*relpaths: str) -> Callable[[CheckContext], CheckerVerdict]:
    def _check(context: CheckContext) -> CheckerVerdict:
        missing = [path for path in relpaths if not (context.workspace / path).exists()]
        if missing:
            return CheckerVerdict(0.0, detail=f"missing files: {', '.join(missing)}")
        return CheckerVerdict(1.0, detail=f"files exist: {', '.join(relpaths)}")

    return _check


def content_contains(relpath: str, needle: str) -> Callable[[CheckContext], CheckerVerdict]:
    def _check(context: CheckContext) -> CheckerVerdict:
        path = context.workspace / relpath
        if not path.exists():
            return CheckerVerdict(0.0, detail=f"{relpath} does not exist")
        text = path.read_text(encoding="utf-8", errors="replace")
        if needle in text:
            return CheckerVerdict(1.0, detail=f"{relpath} contains {needle!r}")
        return CheckerVerdict(0.0, detail=f"{relpath} lacks {needle!r}")

    return _check


def content_matches(relpath: str, regex: str) -> Callable[[CheckContext], CheckerVerdict]:
    import re

    def _check(context: CheckContext) -> CheckerVerdict:
        path = context.workspace / relpath
        if not path.exists():
            return CheckerVerdict(0.0, detail=f"{relpath} does not exist")
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(regex, text, flags=re.MULTILINE):
            return CheckerVerdict(1.0, detail=f"{relpath} matches {regex!r}")
        return CheckerVerdict(0.0, detail=f"{relpath} does not match {regex!r}")

    return _check


def tool_used(name: str, *, minimum: int = 1) -> Callable[[CheckContext], CheckerVerdict]:
    def _check(context: CheckContext) -> CheckerVerdict:
        count = context.tool_calls_by_name.get(name, 0)
        if count >= minimum:
            return CheckerVerdict(1.0, detail=f"{name} called {count}x")
        return CheckerVerdict(0.0, detail=f"{name} called {count}x (expected {minimum})")

    return _check


def final_answer_contains(needle: str) -> Callable[[CheckContext], CheckerVerdict]:
    def _check(context: CheckContext) -> CheckerVerdict:
        if needle in context.final_answer:
            return CheckerVerdict(1.0, detail=f"final answer contains {needle!r}")
        return CheckerVerdict(0.0, detail=f"final answer lacks {needle!r}")

    return _check


def status_completed(context: CheckContext) -> CheckerVerdict:
    if context.status == "completed":
        return CheckerVerdict(1.0, detail="run completed")
    return CheckerVerdict(0.0, detail=f"run status: {context.status}")


def skill_activated(name: str) -> Callable[[CheckContext], CheckerVerdict]:
    def _check(context: CheckContext) -> CheckerVerdict:
        active = context.metrics.active_skill_names
        if name in active:
            return CheckerVerdict(1.0, detail=f"skill {name!r} activated")
        return CheckerVerdict(0.0, detail=f"skill {name!r} not activated (active: {active})")

    return _check
