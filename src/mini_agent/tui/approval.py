"""Terminal implementation of the runtime Human-in-the-Loop boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from mini_agent.runtime.contracts import InterruptDecision, InterruptRequest

PermissionMode = Literal["approval_for_me", "full_access"]


def _console_write(text: str, end: str = "\n") -> None:
    print(text, end=end)


class TerminalApproval:
    """Render a plan or tool request and collect one explicit human decision."""

    def __init__(
        self,
        permission_mode: PermissionMode = "approval_for_me",
        write: Callable[[str, str], None] | None = None,
    ) -> None:
        self._permission_mode = permission_mode
        self._write = write or _console_write

    @property
    def permission_mode(self) -> PermissionMode:
        """Return the in-memory permission mode for this terminal process."""

        return self._permission_mode

    def configure_permission(self) -> None:
        """Let the user choose how this terminal responds to tool approvals."""

        while True:
            self.render_permission()
            choice = input("Choose 1 or 2: ").strip().lower()
            mode = self.parse_permission(choice)
            if mode is not None:
                self.set_permission(mode)
                return
            self._write("Choose 1 or 2.")

    def render_permission(self) -> None:
        self._write("\nPERMISSION")
        self._write(f"Current: {self._permission_label(self._permission_mode)}")
        self._write("[1] Approval for me — Ask before every tool that requires confirmation.")
        self._write("[2] Full access — Auto-approve tools; PLAN REVIEW always remains manual.")

    @staticmethod
    def parse_permission(value: str) -> PermissionMode | None:
        choice = value.strip().lower()
        if choice in {"1", "approval", "approval for me", "approval_for_me"}:
            return "approval_for_me"
        if choice in {"2", "full", "full access", "full_access"}:
            return "full_access"
        return None

    def set_permission(self, mode: PermissionMode) -> None:
        self._permission_mode = mode
        self._write(f"PERMISSION SET — {self._permission_label(mode)}")

    def notify(self, message: str) -> None:
        self._write(message)

    def __call__(self, request: InterruptRequest) -> InterruptDecision:
        automatic = self.automatic_decision(request)
        if automatic is not None:
            return automatic
        self.render_request(request)
        return self._read_decision(request)

    def automatic_decision(self, request: InterruptRequest) -> InterruptDecision | None:
        if request.kind == "tool" and self._permission_mode == "full_access":
            return InterruptDecision("continue")
        return None

    def render_request(self, request: InterruptRequest) -> None:
        if request.kind == "plan":
            proposal = request.data.get("plan")
            if isinstance(proposal, str):
                self._write(f"\nPLAN REVIEW\n{proposal}")
            else:
                self._write(f"\nPLAN REVIEW\nGoal: {request.data['goal']}")
                for index, step in enumerate(request.data["steps"], start=1):
                    self._write(f"{index}. {step}")
            artifact_path = request.data.get("artifact_path")
            if isinstance(artifact_path, str) and artifact_path:
                self._write(f"PLAN FILE {artifact_path}")
            return

        self._write(f"\nTOOL REVIEW\n{request.data['tool']} {request.data['arguments']}")

    @staticmethod
    def input_prompt(request: InterruptRequest, *, supplement: bool = False) -> str:
        if supplement:
            return "Supplement: "
        if request.kind == "plan":
            return "[1] Implement  [2] Implement and Clear Session  [3] Cancel and Stay in plan mode: "
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
                "implement clear",
                "implement_clear_session",
                "implement and clear session",
            }:
                return InterruptDecision("implement_clear_session"), False
            if choice in {"3", "cancel", "cancel and stay", "cancel and stay in plan mode"}:
                return InterruptDecision("cancel"), False
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

    @staticmethod
    def _permission_label(mode: PermissionMode) -> str:
        return "Full access" if mode == "full_access" else "Approval for me"

    @staticmethod
    def _read_plan_decision() -> InterruptDecision:
        request = InterruptRequest("plan", "", {})
        return TerminalApproval()._read_decision(request)

    @staticmethod
    def _read_tool_decision() -> InterruptDecision:
        request = InterruptRequest("tool", "", {})
        return TerminalApproval()._read_decision(request)
