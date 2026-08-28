"""Select and snapshot Skills (user + trusted project) before run routing."""

from __future__ import annotations

from backend.domain import PlanningError
from backend.planning import PlannerCapabilities
from backend.skills import (
    ProjectSkillDefinition,
    ProjectSkillGateResult,
    SkillCatalog,
    SkillConfigurationError,
    SkillDefinition,
)

from ..core.context import AgentRuntime
from ..core.events import RuntimeEvent
from .lifecycle.outcomes import fail_run, planning_failure_data
from .workflows import _publish_repairs


class SkillActivator:
    """Activate one stable Skill set for the complete run.

    User Skills come from the runner catalog.  Trusted project Skills are
    merged per run after the project gate approves each untrusted candidate
    individually; untrusted project Skills stay invisible to the model.
    """

    def __init__(self, project_gate: object | None = None) -> None:
        self.project_gate = project_gate

    def activate(self, runtime: AgentRuntime) -> bool:
        run = runtime.run
        if run.active_skills:
            names = [skill.name for skill in run.active_skills]
            self._publish(runtime, names, [], [], source="handoff")
            return True

        catalog = runtime.services.skill_catalog
        if not isinstance(catalog, SkillCatalog):
            return True

        gate_result = self._prepare_project_skills(runtime)

        try:
            merged = self._merged_catalog(catalog, gate_result.usable)
            explicit = set(merged.explicit_names(run.task))
        except SkillConfigurationError as exc:
            fail_run(runtime, f"Skill activation failed: {exc}")
            return False

        project_names = {skill.name for skill in gate_result.usable}
        user_names = set(catalog.names()) - project_names
        explicit_user = explicit & user_names
        explicit_project = explicit & project_names

        if explicit_user and not runtime.services.skills_enabled:
            rendered = ", ".join(sorted(explicit_user))
            fail_run(runtime, f"User Skill(s) disabled by capabilities.skills: {rendered}.")
            return False

        if explicit_project:
            untrusted = set(gate_result.untrusted_names) & {name for name in explicit}
            if untrusted:
                rendered = ", ".join(sorted(untrusted))
                fail_run(
                    runtime,
                    f"Project Skill(s) not trusted: {rendered}. Approve the Skill before using $skill-name.",
                )
                return False
            try:
                project_catalog = SkillCatalog(tuple(skill for skill in gate_result.usable))
                run.active_skills = project_catalog.snapshots(explicit_project)
            except SkillConfigurationError as exc:
                fail_run(runtime, f"Skill activation failed: {exc}")
                return False
            names = [skill.name for skill in run.active_skills]
            self._publish(runtime, names, names, [], source="explicit")
            runtime.save()
        if explicit:
            # User Skills use progressive disclosure. Their exact manifest path
            # is already in the main Agent request, and the Agent must load it
            # through read_file so the durable tool result becomes the context.
            return True

        if not runtime.services.skill_auto_select or (runtime.services.project_skill_gate or self.project_gate) is None:
            return True

        capabilities = PlannerCapabilities.from_planner(runtime.services.planner)
        selector = capabilities.skill_selector
        if selector is None:
            fail_run(runtime, "Automatic Skill selection requires the LLM planner.")
            return False

        run.skill_selection_calls += 1
        project_catalog = SkillCatalog(tuple(skill for skill in gate_result.usable))
        original_catalog = runtime.services.skill_catalog
        try:
            # Preserve the trusted project-Skill selector without exposing user
            # Skills to the hidden pre-selection request.
            runtime.services.skill_catalog = project_catalog
            selection = selector.select_skills(runtime)
            automatic = set(selection.names)
            snapshots = project_catalog.snapshots(automatic)
        except (PlanningError, SkillConfigurationError) as exc:
            _publish_repairs(runtime, capabilities)
            fail_run(
                runtime,
                f"Skill selection failed: {exc}",
                **planning_failure_data(exc, capabilities.name),
            )
            return False
        finally:
            runtime.services.skill_catalog = original_catalog
        _publish_repairs(runtime, capabilities)

        run.active_skills = snapshots
        names = [skill.name for skill in snapshots]
        self._publish(
            runtime,
            names,
            [],
            [name for name in project_catalog.names() if name in automatic],
            source="llm",
        )
        runtime.save()
        return True

    @staticmethod
    def _merged_catalog(
        user_catalog: SkillCatalog,
        project_definitions: list[ProjectSkillDefinition],
    ) -> SkillCatalog:
        merged: dict[str, SkillDefinition] = {skill.name: skill for skill in user_catalog.definitions()}
        for definition in project_definitions:
            merged[definition.name] = definition
        return SkillCatalog(tuple(merged[name] for name in sorted(merged)))

    def _prepare_project_skills(self, runtime: AgentRuntime) -> ProjectSkillGateResult:
        gate = runtime.services.project_skill_gate or self.project_gate
        if gate is None:
            return ProjectSkillGateResult([], [])
        prepare = getattr(gate, "prepare", None)
        if not callable(prepare):
            return ProjectSkillGateResult([], [])
        try:
            result = prepare(runtime)
        except Exception as exc:
            # A scanning or trust failure must never bypass the gate; treat
            # the whole project layer as unusable for this run.
            if isinstance(exc, SkillConfigurationError):
                self._publish(runtime, [], [], [], source="project_disable")
            return ProjectSkillGateResult([], [])
        if not isinstance(result, ProjectSkillGateResult):
            return ProjectSkillGateResult([], [])
        return result

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
        publish = runtime.services.publish or (lambda _event: None)
        publish(RuntimeEvent("skills_selected", message, data))
