"""Default non-interactive interrupt policy for agent execution."""

from __future__ import annotations

from backend.tools import ToolError

from ...core.context import AgentRuntime
from ...core.contracts import InterruptDecision, InterruptRequest


def default_interrupt(runtime: AgentRuntime):
    """Build the conservative approval policy used without a UI handler."""

    def decide(request: InterruptRequest) -> InterruptDecision:
        if request.kind == "plan":
            return InterruptDecision("cancel")
        if request.kind == "skill":
            # Without an interactive handler an untrusted project Skill must
            # never be activated: fail closed by skipping it.
            return InterruptDecision("skip")
        tool = request.data.get("tool")
        if not isinstance(tool, str):
            return InterruptDecision("cancel")
        try:
            requires_confirmation = runtime.services.tools.requires_confirmation(tool)
        except ToolError:
            return InterruptDecision("cancel")
        if not requires_confirmation:
            return InterruptDecision("continue")
        message = f"{tool} requires confirmation before an external or destructive operation."
        confirm = runtime.services.confirm
        return InterruptDecision("continue" if confirm is not None and confirm(message) else "cancel")

    return decide
