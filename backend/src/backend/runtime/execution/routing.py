"""Resolve execution strategy from AgentRuntime."""

from __future__ import annotations

from backend.domain import ModelOutputError, PlanningError, StrategySelection
from backend.planning import PlannerCapabilities

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent
from .lifecycle.outcomes import fail_run, planning_failure_data
from .workflows import _claim_model_turn, _publish_repairs


class StrategyRouter:
    def resolve(self, runtime: AgentRuntime) -> StrategySelection | None:
        run = runtime.run
        settings = runtime.state.runner_settings
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        fallback_data: dict[str, object] = {}
        if run.mode == "plan":
            selection = StrategySelection("reactive", "Plan mode drafts a proposal for explicit implementation review.")
            source = "mode"
        elif settings.strategy == "auto":
            if capabilities.strategy_selector is None:
                fail_run(runtime, f"Planner {capabilities.name!r} does not support automatic strategy selection.")
                return None
            if not _claim_model_turn(runtime, "strategy"):
                return None
            try:
                selection = capabilities.strategy_selector.select_strategy(runtime)
            except ModelOutputError as exc:
                _publish_repairs(runtime, capabilities)
                attempts = settings.max_model_repairs + 1
                selection = StrategySelection(
                    "reactive",
                    f"Strategy output remained invalid after {attempts} attempts; "
                    f"defaulting to reactive: {exc.validation_error}",
                )
                source = "fallback"
                fallback_data = {
                    "validation_error": exc.validation_error,
                    "attempts": attempts,
                }
            except PlanningError as exc:
                _publish_repairs(runtime, capabilities)
                fail_run(runtime, f"Strategy selection failed: {exc}", **planning_failure_data(exc, capabilities.name))
                return None
            else:
                _publish_repairs(runtime, capabilities)
                if selection.strategy not in {"reactive", "dynamic_replan"}:
                    fail_run(runtime, f"Planner selected unsupported execution strategy: {selection.strategy!r}.")
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
            **fallback_data,
        )
        publish = runtime.services.publish or (lambda _event: None)
        publish(
            RuntimeEvent(
                "strategy",
                selection.strategy,
                {"reason": selection.reason, "source": source, **fallback_data},
            )
        )
        return selection
