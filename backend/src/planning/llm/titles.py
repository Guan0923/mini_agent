"""Dedicated first-message conversation title generation."""

from __future__ import annotations

from backend.domain import PlanningError, SystemMessage, UserMessage
from backend.runtime.core.context import AgentRuntime

from ..prompts import load_title_prompt

TITLE_MAX_CHARS = 10
_TITLE_OUTPUT_TOKENS = 32
_WRAPPERS = (("```", "```"), ("`", "`"), ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))


def normalize_conversation_title(value: str) -> str:
    """Return one whitespace-normalized, unquoted title of at most 10 characters."""

    title = " ".join(value.split()).strip()
    changed = True
    while changed and title:
        changed = False
        for opening, closing in _WRAPPERS:
            if len(title) >= len(opening) + len(closing) and title.startswith(opening) and title.endswith(closing):
                title = title[len(opening) : -len(closing)].strip()
                changed = True
                break
    return title[:TITLE_MAX_CHARS]


class TitleMixin:
    """Issue one isolated, provider-neutral title request."""

    def generate_title(self, runtime: AgentRuntime, first_user_text: str) -> str:
        previous_usage = runtime.state.turn_usage
        previous_state_parameters = runtime.state.request_parameters
        previous_model_snapshot = runtime.state.model_snapshot
        previous_parameters = runtime.exchange.context.get("request_parameters")
        had_parameters = "request_parameters" in runtime.exchange.context
        runtime.state.request_parameters = {}
        runtime.state.model_snapshot = {
            **previous_model_snapshot,
            "thinking": "disable",
            "output_length": _TITLE_OUTPUT_TOKENS,
        }
        runtime.exchange.context["request_parameters"] = {
            "thinking": {"type": "disabled"},
            "max_tokens": _TITLE_OUTPUT_TOKENS,
        }
        try:
            prepared = self._request(
                runtime,
                [SystemMessage(content=load_title_prompt()), UserMessage(content=first_user_text)],
                operation="title",
                output_mode="text",
                allowed_tools=[],
                operation_tools=[],
                stream=False,
            )
        finally:
            runtime.state.turn_usage = previous_usage
            runtime.state.request_parameters = previous_state_parameters
            runtime.state.model_snapshot = previous_model_snapshot
            if had_parameters:
                runtime.exchange.context["request_parameters"] = previous_parameters
            else:
                runtime.exchange.context.pop("request_parameters", None)
        title = normalize_conversation_title(prepared.message.content or "")
        if not title:
            raise PlanningError("Conversation title generation returned no content.")
        return title


__all__ = ["TITLE_MAX_CHARS", "TitleMixin", "normalize_conversation_title"]
