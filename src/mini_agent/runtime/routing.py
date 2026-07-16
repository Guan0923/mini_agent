"""Resolve execution strategy from AgentRuntime."""

from __future__ import annotations

from mini_agent.domain import PlanningError, StrategySelection
from mini_agent.planning import PlannerCapabilities

from .context import AgentRuntime
from .events import RuntimeEvent
from .outcomes import fail_run, planning_failure_data


class StrategyRouter:
    def resolve(self, runtime: AgentRuntime) -> StrategySelection | None:
        run = runtime.run
        settings = runtime.state.runner_settings
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        if run.mode == "plan":
            selection = StrategySelection(
                "reactive", "Plan mode drafts a proposal for explicit implementation review."
            )
            source = "mode"
        elif settings.strategy == "auto":
            if capabilities.strategy_selector is None:
                fail_run(runtime, f"Planner {capabilities.name!r} does not support automatic strategy selection.")
                return None
            try:
                selection = capabilities.strategy_selector.select_strategy(runtime)
            except PlanningError as exc:
                fail_run(runtime, f"Strategy selection failed: {exc}", **planning_failure_data(exc, capabilities.name))
                return None
            if selection.strategy == "plan_execute":
                fail_run(runtime, "Automatic strategy selection cannot use experimental plan_execute.")
                return None
            source = "llm" if capabilities.name == "llm" else "planner"
        else:
            selection = StrategySelection(settings.strategy, "Execution strategy forced by configuration.")
            source = "override"
        run.strategy = selection.strategy
        run.strategy_reason = selection.reason
        run.add_event(
            "strategy",
            "Execution strategy selected",
            strategy=selection.strategy,
            reason=selection.reason,
            source=source,
        )
        publish = runtime.services.publish or (lambda _event: None)
        publish(RuntimeEvent("strategy", selection.strategy, {"reason": selection.reason, "source": source}))
        return selection
