"""Interrupt handlers and the run-time decision registry for interactive chat.

Auto-approve mirrors the offline policy used by the benchmark harness; the
interactive handler pauses a run and asks the client to decide, which the TUI
client resolves through ``POST /api/decisions``.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from time import monotonic
from typing import Any

from backend.runtime.core.contracts import InterruptDecision, InterruptRequest


def auto_approve(request: InterruptRequest) -> InterruptDecision:
    if request.kind == "plan":
        return InterruptDecision("implement")
    if request.kind == "tool":
        return InterruptDecision("continue")
    if request.kind == "question":
        answers = {q.id: [q.options[0].label] for q in request.questions}
        return InterruptDecision("answer", answers=answers)
    if request.kind == "skill":
        return InterruptDecision("skip")
    return InterruptDecision("continue")


class _PendingDecision:
    def __init__(self, owner_id: str | None = None) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] = {}
        self.owner_id = owner_id


class DecisionRegistry:
    """Thread-safe store of decisions the run is currently waiting on."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingDecision] = {}
        self._lock = threading.Lock()

    def register(self, decision_id: str, owner_id: str | None = None) -> _PendingDecision:
        pending = _PendingDecision(owner_id)
        with self._lock:
            self._pending[decision_id] = pending
        return pending

    def resolve(self, decision_id: str, decision: dict[str, Any], owner_id: str | None = None) -> bool:
        with self._lock:
            pending = self._pending.get(decision_id)
            if pending is None:
                return False
            # Network approvals are scoped to the authenticated user.  The
            # optional owner keeps the registry compatible with local/runtime
            # callers that do not have a web identity.
            if pending.owner_id is not None and pending.owner_id != owner_id:
                return False
            self._pending.pop(decision_id, None)
        if pending is None:
            return False
        pending.result.update(decision)
        pending.event.set()
        return True

    def discard(self, decision_id: str) -> None:
        with self._lock:
            self._pending.pop(decision_id, None)


registry = DecisionRegistry()


def make_interactive_interrupt(
    sink,
    *,
    timeout: float = 120.0,
    cancel_requested: Callable[[], bool] | None = None,
    auto_approve_tools: bool = False,
    owner_id: str | None = None,
):
    """Build an interrupt handler that pauses the run and asks the client."""

    def decide(request: InterruptRequest) -> InterruptDecision:
        if request.kind == "tool" and auto_approve_tools:
            return InterruptDecision("continue")
        decision_id = f"dec_{uuid.uuid4().hex}"
        questions = [
            {
                "id": question.id,
                "header": question.header,
                "question": question.question,
                "options": [{"label": option.label, "description": option.description} for option in question.options],
            }
            for question in request.questions
        ]
        sink(
            {
                "type": "event",
                "kind": "decision_requested",
                "message": request.message,
                "data": {
                    "decision_id": decision_id,
                    "kind": request.kind,
                    "tool": request.data.get("tool"),
                    "arguments": request.data.get("arguments", {}),
                    "questions": questions,
                    "plan": request.data.get("plan"),
                    "goal": request.data.get("goal"),
                    "steps": request.data.get("steps", []),
                    "details": request.data.get("details"),
                    "skill": request.data.get("skill"),
                    "description": request.data.get("description"),
                    "project_id": request.data.get("project_id"),
                    "workspace_sha256": request.data.get("workspace_sha256"),
                    "tree_sha256": request.data.get("tree_sha256"),
                    "path": request.data.get("path"),
                },
            }
        )
        pending = registry.register(decision_id, owner_id)
        deadline = monotonic() + timeout
        while True:
            if pending.event.wait(timeout=min(0.1, max(0.0, deadline - monotonic()))):
                break
            if cancel_requested is not None and cancel_requested():
                registry.discard(decision_id)
                return InterruptDecision("cancel")
            if monotonic() >= deadline:
                registry.discard(decision_id)
                return InterruptDecision("cancel")
        if cancel_requested is not None and cancel_requested():
            registry.discard(decision_id)
            return InterruptDecision("cancel")
        choice = str(pending.result.get("choice", "cancel"))
        if request.kind == "tool":
            supplement = pending.result.get("supplement")
            if choice == "supplement":
                return InterruptDecision(
                    "supplement",
                    supplement=supplement if isinstance(supplement, str) and supplement else None,
                )
            return InterruptDecision(
                "continue" if choice == "continue" else "cancel",
                supplement=supplement if isinstance(supplement, str) and supplement else None,
            )
        if request.kind == "plan":
            if choice == "implement_clear_session":
                return InterruptDecision("implement_clear_session")
            return InterruptDecision("implement" if choice == "implement" else "cancel")
        if request.kind == "question":
            answers = pending.result.get("answers")
            return InterruptDecision("answer", answers=answers if isinstance(answers, dict) else None)
        if request.kind == "resume":
            return InterruptDecision("continue" if choice == "continue" else "back")
        if request.kind == "skill":
            return InterruptDecision("trust" if choice == "trust" else "skip")
        return InterruptDecision("cancel")
        return InterruptDecision("continue")

    return decide
