"""LLM planner selection behavior."""

from __future__ import annotations

import json

from backend.domain import (
    ModelOutputError,
    SkillSelection,
    SystemMessage,
    UserMessage,
)
from backend.runtime.core.context import AgentRuntime
from backend.skills import SkillCatalog


class SelectionMixin:
    def select_skills(self, runtime: AgentRuntime) -> SkillSelection:
        return self._with_output_repair(
            runtime,
            "skill_selection",
            lambda correction: self._select_skills_once(runtime, correction),
        )

    def _select_skills_once(
        self,
        runtime: AgentRuntime,
        correction: UserMessage | None = None,
    ) -> SkillSelection:
        catalog = runtime.services.skill_catalog
        definitions = catalog.definitions() if isinstance(catalog, SkillCatalog) else ()
        metadata: list[dict[str, object]] = []
        for skill in definitions:
            entry: dict[str, object] = {"name": skill.name, "description": skill.description}
            if skill.metadata:
                entry["metadata"] = dict(skill.metadata)
            if skill.allowed_tools:
                entry["allowed-tools"] = skill.allowed_tools
            metadata.append(entry)
        raw = self._json_request(
            runtime,
            SystemMessage(
                content=(
                    "Select the project Skills whose instructions are materially relevant to the current user task.\n\n"
                    "Choose no Skill when none is needed. Select only from this catalog:\n"
                    f"{json.dumps(metadata, ensure_ascii=False)}\n\n"
                    'Return JSON only as {"skills":["skill-name"]}. Do not return explanations or unknown names.'
                )
            ),
            "skill_selection",
            extra=[correction] if correction is not None else None,
            current_turn_only=True,
        )
        payload = self._json_object(raw, "skill_selection")
        if set(payload) != {"skills"}:
            raise ModelOutputError(
                "Skill selection must contain only the skills field.",
                operation="skill_selection",
                invalid_output=raw,
            )
        names = payload["skills"]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ModelOutputError(
                "Skill selection must be a list of Skill names.",
                operation="skill_selection",
                invalid_output=raw,
            )
        allowed = {skill.name for skill in definitions}
        unknown = sorted(set(names) - allowed)
        if unknown:
            raise ModelOutputError(
                f"Skill selection contains unknown names: {', '.join(unknown)}.",
                operation="skill_selection",
                invalid_output=raw,
            )
        return SkillSelection(tuple(names))
