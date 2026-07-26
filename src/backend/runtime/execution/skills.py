"""Select and snapshot project Skills before run routing."""

from __future__ import annotations

from backend.domain import PlanningError
from backend.planning import PlannerCapabilities
from backend.skills import SkillCatalog, SkillConfigurationError

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent
from .lifecycle.outcomes import fail_run, planning_failure_data
from .workflows import _claim_model_turn, _publish_repairs


class SkillActivator:
    """Activate one stable Skill set for the complete run."""

    def activate(self, runtime: AgentRuntime) -> bool:
        run = runtime.run
        if run.active_skills:
            names = [skill.name for skill in run.active_skills]
            self._publish(runtime, names, [], [], source="handoff")
            return True

        catalog = runtime.services.skill_catalog
        if not isinstance(catalog, SkillCatalog) or not catalog:
            return True

        explicit = set(catalog.explicit_names(run.task))
        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        selector = capabilities.skill_selector
        if selector is None:
            if explicit:
                fail_run(runtime, "Skill execution requires the LLM planner.")
                return False
            return True

        if not _claim_model_turn(runtime, "skill_selection"):
            return False
        try:
            selection = selector.select_skills(runtime)
            automatic = set(selection.names)
            snapshots = catalog.snapshots(explicit | automatic)
        except (PlanningError, SkillConfigurationError) as exc:
            _publish_repairs(runtime, capabilities)
            fail_run(
                runtime,
                f"Skill selection failed: {exc}",
                **planning_failure_data(exc, capabilities.name),
            )
            return False
        _publish_repairs(runtime, capabilities)

        run.active_skills = snapshots
        names = [skill.name for skill in snapshots]
        self._publish(
            runtime,
            names,
            [name for name in catalog.names() if name in explicit],
            [name for name in catalog.names() if name in automatic],
            source="llm",
        )
        runtime.save()
        return True

    @staticmethod
    def _publish(
        runtime: AgentRuntime,
        names: list[str],
        explicit: list[str],
        automatic: list[str],
        *,
        source: str,
    ) -> None:
        data = {
            "skills": names,
            "explicit": explicit,
            "automatic": automatic,
            "source": source,
        }
        message = ", ".join(names) if names else "none"
        runtime.run.add_event("skills_selected", "Skills selected", **data)
        publish = runtime.services.publish or (lambda _event: None)
        publish(RuntimeEvent("skills_selected", message, data))
