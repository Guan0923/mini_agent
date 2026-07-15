"""Independent implementations of the supported execution workflows."""

from __future__ import annotations

from collections.abc import Callable

from mini_agent.domain import ExecutionPlan, PlanStep, RunState
from mini_agent.planning import Planner, PlanningError
from mini_agent.providers import ModelRequestError

from .contracts import EventHandler, InterruptHandler
from .events import RuntimeEvent
from .outcomes import cancel_run, complete_run, fail_run, record_plan_feedback
from .steps import ToolStepExecutor, ToolStepResult

ReasoningHandler = Callable[[str], None]


def _reasoning_stream(publish: EventHandler) -> tuple[ReasoningHandler, Callable[[], bool]]:
    """Create an event publisher and a closer for optional provider reasoning streams."""
    streamed = False

    def on_reasoning(chunk: str) -> None:
        nonlocal streamed
        if not streamed:
            publish(RuntimeEvent("thinking_start"))
            streamed = True
        publish(RuntimeEvent("thinking_delta", chunk))

    def close() -> bool:
        if streamed:
            publish(RuntimeEvent("thinking_end"))
        return streamed

    return on_reasoning, close


class ReactiveWorkflow:
    """Repeatedly request one action, execute it, and feed its result back to the planner."""

    def __init__(self, planner: Planner, steps: ToolStepExecutor, max_actions: int) -> None:
        self._planner = planner
        self._steps = steps
        self._max_actions = max_actions

    def run(
        self,
        state: RunState,
        history: list[dict[str, str]],
        conversation: list[dict[str, str]] | None,
        publish: EventHandler,
        interrupt: InterruptHandler,
    ) -> RunState:
        for _ in range(max(0, self._max_actions - len(state.actions))):
            on_reasoning, close_reasoning = _reasoning_stream(publish)
            try:
                action = self._planner.decide(history, state.mode, on_reasoning=on_reasoning)
            except (ModelRequestError, PlanningError) as exc:
                close_reasoning()
                return fail_run(state, publish, f"Decision failed: {exc}", planner=self._planner.name)

            state.actions.append(action)
            state.add_event("model", "Model action validated", action_type=action.type, mode=state.mode)
            streamed_reasoning = close_reasoning()
            if action.reasoning:
                state.add_event("reasoning", "Model reasoning", content=action.reasoning)
                if not streamed_reasoning:
                    publish(RuntimeEvent("thinking_start"))
                    publish(RuntimeEvent("thinking_delta", action.reasoning))
                    publish(RuntimeEvent("thinking_end"))
            if action.type == "final_answer":
                return complete_run(state, action.answer or "", conversation, publish)

            outcome = self._steps.execute(state, action, publish, interrupt)
            if outcome.interrupt is not None:
                if outcome.interrupt.choice == "cancel":
                    return cancel_run(state, publish)
                if record_plan_feedback(state, history, outcome.interrupt.supplement, publish) is None:
                    return state
                continue
            if not outcome.success:
                return fail_run(state, publish, f"Stopped: {action.tool} failed: {outcome.error}")
            assert action.tool is not None and outcome.output is not None
            history.extend(
                [
                    {"role": "assistant", "content": f"[Tool call] {action.tool} {action.arguments}"},
                    {"role": "user", "content": f"[Tool result]\n{outcome.output}"},
                ]
            )
        return fail_run(state, publish, f"Stopped after {self._max_actions} actions without a final answer.")


