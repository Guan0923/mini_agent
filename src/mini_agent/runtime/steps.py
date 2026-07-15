"""Tool-step execution policy: Human approval, permissions, retries, and events."""

from __future__ import annotations

from dataclasses import dataclass

from mini_agent.domain import AgentAction, RunState
from mini_agent.tools import ToolError, ToolExecutor

from .contracts import EventHandler, InterruptDecision, InterruptHandler, InterruptRequest
from .events import RuntimeEvent


@dataclass(frozen=True)
class ToolStepResult:
    """The execution outcome consumed by a workflow-specific failure policy."""

    success: bool
    output: str | None = None
    error: str | None = None
    interrupt: InterruptDecision | None = None


class ToolStepExecutor:
    """Executes a single validated tool action without knowing its workflow."""

    def __init__(self, tools: ToolExecutor, max_retries: int) -> None:
        self._tools = tools
        self._max_retries = max_retries

    def execute(
        self,
        state: RunState,
        action: AgentAction,
        publish: EventHandler,
        interrupt: InterruptHandler,
    ) -> ToolStepResult:
        if action.type != "tool_call" or not action.tool:
            return self._failure(state, publish, "unknown", "Only tool-call actions may be executed as a runtime step.")
        tool = action.tool
        request = InterruptRequest(
            "tool",
            f"Call tool {tool}?",
            {"run_id": state.run_id, "tool": tool, "arguments": action.arguments},
        )
        state.add_event("approval_requested", "Tool approval requested", interrupt_kind="tool", **request.data)
        publish(RuntimeEvent("approval_requested", request.message, request.data))
        decision = interrupt(request)
        if decision.choice != "continue":
            return ToolStepResult(success=False, interrupt=decision)
        state.add_event("approval_granted", "Tool approval granted", interrupt_kind="tool", **request.data)
        publish(RuntimeEvent("approval_granted", request.message, request.data))
        try:
            if state.mode == "plan" and not self._tools.is_read_only(tool):
                return self._failure(state, publish, tool, f"Read-only Plan mode blocked tool: {tool}")
        except ToolError as exc:
            return self._failure(state, publish, tool, str(exc))
        state.add_event("tool_call", f"Calling {tool}", **action.arguments)
        publish(RuntimeEvent("tool_call", tool, {"arguments": action.arguments}))
        for attempt in range(self._max_retries + 1):
            try:
                result = self._invoke(tool, action.arguments)
                state.completed_steps.append(len(state.actions))
                state.add_event("tool_result", f"{tool} succeeded", result=result)
                publish(RuntimeEvent("tool_result", result, {"tool": tool}))
                return ToolStepResult(success=True, output=result)
            except ToolError as exc:
                if attempt < self._max_retries:
                    state.add_event("retry", f"Retrying {tool}", error=str(exc), attempt=attempt + 1)
                    publish(RuntimeEvent("retry", str(exc), {"tool": tool, "attempt": attempt + 1}))
                    continue
                return self._failure(state, publish, tool, str(exc))
        return self._failure(state, publish, tool, "Tool execution ended without an outcome.")

    @staticmethod
    def _failure(state: RunState, publish: EventHandler, tool: str, error: str) -> ToolStepResult:
        state.add_event("tool_failed", f"{tool} failed", error=error)
        publish(RuntimeEvent("tool_failed", error, {"tool": tool}))
        return ToolStepResult(success=False, error=error)

    def _invoke(self, tool: str, arguments: dict[str, object]) -> str:
        """The preceding Human-in-the-Loop approval is the runtime confirmation."""
        return self._tools.invoke(tool, arguments, confirmed=True)
