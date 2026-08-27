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
from backend.sandbox import ApprovalDecision, ApprovalStore


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
    def __init__(
        self,
        request_kind: str | None = None,
        approval_context: dict[str, str] | None = None,
        approval_store: ApprovalStore | None = None,
    ) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] = {}
        self.request_kind = request_kind
        self.approval_context = approval_context
        self.approval_store = approval_store


class DecisionRegistry:
    """Thread-safe store of decisions the run is currently waiting on."""

    def __init__(self, *, approval_store: ApprovalStore | None = None) -> None:
        self._pending: dict[str, _PendingDecision] = {}
        self._lock = threading.Lock()
        self.approval_store = approval_store or ApprovalStore()

    def register(
        self,
        decision_id: str,
        *,
        request_kind: str | None = None,
        approval_context: dict[str, str] | None = None,
        approval_store: ApprovalStore | None = None,
    ) -> _PendingDecision:
        pending = _PendingDecision(request_kind, approval_context, approval_store)
        with self._lock:
            self._pending[decision_id] = pending
        return pending

    def kind(self, decision_id: str) -> str | None:
        with self._lock:
            pending = self._pending.get(decision_id)
            return pending.request_kind if pending is not None else None

    def resolve(self, decision_id: str, decision: dict[str, Any]) -> bool:
        with self._lock:
            pending = self._pending.get(decision_id)
            if pending is None:
                return False
            self._pending.pop(decision_id, None)
        if pending is None:
            return False
        pending.result.update(decision)
        if pending.approval_context is not None and decision.get("choice") == ApprovalDecision.ALLOW_SESSION.value:
            context = pending.approval_context
            (pending.approval_store or self.approval_store).decide(
                session_id=context["session_id"],
                command=context["command"],
                cwd=context["cwd"],
                permission_target=context["permission_target"],
                network_target=context.get("network_target", ""),
                decision=ApprovalDecision.ALLOW_SESSION,
            )
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
    approval_store: ApprovalStore | None = None,
):
    """Build an interrupt handler that pauses the run and asks the client."""

    def decide(request: InterruptRequest) -> InterruptDecision:
        if request.kind == "tool" and auto_approve_tools:
            return InterruptDecision("continue")
        approval_context = _approval_context(request)
        active_approval_store = approval_store or registry.approval_store
        if approval_context is not None and active_approval_store.allowed(**approval_context):
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
                    "call_id": request.data.get("call_id"),
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
        pending = registry.register(
            decision_id,
            request_kind=request.kind,
            approval_context=approval_context,
            approval_store=active_approval_store,
        )
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
            if choice == "deny":
                return InterruptDecision("deny")
            return InterruptDecision(
                "continue" if choice in {"continue", "allow_once", "allow_session"} else "cancel",
                supplement=supplement if isinstance(supplement, str) and supplement else None,
            )
        if request.kind == "plan":
            if choice in {"implement", "implement_and_compaction", "stay_in_plan_mode"}:
                return InterruptDecision(choice)  # type: ignore[arg-type]
            return InterruptDecision("stay_in_plan_mode")
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


def _approval_context(request: InterruptRequest) -> dict[str, str] | None:
    if request.kind != "tool" or not isinstance(request.data, dict):
        return None
    values = request.data
    session_id = values.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    return {
        "session_id": session_id,
        "command": str(values.get("command") or values.get("tool") or "tool"),
        "cwd": str(values.get("cwd") or ""),
        "permission_target": str(values.get("permission_target") or "workspace_write"),
        "network_target": str(values.get("network_target") or ""),
    }