class PlanWorkflow:
    """Shared plan creation, validation, state recording, and presentation behavior."""

    def __init__(self, planner: Planner, steps: ToolStepExecutor, max_actions: int) -> None:
        self._planner = planner
        self._steps = steps
        self._max_actions = max_actions

    def _create_plan(
        self,
        state: RunState,
        history: list[dict[str, str]],
        publish: EventHandler,
        creator_name: str = "create_plan",
    ) -> ExecutionPlan | None:
        create_plan = getattr(self._planner, creator_name, None)
        if not callable(create_plan):
            fail_run(state, publish, f"Planner {self._planner.name!r} does not support {creator_name}.")
            return None
        on_reasoning, close_reasoning = _reasoning_stream(publish)
        try:
            return create_plan(history, state.mode, on_reasoning=on_reasoning)
        except (ModelRequestError, PlanningError) as exc:
            fail_run(state, publish, f"Plan creation failed: {exc}", planner=self._planner.name)
            return None
        finally:
            close_reasoning()

    def _activate_plan(
        self,
        state: RunState,
        plan: ExecutionPlan,
        publish: EventHandler,
        *,
        event_kind: str = "plan",
        message: str = "Execution plan created",
    ) -> None:
        state.plan = plan
        state.add_event(event_kind, message, revision=plan.revision, goal=plan.goal, step_count=len(plan.steps))
        publish(RuntimeEvent(event_kind, self._format_plan(plan), {"revision": plan.revision}))

    def _validate_plan(self, state: RunState, plan: ExecutionPlan, allowed_steps: int, publish: EventHandler) -> bool:
        if len(plan.steps) > allowed_steps:
            fail_run(
                state,
                publish,
                f"Plan has {len(plan.steps)} steps but only {allowed_steps} actions remain.",
            )
            return False
        invalid_steps = [step.id for step in plan.steps if step.action.type != "tool_call" or not step.action.tool]
        if invalid_steps:
            fail_run(state, publish, f"Execution plan contains non-tool steps: {', '.join(invalid_steps)}.")
            return False
        return True

    def prepare(self, state: RunState, history: list[dict[str, str]], publish: EventHandler) -> ExecutionPlan | None:
        """Create and validate a plan without executing any of its steps."""
        if state.plan is not None:
            return state.plan
        plan = self._create_plan(state, history, publish)
        if plan is None:
            return None
        self._activate_plan(state, plan, publish)
        if not self._validate_plan(state, plan, self._max_actions, publish):
            return None
        return plan

    def revise_with_feedback(
        self,
        state: RunState,
        history: list[dict[str, str]],
        supplement: str | None,
        publish: EventHandler,
    ) -> bool:
        """Regenerate the active plan after explicit human feedback."""
        feedback = record_plan_feedback(state, history, supplement, publish)
        if feedback is None:
            return False
        previous = state.plan
        replacement = self._create_feedback_revision(state, history, previous, feedback, publish)
        if replacement is None:
            return False
        if previous is not None:
            replacement.revision = previous.revision + 1
            for step in previous.steps:
                if step.status in {"pending", "running"}:
                    step.status = "superseded"
            state.plan_history.append(previous)
        state.plan = replacement
        if not self._validate_plan(state, replacement, self._max_actions - len(state.actions), publish):
            return False
        state.add_event(
            "replan_applied",
            "Plan revised from human feedback",
            revision=replacement.revision,
            supplement=feedback,
            step_count=len(replacement.steps),
        )
        publish(RuntimeEvent("replan_applied", self._format_plan(replacement), {"revision": replacement.revision}))
        return True

    def _create_feedback_revision(
        self,
        state: RunState,
        history: list[dict[str, str]],
        previous: ExecutionPlan | None,
        feedback: str,
        publish: EventHandler,
    ) -> ExecutionPlan | None:
        """Prefer a planner's remaining-work replan capability for human feedback."""
        replan = getattr(self._planner, "replan", None)
        if previous is None or not callable(replan):
            return self._create_plan(state, history, publish)

        reason = f"Human plan feedback: {feedback}"
        state.add_event("replan_requested", "Plan revision requested by user", revision=previous.revision, reason=reason)
        publish(RuntimeEvent("replan_requested", reason, {"revision": previous.revision}))
        on_reasoning, close_reasoning = _reasoning_stream(publish)
        try:
            return replan(history, previous, reason, on_reasoning=on_reasoning)
        except (ModelRequestError, PlanningError) as exc:
            fail_run(state, publish, f"Plan revision failed: {exc}", planner=self._planner.name)
            return None
        finally:
            close_reasoning()

    @staticmethod
    def _discard_unexecuted_action(state: RunState, step: PlanStep) -> None:
        """Do not charge a plan step against the action budget before approval is granted."""
        if state.actions and state.actions[-1] == step.action:
            state.actions.pop()

    @staticmethod
    def _begin_step(state: RunState, step: PlanStep) -> None:
        step.status = "running"
        action = step.action
        state.actions.append(action)
        state.add_event(
            "model",
            "Planned action validated",
            action_type=action.type,
            mode=state.mode,
            plan_revision=state.plan.revision if state.plan else None,
            plan_step=step.id,
        )

    @staticmethod
    def _format_plan(plan: ExecutionPlan) -> str:
        header = f"Plan v{plan.revision}: {plan.goal}"
        if not plan.steps:
            return header
        lines = [header]
        lines.extend(f"{index}. {step.description}" for index, step in enumerate(plan.steps, start=1))
        return "\n".join(lines)

    @staticmethod
    def _format_completion(
        plans: list[ExecutionPlan], replan_count: int = 0, *, dynamic: bool = False
    ) -> str:
        prefix = "Dynamic plan completed" if dynamic else "Execution plan completed"
        lines = [f"{prefix} after {replan_count} replans:"] if dynamic else [f"{prefix}:"]
        completed = [
            (plan.revision, step)
            for plan in plans
            for step in plan.steps
            if step.status == "completed"
        ]
        lines.extend(
            f"v{revision}.{step.id} {step.description}: {step.result or 'completed'}"
            for revision, step in completed
        )
        return "\n".join(lines)


