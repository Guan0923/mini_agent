"""Terminal implementation of the runtime Human-in-the-Loop boundary."""

from __future__ import annotations

from mini_agent.runtime.contracts import InterruptDecision, InterruptRequest


class TerminalApproval:
    """Render a plan or tool request and collect one explicit human decision."""

    def __call__(self, request: InterruptRequest) -> InterruptDecision:
        if request.kind == "plan":
            print(f"\nPLAN REVIEW\nGoal: {request.data['goal']}")
            for index, step in enumerate(request.data["steps"], start=1):
                print(f"{index}. {step}")
        else:
            print(f"\nTOOL REVIEW\n{request.data['tool']} {request.data['arguments']}")
        return self._read_decision()

    @staticmethod
    def _read_decision() -> InterruptDecision:
        while True:
            choice = input("[1] Continue  [2] Cancel  [3] Supplement: ").strip().lower()
            if choice in {"1", "continue"}:
                return InterruptDecision("continue")
            if choice in {"2", "cancel"}:
                return InterruptDecision("cancel")
            if choice in {"3", "supplement"}:
                supplement = input("Supplement: ").strip()
                if supplement:
                    return InterruptDecision("supplement", supplement)
                print("Supplement cannot be empty.")
                continue
            print("Choose 1, 2, or 3.")
