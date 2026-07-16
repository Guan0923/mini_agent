"""Execution workflows operating on a single AgentRuntime argument."""

from __future__ import annotations

import re
from collections.abc import Callable

from mini_agent.domain import AssistantMessage, ExecutionPlan, PlanningError, PlanStep, ToolMessage, UserMessage
from mini_agent.planning import PlannerCapabilities

from .context import AgentRuntime
from .events import RuntimeEvent
from .outcomes import cancel_run, complete_run, fail_run, planning_failure_data, record_plan_feedback
from .steering import SteeringUpdate, apply_steering, collect_steering, consume_steering
from .steps import ToolStepExecutor, ToolStepResult

_MAX_TOOL_CONTEXT_CHARS = 2_000


def _publish(runtime: AgentRuntime, event: RuntimeEvent) -> None:
    (runtime.services.publish or (lambda _event: None))(event)


def _reasoning_stream(runtime: AgentRuntime) -> Callable[[], bool]:
    streamed = False

    def on_reasoning(chunk: str) -> None:
        nonlocal streamed
        if not streamed:
            _publish(runtime, RuntimeEvent("thinking_start"))
            streamed = True
        _publish(runtime, RuntimeEvent("thinking_delta", chunk))

    runtime.exchange.on_reasoning = on_reasoning

    def close() -> bool:
        runtime.exchange.on_reasoning = None
        if streamed:
            _publish(runtime, RuntimeEvent("thinking_end"))
        return streamed

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
    if not streamed:
        _publish(runtime, RuntimeEvent("thinking_start"))
        _publish(runtime, RuntimeEvent("thinking_delta", message.reasoning))
        _publish(runtime, RuntimeEvent("thinking_end"))


def _truncate(value: str) -> str:
    if len(value) <= _MAX_TOOL_CONTEXT_CHARS:
        return value
    omitted = len(value) - _MAX_TOOL_CONTEXT_CHARS
    return f"{value[:_MAX_TOOL_CONTEXT_CHARS]}… ({omitted} characters omitted)"


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
        for tool in message.tool_messages[next_tool_index:]:
            if tool.status == "pending":
                tool.status = "failed"
                tool.content = "Not executed because the user supplied new instructions."
                tool.retryable = False
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
            close = _reasoning_stream(runtime)
            try:
                response = planner.decide(runtime)
            except PlanningError as exc:
                close()
                fail_run(runtime, f"Plan creation failed: {exc}", **planning_failure_data(exc, capabilities.name))
                return None
            streamed = close()
            _record_reasoning(runtime, response, streamed)
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
                            content="[Plan format correction]\nReturn the proposal as a concise numbered plan starting with 1."
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
            _start_assistant(runtime, response)
            steered = False
            for index, tool in enumerate(response.tool_messages):
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
        close = _reasoning_stream(runtime)
        try:
            return (
                creator.create_dynamic_plan(runtime)
                if dynamic and capabilities.dynamic_plan_creator
                else creator.create_plan(runtime)
            )
        except PlanningError as exc:
            fail_run(runtime, f"Planning failed: {exc}", **planning_failure_data(exc, capabilities.name))
            return None
        finally:
            close()

    def _activate(self, runtime: AgentRuntime, plan: ExecutionPlan) -> bool:
        remaining = runtime.state.runner_settings.max_actions - len(runtime.run.actions)
        if len(plan.steps) > remaining:
            fail_run(runtime, f"Plan has {len(plan.steps)} steps but only {remaining} actions remain.")
            return False
        runtime.run.plan = plan
        runtime.run.add_event("plan", "Execution plan created", revision=plan.revision, step_count=len(plan.steps))
        _publish(runtime, RuntimeEvent("plan", self._format_plan(plan), {"revision": plan.revision}))
        runtime.save()
        return True

    def prepare(self, runtime: AgentRuntime) -> ExecutionPlan | None:
        if runtime.run.plan is not None:
            return runtime.run.plan
        while runtime.run.plan is None:
            plan = self._create_plan(runtime)
            if plan is None:
                return None
            if consume_steering(runtime, phase="after_plan_creation") is not None:
                continue
            return plan if self._activate(runtime, plan) else None
        return runtime.run.plan

    def revise_with_feedback(self, runtime: AgentRuntime) -> bool:
        supplement = runtime.exchange.context.get("supplement")
        feedback = record_plan_feedback(runtime, supplement if isinstance(supplement, str) else None)
        if feedback is None:
            return False
        return self._revise(runtime, f"Human plan feedback: {feedback}")

    def revise_with_steering(self, runtime: AgentRuntime) -> bool:
        return self._revise(runtime, "The user supplied new instructions while the plan was running.")

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
            fail_run(runtime, f"Replan failed: {exc}", **planning_failure_data(exc, capabilities.name))
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


