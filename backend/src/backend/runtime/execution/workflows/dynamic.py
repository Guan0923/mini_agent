"""Dynamic replanning workflow."""

from __future__ import annotations

from backend.domain import AssistantMessage, PlanningError
from backend.planning import PlannerCapabilities

from ...conversation.steering import apply_steering, collect_steering, consume_steering
from ...core.context import AgentRuntime
from ...core.events import RuntimeEvent
from ..lifecycle.cancellation import cancel_if_requested
from ..lifecycle.outcomes import cancel_run, complete_run, fail_run, planning_failure_data
from .budgets import _claim_model_turn, _ensure_tool_budget, _plan_step_snapshots, _publish_plan_progress
from .common import _publish, _publish_repairs
from .plan import PlanWorkflow


class DynamicReplanWorkflow(PlanWorkflow):
    def resume(self, runtime: AgentRuntime):
        """Replan around indeterminate steps before normal dynamic execution."""

        plan = runtime.run.plan
        if plan is not None:
            recovery_steps = [step for step in plan.steps if step.status in {"failed", "indeterminate"}]
            if recovery_steps:
                capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
                reason = "Interrupted plan steps require a fresh decision: " + ", ".join(
                    f"{step.id} ({step.status})" for step in recovery_steps
                )
                if not self._replace(runtime, reason, capabilities):
                    return runtime.run
        return self.run(runtime)

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
            if not _ensure_tool_budget(runtime):
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
            if runtime.run.status != "running":
                return runtime.run
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
                if not _claim_model_turn(runtime, "evaluate"):
                    return runtime.run
                try:
                    evaluation = capabilities.dynamic_replanner.evaluate_step(runtime)
                except PlanningError as exc:
                    _publish_repairs(runtime, capabilities)
                    if cancel_if_requested(runtime):
                        return runtime.run
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
        runtime.exchange.context = {"plan": current, "reason": reason}
        if not _claim_model_turn(runtime, "replan"):
            return False
        try:
            replacement = capabilities.dynamic_replanner.replan(runtime)
        except PlanningError as exc:
            _publish_repairs(runtime, capabilities)
            if cancel_if_requested(runtime):
                return False
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
