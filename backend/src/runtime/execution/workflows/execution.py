"""Default execution workflow."""

from __future__ import annotations

from backend.domain import PlanningError
from backend.planning import PlannerCapabilities

from ...conversation.steering import collect_steering, consume_steering
from ...core.context import AgentRuntime
from ...core.contracts import WorkflowModeChanged
from ..lifecycle.cancellation import cancel_if_requested
from ..lifecycle.outcomes import cancel_run, complete_run, fail_run, planning_failure_data, record_plan_feedback
from ..steps import USER_DENIED_BATCH_FAILURE_CODE, ToolStepExecutor
from .budgets import _claim_model_turn, _ensure_tool_budget, _reject_over_budget_tools, _tool_batch_fits
from .common import (
    _apply_tool_batch_steering,
    _execute_tool,
    _fail_pending_tools,
    _finish_assistant,
    _model_text_stream,
    _publish_assistant_message,
    _publish_repairs,
    _publish_tool_recovery,
    _start_assistant,
    _truncate,
)


class ExecutionWorkflow:
    def __init__(self) -> None:
        self._steps = ToolStepExecutor()

    def run(self, runtime: AgentRuntime):
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        planner = capabilities.decision_planner
        if planner is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support decisions.")
            return runtime.run
        while True:
            runtime.apply_pending_runtime_config()
            if runtime.run.mode != "agent":
                raise WorkflowModeChanged("Agent workflow changed to Plan mode.")
            if cancel_if_requested(runtime):
                return runtime.run
            if not _ensure_tool_budget(runtime):
                return runtime.run
            if not _claim_model_turn(runtime, "decision"):
                return runtime.run
            close = _model_text_stream(runtime, stream_content=True)
            try:
                response = planner.decide(runtime)
            except PlanningError as exc:
                close()
                _publish_repairs(runtime, capabilities)
                if cancel_if_requested(runtime):
                    return runtime.run
                fail_run(runtime, f"Decision failed: {exc}", **planning_failure_data(exc, capabilities.name))
                return runtime.run
            except BaseException:
                close()
                raise
            else:
                streamed = close()
            _publish_repairs(runtime, capabilities)
            _publish_assistant_message(runtime, response, streamed)

            if cancel_if_requested(runtime):
                _fail_pending_tools(runtime, response, "Not executed because the run was cancelled.")
                return runtime.run

            if consume_steering(runtime, phase="after_model_response") is not None:
                _fail_pending_tools(runtime, response, "Not executed because the user supplied new instructions.")
                continue

            if not response.tool_messages:
                complete_run(runtime, response, response_streamed=streamed.content)
                return runtime.run
            if not _tool_batch_fits(runtime, response):
                _reject_over_budget_tools(runtime, response)
                return runtime.run

            _start_assistant(runtime, response)
            steered = False
            denied = False
            for index, tool in enumerate(response.tool_messages):
                if cancel_if_requested(runtime):
                    return runtime.run
                update = collect_steering(runtime)
                if update is not None:
                    _apply_tool_batch_steering(
                        runtime,
                        update,
                        next_tool_index=index,
                        phase="before_tool",
                    )
                    steered = True
                    break
                try:
                    outcome = _execute_tool(runtime, index, self._steps)
                except WorkflowModeChanged:
                    _fail_pending_tools(runtime, response, "Not executed because the workflow mode changed.")
                    _finish_assistant(runtime)
                    raise
                if cancel_if_requested(runtime):
                    return runtime.run
                if outcome.interrupt is not None:
                    if outcome.interrupt.choice == "deny":
                        _fail_pending_tools(
                            runtime,
                            response,
                            "Not executed because tool execution was interrupted.",
                            failure_code=USER_DENIED_BATCH_FAILURE_CODE,
                        )
                        _finish_assistant(runtime)
                        denied = True
                        break
                    _fail_pending_tools(runtime, response, "Not executed because tool execution was interrupted.")
                    runtime.state.active_message = None
                    runtime.state.active_tool_index = None
                    if outcome.interrupt.choice == "cancel":
                        cancel_run(runtime)
                    elif record_plan_feedback(runtime, outcome.interrupt.supplement) is None:
                        pass
                    return runtime.run
                update = collect_steering(runtime)
                if update is not None:
                    _apply_tool_batch_steering(
                        runtime,
                        update,
                        next_tool_index=index + 1,
                        phase="after_tool",
                    )
                    steered = True
                    break
                if outcome.success:
                    continue
                error = outcome.error or "Tool execution failed without an error message."
                tool.content = f"{tool.name} failed: {_truncate(error)}"
                _publish_tool_recovery(runtime, tool, error)
            if steered or denied:
                continue
            _finish_assistant(runtime)