class PlanExecuteWorkflow(PlanWorkflow):
    def run(self, runtime: AgentRuntime):
        plan = self.prepare(runtime)
        if plan is None:
            return runtime.run
        if not plan.steps:
            if consume_steering(runtime, phase="before_plan_completion") is not None:
                if self.revise_with_steering(runtime):
                    return self.run(runtime)
                return runtime.run
            complete_run(runtime, AssistantMessage(content=plan.final_answer or ""))
            return runtime.run
        for step in plan.steps:
            if step.status != "pending":
                continue
            if consume_steering(runtime, phase="before_plan_step") is not None:
                if self.revise_with_steering(runtime):
                    return self.run(runtime)
                return runtime.run
            outcome = self._execute_step(runtime, step)
            if outcome.interrupt is not None:
                step.status = "pending"
                if outcome.interrupt.choice == "cancel":
                    cancel_run(runtime)
                    return runtime.run
                runtime.exchange.context = {"supplement": outcome.interrupt.supplement}
                if not self.revise_with_feedback(runtime):
                    return runtime.run
                return self.run(runtime)
            update = collect_steering(runtime)
            if update is not None:
                if outcome.success:
                    step.status = "completed"
                    step.result = outcome.output
                else:
                    step.status = "failed"
                    step.result = outcome.error
                apply_steering(runtime, update, phase="after_plan_step")
                if self.revise_with_steering(runtime):
                    return self.run(runtime)
                return runtime.run
            if not outcome.success:
                step.status = "failed"
                step.result = outcome.error
                fail_run(runtime, f"Stopped: {step.tool_message.name} failed: {outcome.error}")
                return runtime.run
            step.status = "completed"
            step.result = outcome.output
        if consume_steering(runtime, phase="before_plan_completion") is not None:
            if self.revise_with_steering(runtime):
                return self.run(runtime)
            return runtime.run
        complete_run(runtime, AssistantMessage(content=self._format_completion([plan])))
        return runtime.run


class DynamicReplanWorkflow(PlanWorkflow):
    def run(self, runtime: AgentRuntime):
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        if capabilities.dynamic_replanner is None:
            fail_run(runtime, f"Planner {capabilities.name!r} does not support dynamic_replan.")
            return runtime.run
        while runtime.run.plan is None:
            plan = self._create_plan(runtime, dynamic=True)
            if plan is None:
                return runtime.run
            if consume_steering(runtime, phase="after_plan_creation") is not None:
                continue
            if not self._activate(runtime, plan):
                return runtime.run

        while runtime.run.plan is not None:
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
            if outcome.interrupt is not None:
                step.status = "pending"
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
                runtime.exchange.context = {"plan": plan, "step": step, "result": outcome.output or ""}
                try:
                    evaluation = capabilities.dynamic_replanner.evaluate_step(runtime)
                except PlanningError as exc:
                    fail_run(runtime, f"Step evaluation failed: {exc}", **planning_failure_data(exc, capabilities.name))
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
            fail_run(runtime, f"Replan failed: {exc}", **planning_failure_data(exc, capabilities.name))
            return False
        replacement.revision = current.revision + 1
        for step in current.steps:
            if step.status == "pending":
                step.status = "superseded"
        runtime.run.plan_history.append(current)
        runtime.run.plan = replacement
        runtime.run.replan_count += 1
        runtime.run.add_event(
            "replan_applied", "Replacement plan applied", revision=replacement.revision, reason=reason
        )
        _publish(runtime, RuntimeEvent("replan_applied", self._format_plan(replacement), {"reason": reason}))
        runtime.save()
        return True
