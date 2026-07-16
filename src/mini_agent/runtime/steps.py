"""Tool-step execution policy driven entirely by AgentRuntime."""

from __future__ import annotations

from dataclasses import dataclass

from mini_agent.tools import ToolError

from .context import AgentRuntime
from .contracts import InterruptDecision, InterruptRequest
from .events import RuntimeEvent


@dataclass(frozen=True)
class ToolStepResult:
    success: bool
    output: str | None = None
    error: str | None = None
    interrupt: InterruptDecision | None = None
    retryable: bool | None = None


class ToolStepExecutor:
    """Execute runtime.state.active_message.tool_messages[active_tool_index]."""

    def execute(self, runtime: AgentRuntime) -> ToolStepResult:
        run = runtime.run
        message = runtime.state.active_message
        index = runtime.state.active_tool_index
        if message is None or index is None or not 0 <= index < len(message.tool_messages):
            return self._failure(runtime, "unknown", "Runtime does not identify an active tool call.")
        tool_message = message.tool_messages[index]
        tool = tool_message.name
        tools = runtime.services.tools
        publish = runtime.services.publish or (lambda _event: None)
        try:
            if run.mode == "plan" and not tools.is_read_only(tool):
                return self._failure(runtime, tool, f"Read-only Plan mode blocked tool: {tool}")
            requires_confirmation = tools.requires_confirmation(tool)
            retryable = tools.is_retryable(tool)
        except ToolError as exc:
            return self._failure(runtime, tool, str(exc))

        if requires_confirmation:
            request = InterruptRequest(
                "tool",
                f"Call tool {tool}?",
                {"run_id": run.run_id, "tool": tool, "arguments": tool_message.arguments},
            )
            run.add_event("approval_requested", "Tool approval requested", interrupt_kind="tool", **request.data)
            publish(RuntimeEvent("approval_requested", request.message, request.data))
            if runtime.services.interrupt is None:
                return ToolStepResult(success=False, interrupt=InterruptDecision("cancel"))
            decision = runtime.services.interrupt(request)
            if decision.choice != "continue":
                return ToolStepResult(success=False, interrupt=decision)
            run.add_event("approval_granted", "Tool approval granted", interrupt_kind="tool", **request.data)
            publish(RuntimeEvent("approval_granted", request.message, request.data))

        run.add_event("tool_call", f"Calling {tool}", arguments=dict(tool_message.arguments))
        publish(RuntimeEvent("tool_call", tool, {"arguments": tool_message.arguments}))
        for attempt in range(runtime.state.runner_settings.max_retries + 1):
            try:
                result = tools.invoke(tool, tool_message.arguments, confirmed=True)
                tool_message.status = "succeeded"
                tool_message.content = result
                tool_message.retryable = retryable
                run.completed_steps.append(len(run.actions))
                run.add_event("tool_result", f"{tool} succeeded", result=result)
                publish(RuntimeEvent("tool_result", result, {"tool": tool}))
                runtime.save()
                return ToolStepResult(success=True, output=result, retryable=retryable)
            except ToolError as exc:
                if attempt < runtime.state.runner_settings.max_retries and retryable:
                    run.add_event("retry", f"Retrying {tool}", error=str(exc), attempt=attempt + 1)
                    publish(RuntimeEvent("retry", str(exc), {"tool": tool, "attempt": attempt + 1}))
                    continue
                return self._failure(runtime, tool, str(exc), retryable=retryable)
        return self._failure(runtime, tool, "Tool execution ended without an outcome.", retryable=retryable)

    @staticmethod
    def _failure(
        runtime: AgentRuntime,
        tool: str,
        error: str,
        *,
        retryable: bool | None = None,
    ) -> ToolStepResult:
        run = runtime.run
        message = runtime.state.active_message
        index = runtime.state.active_tool_index
        if message is not None and index is not None and 0 <= index < len(message.tool_messages):
            current = message.tool_messages[index]
            current.status = "failed"
            current.content = error
            current.retryable = retryable
        run.add_event("tool_failed", f"{tool} failed", error=error)
        publish = runtime.services.publish or (lambda _event: None)
        publish(RuntimeEvent("tool_failed", error, {"tool": tool}))
        runtime.save()
        return ToolStepResult(success=False, error=error, retryable=retryable)
