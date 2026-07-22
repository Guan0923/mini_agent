"""Fixed execution-plan workflow."""

from __future__ import annotations

from mini_agent.domain import AssistantMessage, ExecutionPlan, PlanningError, PlanStep
from mini_agent.planning import PlannerCapabilities

from ...core.context import AgentRuntime
from ...core.events import RuntimeEvent
from ..lifecycle.cancellation import cancel_if_requested
from ..lifecycle.outcomes import fail_run, planning_failure_data, record_plan_feedback
from ..steps import ToolStepExecutor, ToolStepResult
from .budgets import _claim_model_turn, _fail_for_budget, _plan_step_snapshots, _publish_plan_progress
from .common import _finish_assistant, _model_text_stream, _publish, _publish_repairs, _truncate


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
        if not _claim_model_turn(runtime, "plan"):
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
        remaining = runtime.state.runner_settings.max_tool_calls - len(runtime.run.actions)
        if len(plan.steps) > remaining:
            _fail_for_budget(
                runtime,
                "tool_calls",
                f"the plan requires {len(plan.steps)} tool calls, but only {remaining} remained.",
            )
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
        if not _claim_model_turn(runtime, "replan"):
            return False
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
