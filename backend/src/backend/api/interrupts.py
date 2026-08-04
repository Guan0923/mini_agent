"""Interrupt handlers and the run-time decision registry for interactive chat.

Auto-approve mirrors the offline policy used by the benchmark harness; the
interactive handler pauses a run and asks the client to decide, which the TUI
client resolves through ``POST /api/decisions``.
"""

from __future__ import annotations

import threading
import uuid
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


def make_interactive_interrupt(sink, *, timeout: float = 120.0, owner_id: str | None = None):
    """Build an interrupt handler that pauses the run and asks the client."""

    def decide(request: InterruptRequest) -> InterruptDecision:
        decision_id = f"dec_{uuid.uuid4().hex}"
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
                    "questions": [{"id": q.id, "question": q.question} for q in request.questions],
                },
            }
        )
        pending = registry.register(decision_id, owner_id)
        if not pending.event.wait(timeout=timeout):
            registry.discard(decision_id)
            return InterruptDecision("cancel")
        choice = str(pending.result.get("choice", "cancel"))
        if request.kind == "tool":
            supplement = pending.result.get("supplement")
            return InterruptDecision(
                "continue" if choice == "continue" else "cancel",
                supplement=supplement if isinstance(supplement, str) and supplement else None,
            )
        if request.kind == "plan":
            return InterruptDecision("implement" if choice == "implement" else "cancel")
        if request.kind == "question":
            answers = pending.result.get("answers")
            return InterruptDecision("answer", answers=answers if isinstance(answers, dict) else None)
        return InterruptDecision("continue")

    return decide
