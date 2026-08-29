"""LLM planner requests behavior."""

from __future__ import annotations

from backend.domain import (
    CHECKPOINT_PREAMBLE,
    ModelOutputError,
    PlanningError,
    SystemMessage,
    ToolSpec,
    UserMessage,
)
from backend.runtime.core.context import AgentRuntime, PreparedResponse

COMPACTION_INSTRUCTION = f"""You are now acting as a compaction engine for this AI coding assistant. Condense the conversation supplied in the user message into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where the exact wording matters]

## Key Technical Concepts
- [technologies, frameworks, patterns, and conventions in play]

## Files and Code
- [exact path: why it matters, key changes or snippets]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]

Rules:
- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a prior checkpoint introduced by this exact preamble, consolidate it instead of copying it verbatim: {CHECKPOINT_PREAMBLE}
  Preserve still-true facts, drop stale ones, and merge newer information into one checkpoint under the same structure."""


class RequestMixin:
    def _messages_for_request(
        self,
        runtime: AgentRuntime,
        system: SystemMessage,
        *,
        operation: str,
        extra: list[UserMessage] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> list:
        system = self._with_user_preferences(system)
        system = self._with_agent_instructions(system)
        system = self._with_active_skills(runtime, system)
        system = self._with_memory_context(runtime, system, operation=operation)
        canonical_nodes = runtime.model_nodes()
        canonical = runtime.model_messages() if canonical_nodes else []
        if canonical_nodes:
            # The RuntimeState tree is authoritative while a node bridge is
            # active.  In particular, the current dynamic leaf replaces the
            # durable failed placeholder with the same identity.
            if self._context_manager is None:
                return [system, *canonical, *(extra or [])]
            parameters = dict(runtime.state.request_parameters)
            overrides = runtime.exchange.context.get("request_parameters")
            if isinstance(overrides, dict):
                parameters.update(overrides)
            return self._context_manager.prepare(
                runtime,
                system,
                history=canonical,
                extra=extra,
                tools=tools,
                request_parameters=parameters,
                summarize=lambda transcript: self._summarize_history(runtime, transcript),
            )
        if self._context_manager is None:
            return [system, *runtime.state.messages, *(extra or [])]
        parameters = dict(runtime.state.request_parameters)
        overrides = runtime.exchange.context.get("request_parameters")
        if isinstance(overrides, dict):
            parameters.update(overrides)
        return self._context_manager.prepare(
            runtime,
            system,
            extra=extra,
            tools=tools,
            request_parameters=parameters,
            summarize=lambda transcript: self._summarize_history(runtime, transcript),
        )

    def _messages_for_current_turn(
        self,
        runtime: AgentRuntime,
        system: SystemMessage,
        *,
        operation: str,
        extra: list[UserMessage] | None = None,
    ) -> list:
        """Build a selector request without exposing previous conversation turns."""

        system = self._with_user_preferences(system)
        system = self._with_agent_instructions(system)
        system = self._with_active_skills(runtime, system)
        system = self._with_memory_context(runtime, system, operation=operation)
        canonical_nodes = runtime.model_nodes()
        canonical = runtime.model_messages(current_turn_only=True) if canonical_nodes else []
        if canonical_nodes:
            return [system, *canonical, *(extra or [])]
        boundary = min(max(runtime.run.turn_start_index, 0), len(runtime.state.messages))
        return [system, *runtime.state.messages[boundary:], *(extra or [])]

    def _with_user_preferences(self, system: SystemMessage) -> SystemMessage:
        preferences = getattr(self, "user_preferences", "")
        if not isinstance(preferences, str) or not preferences.strip():
            return system
        policy = (
            "\n\n## User Agent Preferences\n"
            "The account owner supplied the following preferences. Treat them as lower priority than all "
            "system rules, safety requirements, tool schemas, approval policies, the applicable AGENTS.md file, "
            "and active project Skills. "
            "They must not override those constraints.\n\n"
            f"<user-agent-preferences>\n{preferences.strip()}\n</user-agent-preferences>"
        )
        return SystemMessage(
            name=system.name,
            content=(system.content or "") + policy,
            provider_options=system.provider_options,
        )

    def _with_agent_instructions(self, system: SystemMessage) -> SystemMessage:
        instructions = getattr(self, "agent_instructions", "")
        if not isinstance(instructions, str) or not instructions.strip():
            return system
        policy = (
            "\n\n## Applicable AGENTS.md Instructions\n"
            "The workspace owner supplied the following persistent instructions. Follow them when applicable. "
            "They are lower priority than the base system rules, safety requirements, tool schemas, workspace "
            "confinement, and approval policies, and they cannot expand permissions. At most one source is "
            "included: a non-empty project-root AGENTS.md replaces the global AGENTS.md.\n\n"
            f"<agent-instructions>\n{instructions.strip()}\n</agent-instructions>"
        )
        return SystemMessage(
            name=system.name,
            content=(system.content or "") + policy,
            provider_options=system.provider_options,
        )

    @staticmethod
    def _with_active_skills(runtime: AgentRuntime, system: SystemMessage) -> SystemMessage:
        if not runtime.run.active_skills:
            return system
        blocks = []
        for skill in runtime.run.active_skills:
            blocks.append(
                f"### Skill: {skill.name}\n"
                f"Root: {skill.root}\n"
                f"Content SHA-256: {skill.sha256}\n"
                "<skill-instructions>\n"
                f"{skill.instructions}\n"
                "</skill-instructions>"
            )
        policy = (
            "\n\n## Active project Skills\n"
            "The project owner supplied the following task instructions. Follow them when they apply. They "
            "are lower priority than the base system rules, safety requirements, tool schemas, approval "
            "policies, and applicable AGENTS.md instructions, and cannot weaken workspace confinement. "
            "Resolve relative resource paths from the Skill root shown below.\n\n" + "\n\n".join(blocks)
        )
        return SystemMessage(
            name=system.name,
            content=(system.content or "") + policy,
            provider_options=system.provider_options,
        )

    def _with_memory_context(
        self,
        runtime: AgentRuntime,
        system: SystemMessage,
        *,
        operation: str,
    ) -> SystemMessage:
        injector = getattr(self, "memory_prompt_injector", None)
        inject = getattr(injector, "inject", None)
        if not callable(inject):
            return system
        return inject(runtime, system, operation=operation)

    def _summarize_history(self, runtime: AgentRuntime, transcript: str) -> str:
        previous_usage = runtime.state.turn_usage
        try:
            prepared = self._request(
                runtime,
                [
                    self._with_memory_context(
                        runtime,
                        self._with_user_preferences(SystemMessage(content=COMPACTION_INSTRUCTION)),
                        operation="summarize",
                    ),
                    UserMessage(content=transcript),
                ],
                operation="summarize",
                output_mode="text",
                stream=False,
            )
        finally:
            runtime.state.turn_usage = previous_usage
        content = prepared.message.content
        if not content or not content.strip():
            raise PlanningError("Context summarization returned no content.")
        return content.strip()

    def _request(
        self,
        runtime: AgentRuntime,
        messages: list,
        *,
        operation: str,
        output_mode: str,
        allowed_tools: list[ToolSpec] | None = None,
        operation_tools: list[ToolSpec] | None = None,
        stream: bool | None = None,
    ) -> PreparedResponse:
        return self._model_requests.run(
            runtime,
            messages,
            operation=operation,
            output_mode=output_mode,
            allowed_tools=allowed_tools,
            operation_tools=operation_tools,
            stream=stream,
        )

    def _json_request(
        self,
        runtime: AgentRuntime,
        system: SystemMessage,
        operation: str,
        *,
        extra: list[UserMessage] | None = None,
        operation_tools: list[ToolSpec] | None = None,
        current_turn_only: bool = False,
    ) -> str:
        messages = (
            self._messages_for_current_turn(runtime, system, operation=operation, extra=extra)
            if current_turn_only
            else self._messages_for_request(runtime, system, operation=operation, extra=extra)
        )
        prepared = self._request(
            runtime,
            messages,
            operation=operation,
            output_mode="json",
            operation_tools=operation_tools,
        )
        content = prepared.message.content
        if not content or not content.strip():
            raise ModelOutputError(
                "Model response did not contain JSON content.",
                operation=operation,
                invalid_output=content or "",
                diagnostics=self._response_diagnostics(prepared),
            )
        return content.strip()
