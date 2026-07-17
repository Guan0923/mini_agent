"""Plan-mode research and proposal workflow."""

from __future__ import annotations

import re

from mini_agent.domain import AssistantMessage, PlanningError, ToolMessage, UserMessage

from ..cancellation import cancel_if_requested
from ..context import AgentRuntime
from ..contracts import InterruptRequest
from ..events import RuntimeEvent
from ..outcomes import cancel_run, fail_run, planning_failure_data, record_plan_feedback
from ..planner import PlannerCapabilities
from ..steering import collect_steering, consume_steering
from ..steps import ToolStepExecutor
from ..user_input import (
    REQUEST_USER_INPUT_NAME,
    format_user_input_answers,
    parse_user_input_questions,
    validate_user_input_answers,
)
from .shared import (
    _apply_tool_batch_steering,
    _execute_tool,
    _finish_assistant,
    _publish,
    _reasoning_stream,
    _record_reasoning,
    _start_assistant,
    _truncate,
)


class PlanProposalWorkflow:
    def __init__(self) -> None:
        self._steps = ToolStepExecutor()

    def prepare(self, runtime: AgentRuntime) -> str | None:
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        planner = capabilities.decision_planner
        if planner is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support plan proposals.")
            return None
        format_repair_used = False
        consecutive_failures = 0
        while len(runtime.run.actions) < runtime.state.runner_settings.max_actions:
            if cancel_if_requested(runtime):
                return None
            close = _reasoning_stream(runtime)
            try:
                response = planner.decide(runtime)
            except PlanningError as exc:
                close()
                fail_run(runtime, f"Plan creation failed: {exc}", **planning_failure_data(exc, capabilities.name))
                return None
            streamed = close()
            _record_reasoning(runtime, response, streamed)
            if cancel_if_requested(runtime):
                return None
            if consume_steering(runtime, phase="after_model_response") is not None:
                continue
            if not response.tool_messages:
                proposal = response.content or ""
                if re.search(r"(?m)^\s*1[.)、]\s+\S", proposal):
                    return proposal
                if format_repair_used:
                    fail_run(runtime, "Plan proposal must be a numbered high-level plan.")
                    return None
                format_repair_used = True
                runtime.state.messages.extend(
                    [
                        response,
                        UserMessage(
                            content=(
                                "[Plan format correction]\nUse request_user_input for material clarification questions; "
                                "otherwise return a concise numbered plan starting with 1."
                            )
                        ),
                    ]
                )
                runtime.run.history = runtime.state.messages
                runtime.run.add_event("model_repair", "Requested numbered plan format", phase="plan_proposal")
                _publish(
                    runtime,
                    RuntimeEvent(
                        "model_repair",
                        "Requested numbered plan format",
                        {"phase": "plan_proposal", "attempt": 1},
                    ),
                )
                continue
            if any(tool.name == REQUEST_USER_INPUT_NAME for tool in response.tool_messages):
                answered = self._request_user_input(runtime, response)
                if runtime.run.status != "running":
                    return None
                if answered:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures > runtime.state.runner_settings.max_tool_recoveries:
                        fail_run(runtime, "Stopped after repeated invalid request_user_input calls.")
                        return None
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
                if outcome.success:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    error = outcome.error or "Tool failed."
                    tool.content = f"{tool.name} failed: {_truncate(error)}"
                    if consecutive_failures <= runtime.state.runner_settings.max_tool_recoveries:
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
            if consecutive_failures > runtime.state.runner_settings.max_tool_recoveries:
                failed = next((tool for tool in reversed(response.tool_messages) if tool.status == "failed"), None)
                details = failed.content if failed is not None else "unknown error"
                fail_run(runtime, f"Stopped: {details}")
                return None
        fail_run(runtime, f"Stopped after {runtime.state.runner_settings.max_actions} actions without a plan proposal.")
        return None

    @staticmethod
    def _request_user_input(runtime: AgentRuntime, response: AssistantMessage) -> bool:
        _start_assistant(runtime, response)
        for tool in response.tool_messages:
            runtime.run.actions.append(tool)

        if len(response.tool_messages) != 1:
            error = "request_user_input must be the only tool call in an assistant response."
            for tool in response.tool_messages:
                tool.status = "failed"
                tool.content = error
                tool.retryable = True
            runtime.run.add_event("tool_failed", f"{REQUEST_USER_INPUT_NAME} failed", error=error)
            _publish(runtime, RuntimeEvent("tool_failed", error, {"tool": REQUEST_USER_INPUT_NAME}))
            _finish_assistant(runtime)
            return False

        tool = response.tool_messages[0]
        runtime.state.active_tool_index = 0
        runtime.run.add_event("model", "Plan question call validated", tool=tool.name, mode=runtime.run.mode)
        try:
            questions = parse_user_input_questions(tool.arguments)
        except ValueError as exc:
            PlanProposalWorkflow._fail_user_input(runtime, tool, str(exc), retryable=True)
            return False

        question_data = [
            {
                "id": question.id,
                "header": question.header,
                "question": question.question,
                "options": [{"label": option.label, "description": option.description} for option in question.options],
            }
            for question in questions
        ]
        request = InterruptRequest(
            "question",
            "Answer the Plan-mode clarification questions.",
            {"questions": question_data},
            questions=questions,
        )
        runtime.run.add_event("user_input_requested", request.message, questions=question_data)
        _publish(runtime, RuntimeEvent("user_input_requested", request.message, request.data))
        runtime.save()

        if runtime.services.interrupt is None:
            PlanProposalWorkflow._fail_user_input(
                runtime,
                tool,
                "Plan question cancelled because no interrupt handler is available.",
                retryable=False,
            )
            cancel_run(runtime)
            return False

        decision = runtime.services.interrupt(request)
        if cancel_if_requested(runtime) or decision.choice == "cancel":
            PlanProposalWorkflow._fail_user_input(runtime, tool, "Plan question cancelled by user.", retryable=False)
            if runtime.run.status == "running":
                cancel_run(runtime)
            return False
        if decision.choice != "answer":
            PlanProposalWorkflow._fail_user_input(
                runtime,
                tool,
                f"Invalid Plan question decision: {decision.choice}.",
                retryable=False,
            )
            fail_run(runtime, f"Invalid Plan question decision: {decision.choice}.")
            return False

        try:
            answers = validate_user_input_answers(questions, decision.answers)
        except ValueError as exc:
            PlanProposalWorkflow._fail_user_input(runtime, tool, str(exc), retryable=True)
            return False

        tool.status = "succeeded"
        tool.content = format_user_input_answers(answers)
        tool.retryable = False
        runtime.run.add_event("user_input_received", "Plan question answers received", answers=answers)
        _publish(runtime, RuntimeEvent("user_input_received", "Plan question answers received", {"answers": answers}))
        _finish_assistant(runtime)
        return True

    @staticmethod
    def _fail_user_input(runtime: AgentRuntime, tool: ToolMessage, error: str, *, retryable: bool) -> None:
        tool.status = "failed"
        tool.content = error
        tool.retryable = retryable
        runtime.run.add_event("tool_failed", f"{REQUEST_USER_INPUT_NAME} failed", error=error)
        _publish(runtime, RuntimeEvent("tool_failed", error, {"tool": REQUEST_USER_INPUT_NAME}))
        _finish_assistant(runtime)
