"""LLM planner selection behavior."""

from __future__ import annotations

import json

from mini_agent.domain import (
    ModelOutputError,
    PlanningError,
    SkillSelection,
    StrategySelection,
    SystemMessage,
    UserMessage,
)
from mini_agent.runtime.core.context import AgentRuntime
from mini_agent.skills import SkillCatalog


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
        metadata = [
            {"name": skill.name, "description": skill.description}
            for skill in definitions
        ]
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

    def select_strategy(self, runtime: AgentRuntime) -> StrategySelection:
        return self._with_output_repair(
            runtime,
            "strategy",
            lambda correction: self._select_strategy_once(runtime, correction),
        )

    def _select_strategy_once(self, runtime: AgentRuntime, correction: UserMessage | None = None) -> StrategySelection:
        if runtime.run.mode == "plan":
            return StrategySelection("reactive", "Plan mode supports read-only discussion and optional Plan Review.")
        raw = self._json_request(
            runtime,
            SystemMessage(
                content=(
                    "Analyze the user's task and choose an execution strategy.\n\n"
                    "Use only the current turn supplied in this request. Do not resume or act on an older, "
                    "unfinished request.\n\n"
                    "Consider:\n"
                    "- Task complexity: single straightforward action vs. multiple dependent steps.\n"
                    "- Ambiguity: is the path clear or does it require exploration first?\n"
                    "- Risk: are there destructive operations that warrant a step-by-step approach?\n\n"
                    "Return one JSON object only. Example JSON:\n"
                    '{"strategy":"reactive","reason":"A simple greeting needs no tools."}\n\n'
                    "The required JSON schema is "
                    '{"strategy":"reactive|dynamic_replan","reason":"short explanation"}. '
                    "Choose reactive for simple, single-step, or exploratory tasks. "
                    "Choose dynamic_replan for multi-step work that benefits from a plan "
                    "with step-by-step evaluation."
                )
            ),
            "strategy",
            extra=[correction] if correction is not None else None,
            current_turn_only=True,
        )
        try:
            payload = self._json_object(raw)
            strategy = payload.get("strategy")
            reason = payload.get("reason")
            if strategy not in {"reactive", "dynamic_replan"}:
                raise ModelOutputError(
                    f"Unsupported execution strategy: {strategy!r}.",
                    operation="strategy",
                    invalid_output=raw,
                )
            if not isinstance(reason, str) or not reason.strip():
                raise ModelOutputError(
                    "Strategy reason must be non-empty text.",
                    operation="strategy",
                    invalid_output=raw,
                )
            return StrategySelection(strategy, reason.strip())
        except PlanningError as exc:
            raise exc
