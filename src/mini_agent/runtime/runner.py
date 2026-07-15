"""Thin application orchestrator that delegates routing and execution workflows."""

from __future__ import annotations

from mini_agent.domain import RunMode, RunState, StrategyPolicy
from mini_agent.planning import Planner
from mini_agent.tools import ToolError, ToolExecutor

from .checkpointing import CheckpointStore
from .contracts import Confirm, EventHandler, InterruptDecision, InterruptHandler, InterruptRequest
from .events import RuntimeEvent
from .outcomes import cancel_run
from .publisher import RunEventPublisher
from .routing import StrategyRouter
from .steps import ToolStepExecutor
from .workflows import DynamicReplanWorkflow, PlanExecuteWorkflow, ReactiveWorkflow


class AgentRunner:
    """Create run state, resolve a strategy, then delegate the selected workflow."""

    def __init__(
        self,
        planner: Planner,
        tools: ToolExecutor,
        max_retries: int = 1,
        max_actions: int = 8,
        max_replans: int = 2,
        strategy: StrategyPolicy = "auto",
        checkpoints: CheckpointStore | None = None,
    ) -> None:
        self.planner = planner
        self.tools = tools
        self._checkpoints = checkpoints
        steps = ToolStepExecutor(tools, max_retries)
        self._router = StrategyRouter(planner, strategy)
        self._reactive = ReactiveWorkflow(planner, steps, max_actions)
        self._plan_execute = PlanExecuteWorkflow(planner, steps, max_actions)
        self._dynamic_replan = DynamicReplanWorkflow(planner, steps, max_actions, max_replans)

    def run(
        self,
        task: str,
        confirm: Confirm | None = None,
        mode: RunMode = "agent",
        conversation: list[dict[str, str]] | None = None,
        on_event: EventHandler | None = None,
        interrupt: InterruptHandler | None = None,
    ) -> RunState:
        state = RunState(task=task, mode=mode, history=[*(conversation or []), {"role": "user", "content": task}])
        publish = self._publisher(state, on_event)
        state.add_event("run_started", "Run started")
        publish(RuntimeEvent("run_started", "started"))
        handler = interrupt or self._default_interrupt(confirm)
        if state.mode == "plan":
            return self._run_plan_mode(state, conversation, publish, handler)
        return self._run_from_router(state, conversation, publish, handler)

    def _run_from_router(
        self,
        state: RunState,
        conversation: list[dict[str, str]] | None,
        publish: EventHandler,
        interrupt: InterruptHandler,
    ) -> RunState:
        selection = self._router.resolve(state, state.history, publish)
        if selection is None:
            return self._finish_run(state, publish)
        return self._execute(state, conversation, publish, interrupt)

    def _execute(
        self,
        state: RunState,
        conversation: list[dict[str, str]] | None,
        publish: EventHandler,
        interrupt: InterruptHandler,
    ) -> RunState:
        if state.strategy == "plan_execute":
            result = self._plan_execute.run(state, state.history, conversation, publish, interrupt)
        elif state.strategy == "dynamic_replan":
            result = self._dynamic_replan.run(state, state.history, conversation, publish, interrupt)
        else:
            result = self._reactive.run(state, state.history, conversation, publish, interrupt)
        return self._finish_run(result, publish)

    def _run_plan_mode(
        self,
        state: RunState,
        conversation: list[dict[str, str]] | None,
        publish: EventHandler,
        interrupt: InterruptHandler,
    ) -> RunState:
        state.strategy = "plan_execute"
        state.strategy_reason = "Plan mode creates a plan and waits for human approval."
        plan = self._plan_execute.prepare(state, state.history, publish)
        if plan is None:
            return self._finish_run(state, publish)
        while True:
            request = InterruptRequest(
                "plan",
                "Execute this plan in Agent mode?",
                {"run_id": state.run_id, "goal": plan.goal, "steps": [step.description for step in plan.steps]},
            )
            state.add_event("approval_requested", "Plan execution approval requested", interrupt_kind="plan", **request.data)
            publish(RuntimeEvent("approval_requested", request.message, request.data))
            decision = interrupt(request)
            if decision.choice == "cancel":
                cancel_run(state, publish)
                return self._finish_run(state, publish)
            if decision.choice == "supplement":
                if not self._plan_execute.revise_with_feedback(state, state.history, decision.supplement, publish):
                    return self._finish_run(state, publish)
                assert state.plan is not None
                plan = state.plan
                continue
            state.mode = "agent"
            state.add_event("approval_granted", "Plan execution approved", interrupt_kind="plan", **request.data)
            publish(RuntimeEvent("approval_granted", request.message, request.data))
            return self._execute(state, conversation, publish, interrupt)

    def _default_interrupt(self, confirm: Confirm | None) -> InterruptHandler:
        """Preserve one confirmation for mutating tools when no UI adapter is supplied."""

        def decide(request: InterruptRequest) -> InterruptDecision:
            if request.kind == "plan":
                return InterruptDecision("continue")
            tool = request.data.get("tool")
            if not isinstance(tool, str):
                return InterruptDecision("cancel")
            try:
                is_read_only = self.tools.is_read_only(tool)
            except ToolError:
                return InterruptDecision("cancel")
            if is_read_only:
                return InterruptDecision("continue")
            message = f"{tool} requires confirmation before it performs a potentially destructive operation."
            return InterruptDecision("continue" if confirm is not None and confirm(message) else "cancel")

        return decide

    def _publisher(self, state: RunState, on_event: EventHandler | None) -> RunEventPublisher:
        checkpoint = self._checkpoints.save if self._checkpoints is not None else None
        return RunEventPublisher(state, on_event or (lambda _event: None), checkpoint)

    @staticmethod
    def _finish_run(state: RunState, publish: EventHandler) -> RunState:
        state.add_event("run_finished", "Run finished", status=state.status)
        publish(RuntimeEvent("run_finished", state.status, {"final_answer": state.final_answer or ""}))
        return state
