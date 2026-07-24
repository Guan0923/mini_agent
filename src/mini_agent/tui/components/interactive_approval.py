"""Bridge blocking runtime approvals onto the asynchronous Textual input loop."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

from mini_agent.runtime.core.contracts import InterruptDecision, InterruptRequest

from ..view import TerminalView
from ..widgets import ChoiceItem
from .approval import TerminalApproval
from .tool_review import format_tool_review


class InteractiveApproval:
    """Move blocking runtime approvals onto the active Textual input loop."""

    def __init__(
        self,
        approval: TerminalApproval,
        loop: asyncio.AbstractEventLoop,
        view: TerminalView | None = None,
    ) -> None:
        self._approval = approval
        self._loop = loop
        self._view = view
        self._pending: tuple[InterruptRequest, Future[InterruptDecision]] | None = None
        self._supplement = False
        self.changed = asyncio.Event()

    @property
    def pending(self) -> bool:
        return self._pending is not None

    @property
    def prompt(self) -> str:
        if self._pending is None:
            return "mini-agent[review]> "
        return self._approval.input_prompt(self._pending[0], supplement=self._supplement)

    @property
    def status(self) -> str:
        if self._pending is None:
            return "REVIEW"
        request = self._pending[0]
        if self._supplement:
            return "TOOL REVIEW | Enter supplement"
        if request.kind == "question":
            return "PLAN QUESTIONS | Select answers"
        if request.kind == "plan":
            return "PLAN REVIEW | Select action"
        if request.kind == "resume":
            return "RESUME | Select action"
        return "TOOL REVIEW | Select action"

    def __call__(self, request: InterruptRequest) -> InterruptDecision:
        automatic = self._approval.automatic_decision(request)
        if automatic is not None:
            return automatic
        decision: Future[InterruptDecision] = Future()
        self._loop.call_soon_threadsafe(self._open, request, decision)
        return decision.result()

    def submit(self, value: str) -> None:
        if self._pending is None:
            return
        request, future = self._pending
        decision, wants_supplement = self._approval.parse_input(
            request,
            value,
            supplement=self._supplement,
        )
        if decision is not None:
            self._pending = None
            self._supplement = False
            future.set_result(decision)
            self.changed.set()
            return
        if self._supplement and wants_supplement:
            self._approval.notify("Supplement cannot be empty.")
        elif not wants_supplement:
            self._approval.notify("Choose 1, 2, or 3.")
        self._supplement = wants_supplement

    def _open(self, request: InterruptRequest, decision: Future[InterruptDecision]) -> None:
        if self._pending is not None:
            decision.set_exception(RuntimeError("Only one terminal approval can be pending at a time."))
            return
        self._pending = (request, decision)
        self._supplement = False
        if (
            self._view is None
            or request.kind == "question"
            and not callable(getattr(self._view, "begin_questionnaire", None))
            or request.kind in {"plan", "tool", "resume"}
            and not callable(getattr(self._view, "begin_review", None))
        ):
            self._approval.render_request(request)
        elif request.kind == "question":
            self._view.begin_questionnaire(request.questions, self._complete_questionnaire)
        elif request.kind == "plan":
            self._view.begin_review(
                "PLAN REVIEW",
                request.message,
                self._plan_details(request),
                (
                    ChoiceItem("implement", "Implement", "Implement in the current session."),
                    ChoiceItem(
                        "implement_clear_session",
                        "Implement and Clear Session",
                        "Start implementation in a new session.",
                    ),
                    ChoiceItem("cancel", "Cancel and Stay in plan mode", "Do not implement this plan."),
                ),
                self._complete_review,
            )
        elif request.kind == "resume":
            self._view.begin_review(
                "RESUME WORKFLOW",
                request.message,
                str(request.data.get("details") or ""),
                (
                    ChoiceItem("continue", "Continue", "Create a new attempt from the durable checkpoint."),
                    ChoiceItem("terminate", "Terminate", "Close this workflow without executing more work."),
                    ChoiceItem("back", "Back", "Leave the current session unchanged."),
                ),
                self._complete_review,
            )
        else:
            self._view.begin_review(
                "TOOL REVIEW",
                request.message,
                self._tool_details(request),
                (
                    ChoiceItem("continue", "Continue", "Run this tool call."),
                    ChoiceItem("cancel", "Cancel", "Stop the current run."),
                    ChoiceItem("supplement", "Supplement", "Send additional instructions.", custom=True),
                ),
                self._complete_review,
            )
        self.changed.set()

    @staticmethod
    def _plan_details(request: InterruptRequest) -> str:
        plan = request.data.get("plan")
        if isinstance(plan, str):
            return plan
        goal = request.data.get("goal", "")
        steps = request.data.get("steps", ())
        rendered = [f"**Goal:** {goal}"] if goal else []
        if isinstance(steps, list):
            rendered.extend(f"{index}. {step}" for index, step in enumerate(steps, start=1))
        return "\n\n".join(rendered)

    @staticmethod
    def _tool_details(request: InterruptRequest) -> str:
        return format_tool_review(request).markdown()

    def _complete_questionnaire(self, answers: dict[str, list[str]]) -> None:
        if self._pending is None or self._pending[0].kind != "question":
            return
        request, future = self._pending
        self._pending = None
        self._supplement = False
        future.set_result(InterruptDecision("answer", answers=answers))
        self.changed.set()

    def _complete_review(self, choice: str, supplement: str | None) -> None:
        if self._pending is None or self._pending[0].kind not in {"plan", "tool", "resume"}:
            return
        request, future = self._pending
        if request.kind == "plan":
            allowed = {"implement", "implement_clear_session", "cancel"}
        elif request.kind == "resume":
            allowed = {"continue", "terminate", "back"}
        else:
            allowed = {"continue", "cancel", "supplement"}
        self._pending = None
        self._supplement = False
        if choice not in allowed:
            future.set_exception(ValueError(f"Invalid {request.kind} review choice: {choice}"))
        else:
            future.set_result(InterruptDecision(choice, supplement=supplement))
        self.changed.set()

    def cancel_pending(self) -> None:
        """Resolve an active review so cooperative run cancellation can continue."""

        if self._pending is None:
            return
        request, future = self._pending
        self._pending = None
        self._supplement = False
        cancel_prompt = getattr(self._view, "cancel_choice_prompt", None)
        if callable(cancel_prompt):
            cancel_prompt()
        future.set_result(InterruptDecision("back" if request.kind == "resume" else "cancel"))
        self.changed.set()
