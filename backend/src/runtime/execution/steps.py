"""Tool-step execution policy driven entirely by AgentRuntime."""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import perf_counter

from backend.domain import safe_error_message
from backend.sandbox import SandboxExecutionDecision
from backend.tools import ToolError, ToolInvocationContext

from ..core.context import AgentRuntime
from ..core.contracts import InterruptDecision, WorkflowModeChanged
from ..core.events import RuntimeEvent
from ..core.hooks import (
    HookOutcome,
    RunHookInfo,
    ToolHookContext,
    ToolHookResult,
    after_tool_hook_manager,
    before_tool_hook_manager,
)

USER_DENIED_FAILURE_CODE = "user_denied"
USER_DENIED_BATCH_FAILURE_CODE = "user_denied_batch"


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
        previous_mode = runtime.run.mode
        runtime.apply_pending_runtime_config()
        run = runtime.run
        if runtime.state.running_mode in {"agent", "plan"}:
            run.mode = runtime.state.running_mode  # type: ignore[assignment]
        if run.mode != previous_mode:
            raise WorkflowModeChanged(f"Workflow mode changed from {previous_mode} to {run.mode}.")
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
            read_only = tools.is_read_only(tool)
            workspace_confined = tools.is_workspace_confined(tool)
            retryable = tools.is_retryable(tool)
            validate = getattr(tools, "validate_arguments", None)
            if callable(validate):
                validate(tool, tool_message.arguments)
        except ToolError as exc:
            return self._failure(runtime, tool, safe_error_message(exc))

        context = ToolHookContext(
            run=RunHookInfo(runtime.state.session_id, run.run_id, run.task, run.mode),
            call_id=tool_message.call_id,
            name=tool,
            arguments=tool_message.arguments,
            workspace_root=runtime.state.workspace_root or "",
            project_cwd=runtime.state.project_cwd or "",
            permission_mode=runtime.state.permission_mode,
            requires_confirmation=requires_confirmation,
            read_only=read_only,
            workspace_confined=workspace_confined,
            sandbox_launcher=runtime.services.sandbox_launcher,
            sandbox_config=runtime.services.sandbox_config or {},
            sandbox_user_id=runtime.services.sandbox_user_id,
            interrupt=runtime.services.interrupt,
            record_event=lambda _kind, _message, _data: None,
            publish=publish,
        )
        before = before_tool_hook_manager.execute(context, publish)
        if before.decision == "reject":
            interrupt = before.data.get("interrupt")
            decision = interrupt if isinstance(interrupt, InterruptDecision) else InterruptDecision("cancel")
            if decision.choice == "deny":
                return self._denied(runtime, tool, decision)
            failure = self._failure(runtime, tool, before.reason or "Tool call rejected by hook.", retryable=False)
            return ToolStepResult(
                success=False,
                error=failure.error,
                interrupt=decision,
                retryable=False,
            )
        sandbox_data = before.data.get("sandbox_decision")
        sandbox_decision = sandbox_data if isinstance(sandbox_data, SandboxExecutionDecision) else None
        result = self._invoke(runtime, retryable=retryable, sandbox_decision=sandbox_decision)
        after_tool_hook_manager.execute(replace(context, outcome=self._hook_outcome(result)), publish)
        return result

    def _invoke(
        self,
        runtime: AgentRuntime,
        *,
        retryable: bool,
        sandbox_decision: SandboxExecutionDecision | None,
    ) -> ToolStepResult:
        run = runtime.run
        message = runtime.state.active_message
        index = runtime.state.active_tool_index
        assert message is not None and index is not None
        tool_message = message.tool_messages[index]
        tool = tool_message.name
        tools = runtime.services.tools
        publish = runtime.services.publish or (lambda _event: None)
        started_at = perf_counter()
        started_at_timestamp = runtime.services.clock()
        publish(
            RuntimeEvent(
                "tool_call",
                tool,
                {
                    "call_id": tool_message.call_id,
                    "arguments": tool_message.arguments,
                    "attempt": 1,
                    "started_at": started_at_timestamp,
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
                        ToolInvocationContext(
                            session_id=runtime.state.session_id,
                            timezone=runtime.state.timezone,
                            clock=runtime.services.clock,
                            job_scope=runtime.services.job_scope,
                            cancel_requested=runtime.stop_requested,
                            sandbox_decision=sandbox_decision,
                        ),
                        confirmed=True,
                    )
                else:
                    result = tools.invoke(tool, tool_message.arguments, confirmed=True)
            if runtime.stop_requested():
                raise ToolError("Tool invocation cancelled.")
            tool_message.status = "succeeded"
            tool_message.content = result
            tool_message.retryable = retryable
            run.completed_steps.append(len(run.actions))
            duration_ms = round((perf_counter() - started_at) * 1000, 3)
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
                safe_error_message(exc),
                retryable=retryable,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
        except Exception as exc:
            # A tool failure is returned to the planner so it can select a
            # different action; it must not abort the surrounding run.
            return self._failure(
                runtime,
                tool,
                safe_error_message(exc),
                retryable=retryable,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )

    @staticmethod
    def _hook_outcome(result: ToolStepResult) -> HookOutcome[ToolHookResult]:
        if result.interrupt is not None and result.interrupt.choice != "deny":
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
    def _denied(runtime: AgentRuntime, tool: str, decision: InterruptDecision) -> ToolStepResult:
        message = runtime.state.active_message
        index = runtime.state.active_tool_index
        assert message is not None and index is not None
        current = message.tool_messages[index]
        error = f"The user denied this {tool} tool call."
        current.status = "failed"
        current.content = error
        current.retryable = False
        current.failure_code = USER_DENIED_FAILURE_CODE
        data = {
            "tool": tool,
            "call_id": current.call_id,
            "error": error,
            "failure_code": USER_DENIED_FAILURE_CODE,
        }
        publish = runtime.services.publish or (lambda _event: None)
        publish(RuntimeEvent("tool_failed", error, data))
        runtime.save()
        return ToolStepResult(
            success=False,
            error=error,
            interrupt=decision,
            retryable=False,
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
        publish = runtime.services.publish or (lambda _event: None)
        publish(RuntimeEvent("tool_failed", error, {"tool": tool, **data}))
        runtime.save()
        return ToolStepResult(success=False, error=error, retryable=retryable)
