"""Execution workflows operating on a single AgentRuntime argument."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mini_agent.domain import AssistantMessage, ExecutionPlan, PlanningError, PlanStep, ToolMessage, message_to_dict
from mini_agent.planning import PlannerCapabilities

from ..conversation.steering import SteeringUpdate, apply_steering, collect_steering, consume_steering
from ..conversation.user_input import (
    REQUEST_USER_INPUT_NAME,
    format_user_input_answers,
    parse_user_input_questions,
    validate_user_input_answers,
)
from ..core.context import AgentRuntime
from ..core.contracts import InterruptRequest
from ..core.events import RuntimeEvent
from ..planning.review import REQUEST_PLAN_REVIEW_NAME, parse_plan_review
from .cancellation import cancel_if_requested
from .outcomes import cancel_run, complete_run, fail_run, planning_failure_data, record_plan_feedback
from .steps import ToolStepExecutor, ToolStepResult

_MAX_TOOL_CONTEXT_CHARS = 2_000


@dataclass(frozen=True)
class PlanProposalResult:
    """One completed Plan-mode response, optionally submitted for review."""

    message: AssistantMessage
    plan: str | None = None
    content_streamed: bool = False


def _publish(runtime: AgentRuntime, event: RuntimeEvent) -> None:
    (runtime.services.publish or (lambda _event: None))(event)


@dataclass(frozen=True)
class _TextStreamResult:
    reasoning: bool
    content: bool


def _model_text_stream(
    runtime: AgentRuntime,
    *,
    stream_content: bool = False,
) -> Callable[[], _TextStreamResult]:
    reasoning_open = False
    response_open = False
    reasoning_streamed = False
    content_streamed = False

    def close_reasoning() -> None:
        nonlocal reasoning_open
        if reasoning_open:
            _publish(runtime, RuntimeEvent("thinking_end"))
            reasoning_open = False

    def close_response() -> None:
        nonlocal response_open
        if response_open:
            _publish(runtime, RuntimeEvent("response_end"))
            response_open = False

    def on_reasoning(chunk: str) -> None:
        nonlocal reasoning_open, reasoning_streamed
        if not chunk:
            return
        close_response()
        if not reasoning_open:
            _publish(runtime, RuntimeEvent("thinking_start"))
            reasoning_open = True
            reasoning_streamed = True
        _publish(runtime, RuntimeEvent("thinking_delta", chunk))

    def on_content(chunk: str) -> None:
        nonlocal response_open, content_streamed
        if not chunk:
            return
        close_reasoning()
        if not response_open:
            _publish(runtime, RuntimeEvent("response_start"))
            response_open = True
            content_streamed = True
        _publish(runtime, RuntimeEvent("response_delta", chunk))

    runtime.exchange.on_reasoning = on_reasoning
    runtime.exchange.on_content = on_content if stream_content else None

    def close() -> _TextStreamResult:
        runtime.exchange.on_reasoning = None
        runtime.exchange.on_content = None
        close_reasoning()
        close_response()
        return _TextStreamResult(reasoning_streamed, content_streamed)

    return close


def _publish_repairs(runtime: AgentRuntime, capabilities: PlannerCapabilities) -> None:
    reporter = capabilities.output_repair_reporter
    if reporter is None:
        return
    for repair in reporter.consume_output_repairs():
        if not isinstance(repair, dict):
            continue
        outcome = repair.get("outcome")
        message = (
            "Malformed model output was repaired automatically."
            if outcome == "repaired"
            else "Malformed model output could not be repaired automatically."
        )
        runtime.run.add_event("model_repair", message, **repair)
        _publish(runtime, RuntimeEvent("model_repair", message, repair))


def _record_reasoning(runtime: AgentRuntime, message: AssistantMessage, streamed: bool) -> None:
    if not message.reasoning:
        return
    runtime.run.add_event("reasoning", "Model reasoning", content=message.reasoning)


def _publish_assistant_message(
    runtime: AgentRuntime,
    message: AssistantMessage,
    streamed: _TextStreamResult,
) -> None:
    """Publish one transient boundary after a completed assistant response."""

    _publish(
        runtime,
        RuntimeEvent(
            "assistant_message",
            data={
                "message": message_to_dict(message),
                "exchange_id": runtime.exchange.exchange_id,
                "reasoning_streamed": streamed.reasoning,
                "content_streamed": streamed.content,
            },
        ),
    )


def _publish_tool_call(runtime: AgentRuntime, tool: ToolMessage) -> None:
    runtime.run.add_event("tool_call", f"Calling {tool.name}", call_id=tool.call_id, arguments=dict(tool.arguments))
    _publish(
        runtime,
        RuntimeEvent("tool_call", tool.name, {"call_id": tool.call_id, "arguments": dict(tool.arguments)}),
    )


def _publish_tool_result(runtime: AgentRuntime, tool: ToolMessage) -> None:
    result = tool.content or ""
    runtime.run.add_event("tool_result", f"{tool.name} succeeded", call_id=tool.call_id, result=result)
    _publish(runtime, RuntimeEvent("tool_result", result, {"tool": tool.name, "call_id": tool.call_id}))


def _publish_tool_failure(runtime: AgentRuntime, tool: ToolMessage, error: str) -> None:
    runtime.run.add_event("tool_failed", f"{tool.name} failed", call_id=tool.call_id, error=error)
    _publish(runtime, RuntimeEvent("tool_failed", error, {"tool": tool.name, "call_id": tool.call_id}))


def _fail_pending_tools(runtime: AgentRuntime, message: AssistantMessage, error: str) -> None:
    """Close unexecuted tool calls after cancellation or steering."""

    for tool in message.tool_messages:
        if tool.status == "pending":
            tool.status = "failed"
            tool.content = error
            tool.retryable = False
            _publish_tool_failure(runtime, tool, error)


def _truncate(value: str) -> str:
    if len(value) <= _MAX_TOOL_CONTEXT_CHARS:
        return value
    omitted = len(value) - _MAX_TOOL_CONTEXT_CHARS
    return f"{value[:_MAX_TOOL_CONTEXT_CHARS]}… ({omitted} characters omitted)"


def _plan_step_snapshots(plan: ExecutionPlan) -> list[dict[str, object]]:
    """Return presentation-independent state for every plan step."""

    return [
        {
            "index": index,
            "id": step.id,
            "description": step.description,
            "status": step.status,
            "result": step.result,
        }
        for index, step in enumerate(plan.steps, start=1)
    ]


def _publish_plan_progress(
    runtime: AgentRuntime,
    plan: ExecutionPlan,
    *,
    trigger: str,
    changed_step_id: str | None = None,
) -> None:
    data = {
        "revision": plan.revision,
        "trigger": trigger,
        "steps": _plan_step_snapshots(plan),
        "changed_step_id": changed_step_id,
    }
    runtime.run.add_event("plan_progress", "Plan step status updated", **data)
    _publish(runtime, RuntimeEvent("plan_progress", "Plan step status updated", data))
    runtime.save()


def _same_tool(first: ToolMessage, second: ToolMessage | None) -> bool:
    return second is not None and first.name == second.name and first.arguments == second.arguments


def _start_assistant(runtime: AgentRuntime, message: AssistantMessage) -> None:
    runtime.state.active_message = message
    runtime.state.active_tool_index = None
    runtime.save()


def _finish_assistant(runtime: AgentRuntime) -> None:
    message = runtime.state.active_message
    if message is None:
        return
    runtime.state.messages.append(message)
    runtime.run.history = runtime.state.messages
    runtime.state.active_message = None
    runtime.state.active_tool_index = None
    runtime.save()


def _execute_tool(runtime: AgentRuntime, index: int, executor: ToolStepExecutor) -> ToolStepResult:
    message = runtime.state.active_message
    assert message is not None
    tool = message.tool_messages[index]
    runtime.state.active_tool_index = index
    runtime.run.actions.append(tool)
    runtime.run.add_event("model", "Model tool call validated", tool=tool.name, mode=runtime.run.mode)
    return executor.execute(runtime)


def _apply_tool_batch_steering(
    runtime: AgentRuntime,
    update: SteeringUpdate,
    *,
    next_tool_index: int,
    phase: str,
) -> None:
    """Preserve completed calls and close any unexecuted calls before steering."""

    message = runtime.state.active_message
    if message is not None and next_tool_index > 0:
        _fail_pending_tools(runtime, message, "Not executed because the user supplied new instructions.")
        _finish_assistant(runtime)
    else:
        runtime.state.active_message = None
        runtime.state.active_tool_index = None
    apply_steering(runtime, update, phase=phase)


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
            close = _model_text_stream(runtime, stream_content=True)
            try:
                response = planner.decide(runtime)
            except PlanningError as exc:
                close()
                _publish_repairs(runtime, capabilities)
                fail_run(runtime, f"Decision failed: {exc}", **planning_failure_data(exc, capabilities.name))
                return runtime.run
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
                return runtime.run

            if consume_steering(runtime, phase="after_model_response") is not None:
                _fail_pending_tools(runtime, response, "Not executed because the user supplied new instructions.")
                continue

            if not response.tool_messages:
                complete_run(runtime, response, response_streamed=streamed.content)
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
                    tool.retryable = False
                    _publish_tool_failure(runtime, tool, stop_after_batch)
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
                    call_id=tool.call_id,
                    error=_truncate(error),
                    attempt=consecutive_failures,
                )
                _publish(
                    runtime,
                    RuntimeEvent(
                        "tool_recovery",
                        _truncate(error),
                        {"tool": tool.name, "call_id": tool.call_id, "attempt": consecutive_failures},
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


class PlanProposalWorkflow:
    def __init__(self) -> None:
        self._steps = ToolStepExecutor()

    def prepare(self, runtime: AgentRuntime) -> PlanProposalResult | None:
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        planner = capabilities.decision_planner
        if planner is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support plan proposals.")
            return None
        consecutive_failures = 0
        while len(runtime.run.actions) < runtime.state.runner_settings.max_actions:
            if cancel_if_requested(runtime):
                return None
            close = _model_text_stream(runtime, stream_content=True)
            try:
                response = planner.decide(runtime)
            except PlanningError as exc:
                close()
                _publish_repairs(runtime, capabilities)
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
            if any(tool.name == REQUEST_PLAN_REVIEW_NAME for tool in response.tool_messages):
                plan = self._request_plan_review(runtime, response)
                if plan is not None:
                    return PlanProposalResult(response, plan, streamed.content)
                consecutive_failures += 1
                if consecutive_failures > runtime.state.runner_settings.max_tool_recoveries:
                    fail_run(runtime, "Stopped after repeated invalid request_plan_review calls.")
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
                            call_id=tool.call_id,
                            error=_truncate(error),
                            attempt=consecutive_failures,
                        )
                        _publish(
                            runtime,
                            RuntimeEvent(
                                "tool_recovery",
                                _truncate(error),
                                {"tool": tool.name, "call_id": tool.call_id, "attempt": consecutive_failures},
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
        fail_run(runtime, f"Stopped after {runtime.state.runner_settings.max_actions} actions without a final answer.")
        return None

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
        runtime.run.add_event("model", "Plan Review call validated", tool=tool.name, mode=runtime.run.mode)
        try:
            plan = parse_plan_review(tool.arguments)
        except ValueError as exc:
            tool.status = "failed"
            tool.content = str(exc)
            tool.retryable = True
            _publish_tool_failure(runtime, tool, str(exc))
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
                "options": [
                    {"label": option.label, "description": option.description} for option in question.options
                ],
            }
            for question in questions
        ]
        request = InterruptRequest(
            "question",
            "Answer the Plan-mode clarification questions.",
            {"questions": question_data, "call_id": tool.call_id},
            questions=questions,
        )
        runtime.run.add_event("user_input_requested", request.message, call_id=tool.call_id, questions=question_data)
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
        _publish_tool_result(runtime, tool)
        runtime.run.add_event("user_input_received", "Plan question answers received", call_id=tool.call_id, answers=answers)
        _publish(
            runtime,
            RuntimeEvent("user_input_received", "Plan question answers received", {"call_id": tool.call_id, "answers": answers}),
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


class PlanWorkflow:
    def __init__(self) -> None:
        self._steps = ToolStepExecutor()

    def _create_plan(self, runtime: AgentRuntime, *, dynamic: bool = False) -> ExecutionPlan | None:
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        creator = capabilities.dynamic_plan_creator if dynamic else capabilities.plan_creator
        if creator is None and dynamic:
            creator = capabilities.plan_creator
        if creator is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support plan creation.")
            return None
        close = _model_text_stream(runtime)
        try:
            plan = (
                creator.create_dynamic_plan(runtime)
                if dynamic and capabilities.dynamic_plan_creator
                else creator.create_plan(runtime)
            )
        except PlanningError as exc:
            _publish_repairs(runtime, capabilities)
            fail_run(runtime, f"Planning failed: {exc}", **planning_failure_data(exc, capabilities.name))
            return None
        finally:
            close()
        _publish_repairs(runtime, capabilities)
        return plan

    def _activate(self, runtime: AgentRuntime, plan: ExecutionPlan) -> bool:
        remaining = runtime.state.runner_settings.max_actions - len(runtime.run.actions)
        if len(plan.steps) > remaining:
            fail_run(runtime, f"Plan has {len(plan.steps)} steps but only {remaining} actions remain.")
            return False
        runtime.run.plan = plan
        snapshots = _plan_step_snapshots(plan)
        runtime.run.add_event(
            "plan",
            "Execution plan created",
            revision=plan.revision,
            step_count=len(plan.steps),
            trigger="created",
            steps=snapshots,
        )
        _publish(
            runtime,
            RuntimeEvent(
                "plan",
                self._format_plan(plan),
                {"revision": plan.revision, "trigger": "created", "steps": snapshots},
            ),
        )
        runtime.save()
        return True

    def revise_with_feedback(self, runtime: AgentRuntime) -> bool:
        supplement = runtime.exchange.context.get("supplement")
        feedback = record_plan_feedback(runtime, supplement if isinstance(supplement, str) else None)
        if feedback is None:
            return False
        return self._revise(runtime, f"Human plan feedback: {feedback}")

    def _revise(self, runtime: AgentRuntime, reason: str) -> bool:
        previous = runtime.run.plan
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        replanner = capabilities.plan_replanner
        if previous is None or replanner is None:
            fail_run(runtime, "Planner cannot revise the active plan.")
            return False
        runtime.exchange.context = {"plan": previous, "reason": reason}
        try:
            replacement = replanner.replan(runtime)
        except PlanningError as exc:
            _publish_repairs(runtime, capabilities)
            fail_run(runtime, f"Replan failed: {exc}", **planning_failure_data(exc, capabilities.name))
            return False
        _publish_repairs(runtime, capabilities)
        if cancel_if_requested(runtime):
            return False
        replacement.revision = previous.revision + 1
        for step in previous.steps:
            if step.status in {"pending", "running"}:
                step.status = "superseded"
        runtime.run.plan_history.append(previous)
        return self._activate(runtime, replacement)

    def _execute_step(self, runtime: AgentRuntime, step: PlanStep) -> ToolStepResult:
        step.status = "running"
        runtime.run.actions.append(step.tool_message)
        runtime.state.active_message = AssistantMessage(tool_messages=[step.tool_message])
        runtime.state.active_tool_index = 0
        runtime.run.add_event(
            "model", "Planned tool call validated", tool=step.tool_message.name, mode=runtime.run.mode
        )
        plan = runtime.run.plan
        if plan is not None:
            _publish_plan_progress(runtime, plan, trigger="step_started", changed_step_id=step.id)
        outcome = self._steps.execute(runtime)
        if outcome.interrupt is None:
            if not outcome.success:
                step.tool_message.content = (
                    f"{step.tool_message.name} failed: {_truncate(outcome.error or 'unknown error')}"
                )
            _finish_assistant(runtime)
        else:
            runtime.state.active_message = None
            runtime.state.active_tool_index = None
            if runtime.run.actions and runtime.run.actions[-1] is step.tool_message:
                runtime.run.actions.pop()
        return outcome

    @staticmethod
    def _format_plan(plan: ExecutionPlan) -> str:
        if not plan.steps:
            return plan.final_answer or ""
        return "\n".join(f"{index}. {step.description}" for index, step in enumerate(plan.steps, 1))

    @staticmethod
    def _format_completion(plans: list[ExecutionPlan], replans: int = 0, *, dynamic: bool = False) -> str:
        completed = [step for plan in plans for step in plan.steps if step.status == "completed"]
        details = "\n".join(f"- {step.description}: {step.result}" for step in completed)
        prefix = f"Execution plan completed with {len(completed)} planned tool calls"
        if dynamic:
            prefix += f" with {replans} replans"
        return f"{prefix}.\n{details}" if details else f"{prefix}."


class DynamicReplanWorkflow(PlanWorkflow):
    def run(self, runtime: AgentRuntime):
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        if capabilities.dynamic_replanner is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support dynamic_replan.")
            return runtime.run
        while runtime.run.plan is None:
            if cancel_if_requested(runtime):
                return runtime.run
            plan = self._create_plan(runtime, dynamic=True)
            if plan is None:
                return runtime.run
            if cancel_if_requested(runtime):
                return runtime.run
            if consume_steering(runtime, phase="after_plan_creation") is not None:
                continue
            if not self._activate(runtime, plan):
                return runtime.run

        while runtime.run.plan is not None:
            if cancel_if_requested(runtime):
                return runtime.run
            plan = runtime.run.plan
            step = next((candidate for candidate in plan.steps if candidate.status == "pending"), None)
            if step is None:
                if consume_steering(runtime, phase="before_plan_completion") is not None:
                    if not self._replace(
                        runtime,
                        "The user supplied new instructions before plan completion.",
                        capabilities,
                    ):
                        return runtime.run
                    continue
                if not plan.steps:
                    complete_run(runtime, AssistantMessage(content=plan.final_answer or ""))
                else:
                    complete_run(
                        runtime,
                        AssistantMessage(
                            content=self._format_completion(
                                [*runtime.run.plan_history, plan], runtime.run.replan_count, dynamic=True
                            )
                        ),
                    )
                return runtime.run
            if len(runtime.run.actions) >= runtime.state.runner_settings.max_actions:
                fail_run(runtime, f"Stopped after {runtime.state.runner_settings.max_actions} actions.")
                return runtime.run

            if consume_steering(runtime, phase="before_plan_step") is not None:
                if not self._replace(
                    runtime,
                    "The user supplied new instructions before the next plan step.",
                    capabilities,
                ):
                    return runtime.run
                continue

            outcome = self._execute_step(runtime, step)
            if cancel_if_requested(runtime):
                step.status = "completed" if outcome.success else "failed"
                step.result = outcome.output if outcome.success else outcome.error
                _publish_plan_progress(
                    runtime,
                    plan,
                    trigger="step_completed" if outcome.success else "step_failed",
                    changed_step_id=step.id,
                )
                return runtime.run
            if outcome.interrupt is not None:
                step.status = "pending"
                _publish_plan_progress(runtime, plan, trigger="step_interrupted", changed_step_id=step.id)
                if outcome.interrupt.choice == "cancel":
                    cancel_run(runtime)
                    return runtime.run
                runtime.exchange.context = {"supplement": outcome.interrupt.supplement}
                if not self.revise_with_feedback(runtime):
                    return runtime.run
                continue

            update = collect_steering(runtime)
            if update is not None:
                if outcome.success:
                    step.status = "completed"
                    step.result = outcome.output
                else:
                    step.status = "failed"
                    step.result = outcome.error
                _publish_plan_progress(
                    runtime,
                    plan,
                    trigger="step_completed" if outcome.success else "step_failed",
                    changed_step_id=step.id,
                )
                apply_steering(runtime, update, phase="after_plan_step")
                if not self._replace(
                    runtime,
                    "The user supplied new instructions after a plan step.",
                    capabilities,
                ):
                    return runtime.run
                continue

            if outcome.success:
                step.status = "completed"
                step.result = outcome.output
                _publish_plan_progress(runtime, plan, trigger="step_completed", changed_step_id=step.id)
                runtime.exchange.context = {"plan": plan, "step": step, "result": outcome.output or ""}
                try:
                    evaluation = capabilities.dynamic_replanner.evaluate_step(runtime)
                except PlanningError as exc:
                    _publish_repairs(runtime, capabilities)
                    fail_run(runtime, f"Step evaluation failed: {exc}", **planning_failure_data(exc, capabilities.name))
                    return runtime.run
                _publish_repairs(runtime, capabilities)
                if cancel_if_requested(runtime):
                    return runtime.run
                if consume_steering(runtime, phase="after_step_evaluation") is not None:
                    if not self._replace(
                        runtime,
                        "The user supplied new instructions during step evaluation.",
                        capabilities,
                    ):
                        return runtime.run
                    continue
                if evaluation.decision == "continue":
                    continue
                reason = evaluation.reason
            else:
                step.status = "failed"
                step.result = outcome.error
                _publish_plan_progress(runtime, plan, trigger="step_failed", changed_step_id=step.id)
                reason = f"Step {step.id} failed: {outcome.error or 'unknown error'}"

            if not self._replace(runtime, reason, capabilities):
                return runtime.run
        return runtime.run

    def _replace(self, runtime: AgentRuntime, reason: str, capabilities: PlannerCapabilities) -> bool:
        current = runtime.run.plan
        assert current is not None and capabilities.dynamic_replanner is not None
        runtime.run.add_event("replan_requested", "Replan requested", revision=current.revision, reason=reason)
        _publish(runtime, RuntimeEvent("replan_requested", reason, {"revision": current.revision}))
        if runtime.run.replan_count >= runtime.state.runner_settings.max_replans:
            fail_run(runtime, f"Stopped after {runtime.state.runner_settings.max_replans} replans: {reason}")
            return False
        runtime.exchange.context = {"plan": current, "reason": reason}
        try:
            replacement = capabilities.dynamic_replanner.replan(runtime)
        except PlanningError as exc:
            _publish_repairs(runtime, capabilities)
            fail_run(runtime, f"Replan failed: {exc}", **planning_failure_data(exc, capabilities.name))
            return False
        _publish_repairs(runtime, capabilities)
        if cancel_if_requested(runtime):
            return False
        replacement.revision = current.revision + 1
        for step in current.steps:
            if step.status == "pending":
                step.status = "superseded"
        previous_steps = _plan_step_snapshots(current)
        runtime.run.plan_history.append(current)
        runtime.run.plan = replacement
        runtime.run.replan_count += 1
        runtime.run.add_event(
            "replan_applied",
            "Replacement plan applied",
            revision=replacement.revision,
            reason=reason,
            previous_steps=previous_steps,
            steps=_plan_step_snapshots(replacement),
        )
        _publish(
            runtime,
            RuntimeEvent(
                "replan_applied",
                self._format_plan(replacement),
                {
                    "revision": replacement.revision,
                    "reason": reason,
                    "previous_steps": previous_steps,
                    "steps": _plan_step_snapshots(replacement),
                },
            ),
        )
        runtime.save()
        return True
