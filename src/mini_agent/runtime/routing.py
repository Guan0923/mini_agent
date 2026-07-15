"""Resolve the execution strategy without coupling policy to a workflow."""

from __future__ import annotations

from mini_agent.domain import RunState, StrategyPolicy, StrategySelection
from mini_agent.planning import Planner, PlanningError
from mini_agent.providers import ModelRequestError

from .contracts import EventHandler
from .events import RuntimeEvent
from .outcomes import fail_run


class StrategyRouter:
    """Selects a validated strategy or applies an explicit test override."""

    def __init__(self, planner: Planner, policy: StrategyPolicy) -> None:
        self._planner = planner
        self._policy = policy

    def resolve(
        self,
        state: RunState,
        history: list[dict[str, str]],
        publish: EventHandler,
    ) -> StrategySelection | None:
        if state.mode == "plan":
            selection = StrategySelection("reactive", "Plan mode requires human approval before execution.")
            source = "mode"
        elif self._policy == "auto":
            select_strategy = getattr(self._planner, "select_strategy", None)
            if not callable(select_strategy):
                fail_run(state, publish, f"Planner {self._planner.name!r} does not support automatic strategy selection.")
                return None
            try:
                selection = select_strategy(history, state.mode)
            except (ModelRequestError, PlanningError) as exc:
                fail_run(state, publish, f"Strategy selection failed: {exc}", planner=self._planner.name)
                return None
            source = "llm" if self._planner.name == "llm" else "planner"
        else:
            selection = StrategySelection(self._policy, "Execution strategy forced by configuration.")
            source = "override"

        state.strategy = selection.strategy
        state.strategy_reason = selection.reason
        state.add_event("strategy", "Execution strategy selected", strategy=selection.strategy, reason=selection.reason, source=source)
        publish(RuntimeEvent("strategy", selection.strategy, {"reason": selection.reason, "source": source}))
        return selection
