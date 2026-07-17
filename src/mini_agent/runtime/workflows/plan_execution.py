"""Fixed and dynamic plan execution workflows."""

from __future__ import annotations

from mini_agent.domain import AssistantMessage, ExecutionPlan, PlanningError, PlanStep

from ..cancellation import cancel_if_requested
from ..context import AgentRuntime
from ..events import RuntimeEvent
from ..outcomes import cancel_run, complete_run, fail_run, planning_failure_data, record_plan_feedback
from ..planner import PlannerCapabilities
from ..steering import apply_steering, collect_steering, consume_steering
from ..steps import ToolStepExecutor, ToolStepResult
from .shared import _finish_assistant, _publish, _reasoning_stream, _truncate


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
            if cancel_if_requested(runtime):
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
        if cancel_if_requested(runtime):
            return runtime.run
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
            if cancel_if_requested(runtime):
                return runtime.run
            if consume_steering(runtime, phase="before_plan_step") is not None:
                if self.revise_with_steering(runtime):
                    return self.run(runtime)
                return runtime.run
            outcome = self._execute_step(runtime, step)
            if cancel_if_requested(runtime):
                step.status = "completed" if outcome.success else "failed"
                step.result = outcome.output if outcome.success else outcome.error
                return runtime.run
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
                return runtime.run
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
        if cancel_if_requested(runtime):
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
