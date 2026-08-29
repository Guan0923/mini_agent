"""LLM planner decisions behavior."""

from __future__ import annotations

from backend.domain import (
    AssistantMessage,
    ModelOutputError,
    PlanningError,
    SystemMessage,
    UserMessage,
)
from backend.runtime.conversation.user_input import REQUEST_USER_INPUT_NAME, REQUEST_USER_INPUT_SPEC
from backend.runtime.core.context import AgentRuntime
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME, REQUEST_PLAN_REVIEW_SPEC

from ..prompts import compose_system_prompt


class DecisionMixin:
    def decide(self, runtime: AgentRuntime) -> AssistantMessage:
        return self._with_output_repair(
            runtime,
            "decision",
            lambda correction: self._decide_once(runtime, correction),
        )

    def _decide_once(self, runtime: AgentRuntime, correction: UserMessage | None = None) -> AssistantMessage:
        allowed = self.read_only_tool_specs if runtime.run.mode == "plan" else self.tool_specs
        if runtime.run.mode == "plan":
            reserved_names = {REQUEST_USER_INPUT_NAME, REQUEST_PLAN_REVIEW_NAME}
            collisions = sorted(spec.name for spec in allowed if spec.name in reserved_names)
            if collisions:
                names = ", ".join(repr(name) for name in collisions)
                verb = "is" if len(collisions) == 1 else "are"
                raise PlanningError(f"{names} {verb} reserved for the Plan-mode control protocol.")
            allowed = [*allowed, REQUEST_USER_INPUT_SPEC, REQUEST_PLAN_REVIEW_SPEC]
        system = SystemMessage(content=compose_system_prompt(runtime.run.mode))
        prepared = self._request(
            runtime,
            self._messages_for_request(
                runtime,
                system,
                operation="decision",
                extra=[correction] if correction is not None else None,
                tools=allowed,
            ),
            operation="decision",
            output_mode="tools",
            allowed_tools=allowed,
        )
        message = prepared.message
        allowed_names = {spec.name for spec in runtime.exchange.operation_tools}
        for tool in message.tool_messages:
            if tool.name not in allowed_names:
                raise ModelOutputError(
                    f"Model requested unavailable tool: {tool.name!r}.",
                    operation="decision",
                    invalid_output=self._message_preview(message),
                )
        if not message.tool_messages and not (message.content and message.content.strip()):
            raise ModelOutputError(
                "Model returned neither text nor a tool call.",
                operation="decision",
                invalid_output=self._message_preview(message),
            )
        return message

    def finalize(self, runtime: AgentRuntime, reason: str) -> AssistantMessage:
        system = SystemMessage(
            content=(
                "The run cannot make more planning or tool decisions because its execution budget is exhausted. "
                "Produce the final user-facing response from the conversation and completed tool results. "
                "Be concise and truthful: summarize completed work, identify anything unfinished, explain the "
                "budget limit, and state how the user can continue. Do not claim the task succeeded and do not "
                f"request or describe another tool call. Budget reason: {reason}" + self._UNTRUSTED_TOOL_RESULT_POLICY
            )
        )
        prepared = self._request(
            runtime,
            self._messages_for_request(runtime, system, operation="finalize", tools=[]),
            operation="finalize",
            output_mode="text",
            allowed_tools=[],
            stream=False,
        )
        message = prepared.message
        if message.tool_messages or not (message.content and message.content.strip()):
            raise ModelOutputError(
                "Budget finalization must return non-empty text without tool calls.",
                operation="finalize",
                invalid_output=self._message_preview(message),
            )
        return message
