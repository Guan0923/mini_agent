"""Reactive model/tool execution workflow."""

from __future__ import annotations

from mini_agent.domain import PlanningError, ToolMessage

from ..cancellation import cancel_if_requested
from ..context import AgentRuntime
from ..events import RuntimeEvent
from ..outcomes import cancel_run, complete_run, fail_run, planning_failure_data, record_plan_feedback
from ..planner import PlannerCapabilities
from ..steering import collect_steering, consume_steering
from ..steps import ToolStepExecutor
from .shared import (
    _apply_tool_batch_steering,
    _execute_tool,
    _finish_assistant,
    _publish,
    _publish_repairs,
    _reasoning_stream,
    _record_reasoning,
    _same_tool,
    _start_assistant,
    _truncate,
)


class ReactiveWorkflow:
    def __init__(self) -> None:
        self._steps = ToolStepExecutor()

    def run(self, runtime: AgentRuntime):
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        planner = capabilities.decision_planner
        if planner is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support reactive decisions.")
            return runtime.run
        consecutive_failures = 0
        blocked: ToolMessage | None = None

        while len(runtime.run.actions) < runtime.state.runner_settings.max_actions:
            if cancel_if_requested(runtime):
                return runtime.run
            close = _reasoning_stream(runtime)
            try:
                response = planner.decide(runtime)
            except PlanningError as exc:
                close()
                _publish_repairs(runtime, capabilities)
                fail_run(runtime, f"Decision failed: {exc}", **planning_failure_data(exc, capabilities.name))
                return runtime.run
            streamed = close()
            _publish_repairs(runtime, capabilities)
            _record_reasoning(runtime, response, streamed)

            if cancel_if_requested(runtime):
                return runtime.run

            if consume_steering(runtime, phase="after_model_response") is not None:
                continue

            if not response.tool_messages:
                complete_run(runtime, response)
                return runtime.run
            if len(runtime.run.actions) + len(response.tool_messages) > runtime.state.runner_settings.max_actions:
                fail_run(runtime, f"Stopped after {runtime.state.runner_settings.max_actions} actions.")
                return runtime.run

            _start_assistant(runtime, response)
            stop_after_batch: str | None = None
            steered = False
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
                if _same_tool(tool, blocked):
                    stop_after_batch = f"Stopped: refusing to repeat non-retryable tool call {tool.name} after failure."
                    tool.status = "failed"
                    tool.content = stop_after_batch
                    continue
                outcome = _execute_tool(runtime, index, self._steps)
                if cancel_if_requested(runtime):
                    return runtime.run
                if outcome.interrupt is not None:
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
                    consecutive_failures = 0
                    blocked = None
                    continue
                error = outcome.error or "Tool execution failed without an error message."
                tool.content = f"{tool.name} failed: {_truncate(error)}"
                consecutive_failures += 1
                blocked = tool if outcome.retryable is False else None
                if consecutive_failures > runtime.state.runner_settings.max_tool_recoveries:
                    stop_after_batch = f"Stopped: {tool.name} failed: {error}"
                    continue
                runtime.run.add_event(
                    "tool_recovery",
                    f"Recovering from {tool.name} failure",
                    tool=tool.name,
                    error=_truncate(error),
                    attempt=consecutive_failures,
                )
                _publish(
                    runtime,
                    RuntimeEvent(
                        "tool_recovery",
                        _truncate(error),
                        {"tool": tool.name, "attempt": consecutive_failures},
                    ),
                )
            if steered:
                continue
            _finish_assistant(runtime)
            if stop_after_batch:
                fail_run(runtime, stop_after_batch)
                return runtime.run

        fail_run(runtime, f"Stopped after {runtime.state.runner_settings.max_actions} actions without a final answer.")
        return runtime.run