class PlanExecuteWorkflow(PlanWorkflow):
    """Generate one fixed executable plan, then run its steps in order."""

    def run(
        self,
        state: RunState,
        history: list[dict[str, str]],
        conversation: list[dict[str, str]] | None,
        publish: EventHandler,
        interrupt: InterruptHandler,
    ) -> RunState:
        plan = self.prepare(state, history, publish)
        if plan is None:
            return state
        if not plan.steps:
            return complete_run(state, plan.final_answer or "", conversation, publish)

        for step in plan.steps:
            if step.status in {"completed", "superseded"}:
                continue
            self._begin_step(state, step)
            outcome = self._steps.execute(state, step.action, publish, interrupt)
            if outcome.interrupt is not None:
                self._discard_unexecuted_action(state, step)
                step.status = "pending"
                if outcome.interrupt.choice == "cancel":
                    return cancel_run(state, publish)
                if not self.revise_with_feedback(state, history, outcome.interrupt.supplement, publish):
                    return state
                return self.run(state, history, conversation, publish, interrupt)
            if not outcome.success:
                step.status = "failed"
                step.result = outcome.error
                return fail_run(state, publish, f"Stopped: {step.action.tool} failed: {outcome.error}")
            step.status = "completed"
            step.result = outcome.output

        return complete_run(state, self._format_completion([plan]), conversation, publish)


