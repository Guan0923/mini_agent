"""Plan-mode question and review controls."""

from __future__ import annotations

from backend.domain import AssistantMessage, ToolMessage, safe_error_message

from ...conversation.user_input import (
    format_user_input_answers,
    parse_user_input_questions,
    validate_user_input_answers,
)
from ...core.context import AgentRuntime
from ...core.contracts import InterruptRequest
from ...core.events import RuntimeEvent
from ...planning.review import parse_plan_review
from ..lifecycle.cancellation import cancel_if_requested
from ..lifecycle.outcomes import cancel_run, fail_run
from .common import (
    _finish_assistant,
    _publish,
    _publish_tool_call,
    _publish_tool_failure,
    _publish_tool_result,
    _start_assistant,
)


class PlanControlMixin:
    @staticmethod
    def _request_plan_review(runtime: AgentRuntime, response: AssistantMessage) -> str | None:
        _start_assistant(runtime, response)
        for tool in response.tool_messages:
            runtime.run.actions.append(tool)
            _publish_tool_call(runtime, tool)

        if len(response.tool_messages) != 1:
            error = "request_plan_review must be the only tool call in an assistant response."
            for tool in response.tool_messages:
                tool.status = "failed"
                tool.content = error
                tool.retryable = True
                _publish_tool_failure(runtime, tool, error)
            _finish_assistant(runtime)
            return None

        tool = response.tool_messages[0]
        runtime.state.active_tool_index = 0
        try:
            plan = parse_plan_review(tool.arguments)
        except ValueError as exc:
            tool.status = "failed"
            tool.content = safe_error_message(exc)
            tool.retryable = True
            _publish_tool_failure(runtime, tool, safe_error_message(exc))
            _finish_assistant(runtime)
            return None

        tool.status = "succeeded"
        tool.content = "Plan submitted for review."
        tool.retryable = False
        _publish_tool_result(runtime, tool)
        _finish_assistant(runtime)
        return plan

    @staticmethod
    def _request_user_input(runtime: AgentRuntime, response: AssistantMessage) -> bool:
        _start_assistant(runtime, response)
        for tool in response.tool_messages:
            runtime.run.actions.append(tool)
            _publish_tool_call(runtime, tool)

        if len(response.tool_messages) != 1:
            error = "request_user_input must be the only tool call in an assistant response."
            for tool in response.tool_messages:
                tool.status = "failed"
                tool.content = error
                tool.retryable = True
                _publish_tool_failure(runtime, tool, error)
            _finish_assistant(runtime)
            return False

        tool = response.tool_messages[0]
        runtime.state.active_tool_index = 0
        try:
            questions = parse_user_input_questions(tool.arguments)
        except ValueError as exc:
            PlanControlMixin._fail_user_input(runtime, tool, safe_error_message(exc), retryable=True)
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
            {"questions": question_data, "call_id": tool.call_id},
            questions=questions,
        )
        _publish(runtime, RuntimeEvent("user_input_requested", request.message, request.data))
        runtime.save()

        if runtime.services.interrupt is None:
            PlanControlMixin._fail_user_input(
                runtime,
                tool,
                "Plan question cancelled because no interrupt handler is available.",
                retryable=False,
            )
            cancel_run(runtime)
            return False

        decision = runtime.services.interrupt(request)
        if cancel_if_requested(runtime) or decision.choice == "cancel":
            PlanControlMixin._fail_user_input(runtime, tool, "Plan question cancelled by user.", retryable=False)
            if runtime.run.status == "running":
                cancel_run(runtime)
            return False
        if decision.choice != "answer":
            PlanControlMixin._fail_user_input(
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
            PlanControlMixin._fail_user_input(runtime, tool, safe_error_message(exc), retryable=True)
            return False

        tool.status = "succeeded"
        tool.content = format_user_input_answers(answers)
        tool.retryable = False
        _publish_tool_result(runtime, tool)
        _publish(
            runtime,
            RuntimeEvent(
                "user_input_received", "Plan question answers received", {"call_id": tool.call_id, "answers": answers}
            ),
        )
        _finish_assistant(runtime)
        return True

    @staticmethod
    def _fail_user_input(runtime: AgentRuntime, tool: ToolMessage, error: str, *, retryable: bool) -> None:
        tool.status = "failed"
        tool.content = error
        tool.retryable = retryable
        _publish_tool_failure(runtime, tool, error)
        _finish_assistant(runtime)
