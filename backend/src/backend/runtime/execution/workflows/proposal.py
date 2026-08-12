"""Interactive Plan-mode proposal workflow."""

from __future__ import annotations

from backend.domain import PlanningError
from backend.planning import PlannerCapabilities

from ...conversation.steering import collect_steering, consume_steering
from ...conversation.user_input import REQUEST_USER_INPUT_NAME
from ...core.context import AgentRuntime
from ...planning.review import REQUEST_PLAN_REVIEW_NAME
from ..lifecycle.cancellation import cancel_if_requested
from ..lifecycle.outcomes import cancel_run, fail_run, planning_failure_data, record_plan_feedback
from ..steps import ToolStepExecutor
from .budgets import _claim_model_turn, _ensure_tool_budget, _reject_over_budget_tools, _tool_batch_fits
from .common import (
    PlanProposalResult,
    _apply_tool_batch_steering,
    _execute_tool,
    _fail_pending_tools,
    _finish_assistant,
    _model_text_stream,
    _publish_assistant_message,
    _publish_repairs,
    _publish_tool_recovery,
    _record_reasoning,
    _start_assistant,
    _truncate,
)
from .controls import PlanControlMixin


class PlanProposalWorkflow(PlanControlMixin):
    def __init__(self) -> None:
        self._steps = ToolStepExecutor()

    def prepare(self, runtime: AgentRuntime) -> PlanProposalResult | None:
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        planner = capabilities.decision_planner
        if planner is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support plan proposals.")
            return None
        while True:
            if cancel_if_requested(runtime):
                return None
            if not _ensure_tool_budget(runtime):
                return None
            if not _claim_model_turn(runtime, "decision"):
                return None
            close = _model_text_stream(runtime, stream_content=True)
            try:
                response = planner.decide(runtime)
            except PlanningError as exc:
                close()
                _publish_repairs(runtime, capabilities)
                if cancel_if_requested(runtime):
                    return None
                fail_run(runtime, f"Plan creation failed: {exc}", **planning_failure_data(exc, capabilities.name))
                return None
            except BaseException:
                close()
                raise
            else:
                streamed = close()
            _publish_repairs(runtime, capabilities)
            _record_reasoning(runtime, response, streamed.reasoning)
            _publish_assistant_message(runtime, response, streamed)
            if cancel_if_requested(runtime):
                _fail_pending_tools(runtime, response, "Not executed because the run was cancelled.")
                return None
            if consume_steering(runtime, phase="after_model_response") is not None:
                _fail_pending_tools(runtime, response, "Not executed because the user supplied new instructions.")
                continue
            if not response.tool_messages:
                _start_assistant(runtime, response)
                _finish_assistant(runtime)
                return PlanProposalResult(response, content_streamed=streamed.content)
            if not _tool_batch_fits(runtime, response):
                _reject_over_budget_tools(runtime, response)
                return None
            if any(tool.name == REQUEST_USER_INPUT_NAME for tool in response.tool_messages):
                self._request_user_input(runtime, response)
                if runtime.run.status != "running":
                    return None
                continue
            if any(tool.name == REQUEST_PLAN_REVIEW_NAME for tool in response.tool_messages):
                plan = self._request_plan_review(runtime, response)
                if plan is not None:
                    return PlanProposalResult(response, plan, streamed.content)
                continue
            _start_assistant(runtime, response)
            steered = False
            for index, tool in enumerate(response.tool_messages):
                if cancel_if_requested(runtime):
                    return None
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
                outcome = _execute_tool(runtime, index, self._steps)
                if cancel_if_requested(runtime):
                    return None
                if outcome.interrupt is not None:
                    runtime.state.active_message = None
                    runtime.state.active_tool_index = None
                    if outcome.interrupt.choice == "cancel":
                        cancel_run(runtime)
                    else:
                        record_plan_feedback(runtime, outcome.interrupt.supplement)
                    return None
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
                if not outcome.success:
                    error = outcome.error or "Tool failed."
                    tool.content = f"{tool.name} failed: {_truncate(error)}"
                    _publish_tool_recovery(runtime, tool, error)
            if steered:
                continue
            _finish_assistant(runtime)