class DynamicReplanWorkflow(PlanWorkflow):
    """Execute a plan while replacing only unfinished work after failures or deviations."""

    def __init__(self, planner: Planner, steps: ToolStepExecutor, max_actions: int, max_replans: int) -> None:
        super().__init__(planner, steps, max_actions)
        self._max_replans = max_replans

    def run(
        self,
        state: RunState,
        history: list[dict[str, str]],
        conversation: list[dict[str, str]] | None,
        publish: EventHandler,
        interrupt: InterruptHandler,
    ) -> RunState:
        if not self._supports_dynamic_replanning(state, publish):
            return state
        creator_name = "create_dynamic_plan" if callable(getattr(self._planner, "create_dynamic_plan", None)) else "create_plan"
        if state.plan is None:
            plan = self._create_plan(state, history, publish, creator_name=creator_name)
            if plan is None:
                return state
            self._activate_plan(state, plan, publish)
            if not self._validate_plan(state, plan, self._max_actions, publish):
                return state

        while state.plan is not None:
            active_plan = state.plan
            step = next((candidate for candidate in active_plan.steps if candidate.status == "pending"), None)
            if step is None:
                if not active_plan.steps:
                    return complete_run(state, active_plan.final_answer or "", conversation, publish)
                plans = [*state.plan_history, active_plan]
                return complete_run(
                    state,
                    self._format_completion(plans, state.replan_count, dynamic=True),
                    conversation,
                    publish,
                )
            if len(state.actions) >= self._max_actions:
                return fail_run(state, publish, f"Stopped after {self._max_actions} actions.")

            self._begin_step(state, step)
            outcome = self._steps.execute(state, step.action, publish, interrupt)
            if outcome.interrupt is not None:
                self._discard_unexecuted_action(state, step)
                step.status = "pending"
                if outcome.interrupt.choice == "cancel":
                    return cancel_run(state, publish)
                if not self.revise_with_feedback(state, history, outcome.interrupt.supplement, publish):
                    return state
                continue
            if outcome.success:
                step.status = "completed"
                step.result = outcome.output
                reason = self._evaluate_step(state, history, active_plan, step, outcome, publish)
                if state.status == "failed":
                    return state
                if reason is not None:
                    if not self._replace_remaining_plan(state, history, reason, publish):
                        return state
                    continue
                if not any(candidate.status == "pending" for candidate in active_plan.steps):
                    plans = [*state.plan_history, active_plan]
                    return complete_run(
                        state,
                        self._format_completion(plans, state.replan_count, dynamic=True),
                        conversation,
                        publish,
                    )
                continue
            else:
                step.status = "failed"
                step.result = outcome.error
                reason = f"Step {step.id} failed: {outcome.error}"

            if not self._replace_remaining_plan(state, history, reason, publish):
                return state

        return fail_run(state, publish, "Dynamic execution ended without an active plan.")

    def _supports_dynamic_replanning(self, state: RunState, publish: EventHandler) -> bool:
        if not callable(getattr(self._planner, "evaluate_step", None)) or not callable(getattr(self._planner, "replan", None)):
            fail_run(state, publish, f"Planner {self._planner.name!r} does not support dynamic_replan.")
            return False
        return True

    def _evaluate_step(
        self,
        state: RunState,
        history: list[dict[str, str]],
        plan: ExecutionPlan,
        step: PlanStep,
        outcome: ToolStepResult,
        publish: EventHandler,
    ) -> str | None:
        assert outcome.output is not None
        evaluate_step = getattr(self._planner, "evaluate_step")
        try:
            evaluation = evaluate_step(history, plan, step, outcome.output)
        except (ModelRequestError, PlanningError) as exc:
            fail_run(state, publish, f"Step evaluation failed: {exc}", planner=self._planner.name)
            return None
        if evaluation.decision == "continue":
            return None
        return evaluation.reason

    def _replace_remaining_plan(
        self,
        state: RunState,
        history: list[dict[str, str]],
        reason: str,
        publish: EventHandler,
    ) -> bool:
        active_plan = state.plan
        assert active_plan is not None
        state.add_event("replan_requested", "Replan requested", revision=active_plan.revision, reason=reason)
        publish(RuntimeEvent("replan_requested", reason, {"revision": active_plan.revision}))
        if state.replan_count >= self._max_replans:
            fail_run(state, publish, f"Stopped after {self._max_replans} replans: {reason}")
            return False

        replan = getattr(self._planner, "replan")
        on_reasoning, close_reasoning = _reasoning_stream(publish)
        try:
            replacement: ExecutionPlan = replan(history, active_plan, reason, on_reasoning=on_reasoning)
        except (ModelRequestError, PlanningError) as exc:
            fail_run(state, publish, f"Replan failed: {exc}", planner=self._planner.name)
            return False
        finally:
            close_reasoning()

        replacement.revision = active_plan.revision + 1
        if not self._validate_plan(state, replacement, self._max_actions - len(state.actions), publish):
            return False
        for step in active_plan.steps:
            if step.status == "pending":
                step.status = "superseded"
        state.plan_history.append(active_plan)
        state.plan = replacement
        state.replan_count += 1
        state.add_event(
            "replan_applied",
            "Replacement plan applied",
            revision=replacement.revision,
            reason=reason,
            step_count=len(replacement.steps),
        )
        publish(RuntimeEvent("replan_applied", self._format_plan(replacement), {"revision": replacement.revision, "reason": reason}))
        return True
