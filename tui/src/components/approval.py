"""Terminal implementation of the runtime Human-in-the-Loop boundary."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Literal

from backend.runtime.conversation.user_input import OTHER_OPTION_LABEL
from backend.runtime.core.contracts import InterruptDecision, InterruptRequest, UserQuestion

from .tool_review import format_tool_review

PermissionMode = Literal["read_only", "workspace_write", "full_access"]


def _console_write(text: str, end: str = "\n") -> None:
    print(text, end=end)


class TerminalApproval:
    """Render a plan or tool request and collect one explicit human decision."""

    def __init__(
        self,
        permission_mode: PermissionMode = "read_only",
        write: Callable[[str, str], None] | None = None,
    ) -> None:
        self._permission_mode = permission_mode
        self._write = write or _console_write
        self._request_lock = Lock()

    @property
    def permission_mode(self) -> PermissionMode:
        """Return the in-memory permission mode for this terminal process."""

        return self._permission_mode

    def configure_permission(self) -> None:
        """Let the user choose how this terminal responds to tool approvals."""

        while True:
            self.render_permission()
            choice = input("Choose 1, 2, or 3: ").strip().lower()
            mode = self.parse_permission(choice)
            if mode is not None:
                self.set_permission(mode)
                return
            self._write("Choose 1, 2, or 3.")

    def render_permission(self) -> None:
        self._write("\nPERMISSION")
        self._write(f"Current: {self._permission_label(self._permission_mode)}")
        self._write("[1] Read only — Permit reads; ask before writes or dangerous tools.")
        self._write("[2] Workspace write — Permit workspace writes; keep dangerous approvals manual.")
        self._write(
            "[3] Full access — Unsandboxed access after explicit confirmation; dangerous approvals remain manual."
        )

    @staticmethod
    def parse_permission(value: str) -> PermissionMode | None:
        choice = value.strip().lower()
        if choice in {"1", "read", "read only", "read_only"}:
            return "read_only"
        if choice in {"2", "write", "workspace write", "workspace_write"}:
            return "workspace_write"
        if choice in {"3", "full", "full access", "full_access"}:
            return "full_access"
        return None

    def set_permission(self, mode: PermissionMode, *, announce: bool = True) -> None:
        self._permission_mode = mode
        if announce:
            self._write(f"PERMISSION SET — {self._permission_label(mode)}")

    def notify(self, message: str) -> None:
        self._write(message)

    def __call__(self, request: InterruptRequest) -> InterruptDecision:
        automatic = self.automatic_decision(request)
        if automatic is not None:
            return automatic
        with self._request_lock:
            self.render_request(request)
            if request.kind == "question":
                return self._read_question_decision(request.questions)
            return self._read_decision(request)

    def automatic_decision(self, request: InterruptRequest) -> InterruptDecision | None:
        return None

    def render_request(self, request: InterruptRequest) -> None:
        if request.kind == "question":
            self._write("\nPLAN QUESTIONS")
            for question_index, question in enumerate(request.questions, start=1):
                self._write(f"\n{question_index}/{len(request.questions)} {question.header}\n{question.question}")
                for option_index, option in enumerate(question.options, start=1):
                    self._write(f"[{option_index}] {option.label} - {option.description}")
                self._write(f"[{len(question.options) + 1}] {OTHER_OPTION_LABEL}")
            return
        if request.kind == "plan":
            proposal = request.data.get("plan")
            if isinstance(proposal, str):
                self._write(f"\nPLAN REVIEW\n{proposal}")
            else:
                self._write(f"\nPLAN REVIEW\nGoal: {request.data['goal']}")
                for index, step in enumerate(request.data["steps"], start=1):
                    self._write(f"{index}. {step}")
            return

        if request.kind == "resume":
            self._write(f"\nRESUME WORKFLOW\n{request.message}")
            details = request.data.get("details")
            if isinstance(details, str):
                self._write(details)
            return

        self._write(f"\nTOOL REVIEW\n{format_tool_review(request).plain()}")

    @staticmethod
    def input_prompt(request: InterruptRequest, *, supplement: bool = False) -> str:
        if supplement:
            return "Supplement: "
        if request.kind == "plan":
            return "[1] Implement  [2] Implement after Compaction  [3] Stay in Plan mode: "
        if request.kind == "resume":
            return "[1] Continue  [2] Back: "
        return "[1] Continue  [2] Cancel  [3] Supplement: "

    @staticmethod
    def parse_input(
        request: InterruptRequest,
        value: str,
        *,
        supplement: bool = False,
    ) -> tuple[InterruptDecision | None, bool]:
        choice = value.strip().lower()
        if supplement:
            return (InterruptDecision("supplement", value.strip()), False) if value.strip() else (None, True)
        if request.kind == "plan":
            if choice in {"1", "implement"}:
                return InterruptDecision("implement"), False
            if choice in {
                "2",
                "implement after compaction",
                "implement_and_compaction",
            }:
                return InterruptDecision("implement_and_compaction"), False
            if choice in {"3", "stay", "stay in plan mode", "stay_in_plan_mode"}:
                return InterruptDecision("stay_in_plan_mode"), False
            return None, False
        if request.kind == "resume":
            if choice in {"1", "continue"}:
                return InterruptDecision("continue"), False
            if choice in {"2", "back"}:
                return InterruptDecision("back"), False
            return None, False
        if choice in {"1", "continue"}:
            return InterruptDecision("continue"), False
        if choice in {"2", "cancel"}:
            return InterruptDecision("cancel"), False
        if choice in {"3", "supplement"}:
            return None, True
        return None, False

    def _read_decision(self, request: InterruptRequest) -> InterruptDecision:
        supplement = False
        while True:
            value = input(self.input_prompt(request, supplement=supplement))
            decision, next_supplement = self.parse_input(request, value, supplement=supplement)
            if decision is not None:
                return decision
            if supplement and next_supplement:
                self._write("Supplement cannot be empty.")
            elif not next_supplement:
                self._write("Choose 1, 2, or 3.")
            supplement = next_supplement

    def _read_question_decision(self, questions: tuple[UserQuestion, ...]) -> InterruptDecision:
        answers: dict[str, list[str]] = {}
        for question_index, question in enumerate(questions, start=1):
            other_index = len(question.options) + 1
            while True:
                raw = input(f"Question {question_index}/{len(questions)} - choose 1 to {other_index}: ").strip()
                try:
                    selected = int(raw)
                except ValueError:
                    selected = 0
                if 1 <= selected <= len(question.options):
                    answers[question.id] = [question.options[selected - 1].label]
                    break
                if selected == other_index:
                    while True:
                        custom = input("Your answer: ").strip()
                        if custom:
                            answers[question.id] = [custom]
                            break
                        self._write("Answer cannot be empty.")
                    break
                self._write(f"Choose 1 to {other_index}.")
        return InterruptDecision("answer", answers=answers)

    @staticmethod
    def _permission_label(mode: PermissionMode) -> str:
        return mode.replace("_", " ").title()

    @staticmethod
    def _read_plan_decision() -> InterruptDecision:
        request = InterruptRequest("plan", "", {})
        return TerminalApproval()._read_decision(request)

    @staticmethod
    def _read_tool_decision() -> InterruptDecision:
        request = InterruptRequest("tool", "", {})
        return TerminalApproval()._read_decision(request)
