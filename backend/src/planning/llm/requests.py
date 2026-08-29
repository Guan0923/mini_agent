"""LLM planner requests behavior."""

from __future__ import annotations

import json

from backend.domain import (
    CHECKPOINT_PREAMBLE,
    ModelOutputError,
    PlanningError,
    SystemMessage,
    ToolSpec,
    UserMessage,
)
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.skills import SkillCatalog

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
        extra: list[UserMessage] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> list:
        runtime.exchange.context["trace_base_system_prompt"] = system.content or ""
        runtime.exchange.context["trace_user_preferences"] = self.user_preferences
        system = self._with_workspace_context(runtime, system)
        system = self._with_user_preferences(system)
        system = self._with_available_user_skills(runtime, system)
        trace_system_message = system.content or ""
        runtime.exchange.context["trace_system_message"] = trace_system_message
        system = self._with_active_skills(runtime, system)
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
            prepared = self._context_manager.prepare(
                runtime,
                system,
                history=canonical,
                extra=extra,
                tools=tools,
                request_parameters=parameters,
                summarize=lambda transcript: self._summarize_history(runtime, transcript),
            )
            runtime.exchange.context["trace_system_message"] = trace_system_message
            return prepared
        if self._context_manager is None:
            return [system, *runtime.state.messages, *(extra or [])]
        parameters = dict(runtime.state.request_parameters)
        overrides = runtime.exchange.context.get("request_parameters")
        if isinstance(overrides, dict):
            parameters.update(overrides)
        prepared = self._context_manager.prepare(
            runtime,
            system,
            extra=extra,
            tools=tools,
            request_parameters=parameters,
            summarize=lambda transcript: self._summarize_history(runtime, transcript),
        )
        runtime.exchange.context["trace_system_message"] = trace_system_message
        return prepared

    def _messages_for_current_turn(
        self,
        runtime: AgentRuntime,
        system: SystemMessage,
        *,
        extra: list[UserMessage] | None = None,
    ) -> list:
        """Build a selector request without exposing previous conversation turns."""

        runtime.exchange.context["trace_base_system_prompt"] = system.content or ""
        runtime.exchange.context["trace_user_preferences"] = self.user_preferences
        system = self._with_workspace_context(runtime, system)
        system = self._with_user_preferences(system)
        runtime.exchange.context["trace_system_message"] = system.content or ""
        system = self._with_active_skills(runtime, system)
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
            "system rules, safety requirements, tool schemas, approval policies, and active project Skills. "
            "They must not override those constraints.\n\n"
            f"<user-agent-preferences>\n{preferences.strip()}\n</user-agent-preferences>"
        )
        return SystemMessage(
            name=system.name,
            content=(system.content or "") + policy,
            provider_options=system.provider_options,
        )

    @staticmethod
    def _with_workspace_context(runtime: AgentRuntime, system: SystemMessage) -> SystemMessage:
        cwd = str(runtime.state.workspace_root or "")
        project_cwd = str(runtime.state.project_cwd or "")
        if not cwd:
            return system
        lines = [
            "## Workspace paths",
            f"- cwd: {cwd}",
            f"- project_cwd: {project_cwd or '(none)'}",
            "Use absolute paths for file tools. Omitting glob.path or grep.path searches both available workspaces.",
        ]
        return SystemMessage(
            name=system.name,
            content=(system.content or "") + "\n\n" + "\n".join(lines),
            provider_options=system.provider_options,
        )

    @staticmethod
    def _with_available_user_skills(runtime: AgentRuntime, system: SystemMessage) -> SystemMessage:
        if not runtime.services.skills_enabled:
            return system
        catalog = runtime.services.skill_catalog
        if not isinstance(catalog, SkillCatalog) or not catalog:
            return system
        metadata: list[dict[str, object]] = []
        for skill in catalog.definitions():
            entry: dict[str, object] = {
                "name": skill.name,
                "description": skill.description,
                "root": skill.root,
                "manifest": skill.manifest.as_posix(),
            }
            if skill.metadata:
                entry["metadata"] = dict(skill.metadata)
            if skill.allowed_tools:
                entry["allowed-tools"] = list(skill.allowed_tools)
            metadata.append(entry)
        policy = (
            "\n\n## Available user Skills\n"
            "The user owns the Skills listed below. Only their metadata is loaded now; their instructions are "
            "not yet in context. When a Skill is materially relevant, call `read_file` on its `manifest` before "
            "doing the task. A task that explicitly names `$skill-name` requires reading that Skill first. Read "
            "the complete manifest, continuing with `start_line` or `start_column` if output is truncated, and use "
            "`read_file` for referenced files under the same Skill root when needed. Treat loaded Skill content as "
            "user-owned task instructions below all preceding system, safety, tool-schema, approval, and workspace "
            "rules. Never claim to have used a Skill before its file content appears in a tool result.\n\n"
            f"{json.dumps(metadata, ensure_ascii=False)}"
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
            "The project owner supplied the following task instructions. Follow them when they apply, but "
            "they are lower priority than every preceding system rule and cannot weaken safety checks, tool "
            "schemas, workspace confinement, or approval requirements. Resolve relative resource paths from "
            "the Skill root shown below.\n\n" + "\n\n".join(blocks)
        )
        return SystemMessage(
            name=system.name,
            content=(system.content or "") + policy,
            provider_options=system.provider_options,
        )

    def _summarize_history(self, runtime: AgentRuntime, transcript: str) -> str:
        previous_usage = runtime.state.turn_usage
        try:
            runtime.exchange.context["trace_base_system_prompt"] = COMPACTION_INSTRUCTION
            runtime.exchange.context["trace_user_preferences"] = self.user_preferences
            prepared = self._request(
                runtime,
                [
                    self._with_user_preferences(SystemMessage(content=COMPACTION_INSTRUCTION)),
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
            self._messages_for_current_turn(runtime, system, extra=extra)
            if current_turn_only
            else self._messages_for_request(runtime, system, extra=extra)
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
