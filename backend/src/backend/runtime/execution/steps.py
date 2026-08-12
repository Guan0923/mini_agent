"""Tool-step execution policy driven entirely by AgentRuntime."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from backend.tools import ToolError, ToolInvocationContext

from ..core.context import AgentRuntime
from ..core.contracts import InterruptDecision, InterruptRequest
from ..core.events import RuntimeEvent
from ..core.hooks import (
    HookCancellation,
    HookOutcome,
    RunHookInfo,
    ToolHookContext,
    ToolHookResult,
)


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

        context = ToolHookContext(
            run=RunHookInfo(runtime.state.session_id, run.run_id, run.task, run.mode),
            call_id=tool_message.call_id,
            name=tool,
            arguments=dict(tool_message.arguments),
        )
        try:
            return runtime.services.hooks.run_tool(
                context,
                lambda hook_context: self._invoke(
                    runtime,
                    hook_context,
                    requires_confirmation=requires_confirmation,
                    retryable=retryable,
                ),
                self._hook_outcome,
                publish,
            )
        except HookCancellation as exc:
            tool_message.status = "failed"
            tool_message.content = exc.reason
            tool_message.retryable = False
            run.add_event("tool_failed", f"{tool} failed", call_id=tool_message.call_id, error=exc.reason)
            publish(RuntimeEvent("tool_failed", exc.reason, {"tool": tool, "call_id": tool_message.call_id}))
            runtime.save()
            return ToolStepResult(
                success=False,
                error=exc.reason,
                interrupt=InterruptDecision("cancel"),
                retryable=False,
            )

    def _invoke(
        self,
        runtime: AgentRuntime,
        context: ToolHookContext,
        *,
        requires_confirmation: bool,
        retryable: bool,
    ) -> ToolStepResult:
        run = runtime.run
        message = runtime.state.active_message
        index = runtime.state.active_tool_index
        assert message is not None and index is not None
        tool_message = message.tool_messages[index]
        tool = tool_message.name
        tools = runtime.services.tools
        publish = runtime.services.publish or (lambda _event: None)
        tool_message.arguments = dict(context.arguments)
        validate = getattr(tools, "validate_arguments", None)
        if callable(validate):
            try:
                validate(tool, tool_message.arguments)
            except ToolError as exc:
                return self._failure(runtime, tool, str(exc), retryable=retryable)

        if requires_confirmation:
            request = InterruptRequest(
                "tool",
                f"Call tool {tool}?",
                {
                    "run_id": run.run_id,
                    "tool": tool,
                    "call_id": tool_message.call_id,
                    "arguments": tool_message.arguments,
                },
            )
            run.add_event("approval_requested", "Tool approval requested", interrupt_kind="tool", **request.data)
            publish(RuntimeEvent("approval_requested", request.message, request.data))
            if runtime.services.interrupt is None:
                failure = self._failure(
                    runtime, tool, "Tool approval cancelled because no interrupt handler is available."
                )
                return ToolStepResult(
                    success=False,
                    error=failure.error,
                    interrupt=InterruptDecision("cancel"),
                    retryable=failure.retryable,
                )
            decision = runtime.services.interrupt(request)
            if decision.choice != "continue":
                failure = self._failure(runtime, tool, "Tool approval was not granted.")
                return ToolStepResult(
                    success=False,
                    error=failure.error,
                    interrupt=decision,
                    retryable=failure.retryable,
                )
            run.add_event("approval_granted", "Tool approval granted", interrupt_kind="tool", **request.data)
            publish(RuntimeEvent("approval_granted", request.message, request.data))

        started_at = perf_counter()
        run.add_event(
            "tool_call", f"Calling {tool}", call_id=tool_message.call_id, arguments=dict(tool_message.arguments)
        )
        publish(
            RuntimeEvent(
                "tool_call",
                tool,
                {
                    "call_id": tool_message.call_id,
                    "arguments": tool_message.arguments,
                    "attempt": 1,
                    "started_at": run.events[-1].timestamp,
                },
            )
        )
        try:
            subagents = runtime.services.subagents
            if subagents is not None and subagents.handles(tool):
                result = subagents.invoke(runtime, tool, tool_message.arguments)
            else:
                invoke_with_context = getattr(tools, "invoke_with_context", None)
                if callable(invoke_with_context):
                    result = invoke_with_context(
                        tool,
                        tool_message.arguments,
                        ToolInvocationContext(runtime.state.session_id, runtime.state.timezone, runtime.services.clock),
                        confirmed=True,
                    )
                else:
                    result = tools.invoke(tool, tool_message.arguments, confirmed=True)
            tool_message.status = "succeeded"
            tool_message.content = result
            tool_message.retryable = retryable
            run.completed_steps.append(len(run.actions))
            duration_ms = round((perf_counter() - started_at) * 1000, 3)
            run.add_event(
                "tool_result",
                f"{tool} succeeded",
                call_id=tool_message.call_id,
                result=result,
                duration_ms=duration_ms,
                attempts=1,
            )
            publish(
                RuntimeEvent(
                    "tool_result",
                    result,
                    {"tool": tool, "call_id": tool_message.call_id, "duration_ms": duration_ms, "attempts": 1},
                )
            )
            runtime.save()
            return ToolStepResult(success=True, output=result, retryable=retryable)
        except ToolError as exc:
            return self._failure(
                runtime,
                tool,
                str(exc),
                retryable=retryable,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
        except Exception as exc:
            # A tool failure is returned to the planner so it can select a
            # different action; it must not abort the surrounding run.
            return self._failure(
                runtime,
                tool,
                f"Unexpected tool failure: {type(exc).__name__}: {exc}",
                retryable=retryable,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )

    @staticmethod
    def _hook_outcome(result: ToolStepResult) -> HookOutcome[ToolHookResult]:
        if result.interrupt is not None:
            status = "cancelled"
        else:
            status = "succeeded" if result.success else "failed"
        return HookOutcome(
            status=status,
            result=ToolHookResult(
                result.success,
                result.output,
                result.error,
                result.retryable,
            ),
        )

    @staticmethod
    def _failure(
        runtime: AgentRuntime,
        tool: str,
        error: str,
        *,
        retryable: bool | None = None,
        duration_ms: float | None = None,
    ) -> ToolStepResult:
        run = runtime.run
        message = runtime.state.active_message
        index = runtime.state.active_tool_index
        call_id = ""
        if message is not None and index is not None and 0 <= index < len(message.tool_messages):
            current = message.tool_messages[index]
            call_id = current.call_id
            current.status = "failed"
            current.content = error
            current.retryable = retryable
        data: dict[str, object] = {"call_id": call_id, "error": error}
        if duration_ms is not None:
            data["duration_ms"] = duration_ms
        run.add_event("tool_failed", f"{tool} failed", **data)
        publish = runtime.services.publish or (lambda _event: None)
        publish(RuntimeEvent("tool_failed", error, {"tool": tool, **data}))
        runtime.save()
        return ToolStepResult(success=False, error=error, retryable=retryable)
