"""Provider-neutral conversation cleanup and automatic history compression."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from mini_agent.domain import (
    AssistantMessage,
    ChatMessage,
    PlanningError,
    SystemMessage,
    ToolSpec,
    UserMessage,
)
from mini_agent.runtime.core.context import AgentRuntime
from mini_agent.runtime.core.events import RuntimeEvent

_CONTEXT_SUMMARY_NAME = "context_summary"
_CONTEXT_SUMMARY_PREFIX = "[Conversation summary]"
_DEFAULT_THRESHOLD = 0.8


class TokenEstimator(Protocol):
    """Count the request tokens visible to one concrete model provider."""

    context_size: int

    def estimate_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int: ...


@dataclass(frozen=True)
class _CleanResult:
    messages: list[ChatMessage]
    turn_start_index: int
    removed_messages: int
    removed_tool_calls: int


@dataclass(frozen=True)
class ContextCompactionResult:
    """Describe one explicit context compaction attempt."""

    compacted: bool
    previous_messages: int
    remaining_messages: int
    summary: str | None = None


class ContextManager:
    """Clean durable history and summarize old turns before model requests."""

    def __init__(self, estimator: TokenEstimator, threshold: float = _DEFAULT_THRESHOLD) -> None:
        if estimator.context_size < 1:
            raise ValueError("context_size must be positive.")
        if not 0 < threshold < 1:
            raise ValueError("context compression threshold must be between zero and one.")
        self.estimator = estimator
        self.threshold = threshold

    def compact(
        self,
        runtime: AgentRuntime,
        *,
        summarize: Callable[[str], str],
    ) -> ContextCompactionResult:
        """Immediately summarize old turns while preserving the latest turn."""

        previous_messages = len(runtime.state.messages)
        clean = self._clean_history(runtime)
        boundary = min(max(clean.turn_start_index, 0), len(clean.messages))
        old_history = clean.messages[:boundary]

        summary_only = (
            len(old_history) == 1
            and isinstance(old_history[0], SystemMessage)
            and old_history[0].name == _CONTEXT_SUMMARY_NAME
        )
        if not old_history or summary_only:
            if clean.removed_messages or clean.removed_tool_calls:
                self._record(
                    runtime,
                    "context_cleaned",
                    "Incomplete context removed",
                    {
                        "removed_messages": clean.removed_messages,
                        "removed_tool_calls": clean.removed_tool_calls,
                        "remaining_messages": len(clean.messages),
                    },
                )
                self._replace_history(runtime, clean.messages, clean.turn_start_index)
            return ContextCompactionResult(
                compacted=False,
                previous_messages=previous_messages,
                remaining_messages=len(clean.messages),
            )

        summary = summarize(self._transcript(old_history)).strip()
        if not summary:
            raise PlanningError("Context summarization returned no content.")

        compressed = [
            SystemMessage(
                name=_CONTEXT_SUMMARY_NAME,
                content=f"{_CONTEXT_SUMMARY_PREFIX}\n{summary}",
            ),
            *clean.messages[boundary:],
        ]
        if clean.removed_messages or clean.removed_tool_calls:
            self._record(
                runtime,
                "context_cleaned",
                "Incomplete context removed",
                {
                    "removed_messages": clean.removed_messages,
                    "removed_tool_calls": clean.removed_tool_calls,
                    "remaining_messages": len(clean.messages),
                },
            )
        self._record(
            runtime,
            "context_compressed",
            "Conversation context compacted manually",
            {
                "previous_messages": len(old_history),
                "remaining_messages": len(compressed),
                "manual": True,
            },
        )
        self._replace_history(runtime, compressed, 1)
        return ContextCompactionResult(
            compacted=True,
            previous_messages=previous_messages,
            remaining_messages=len(compressed),
            summary=summary,
        )

    def prepare(
        self,
        runtime: AgentRuntime,
        system: SystemMessage,
        *,
        extra: list[UserMessage] | None = None,
        tools: list[ToolSpec] | None = None,
        request_parameters: dict[str, Any] | None = None,
        summarize: Callable[[str], str],
    ) -> list[ChatMessage]:
        """Return cleaned request messages, compressing old history when needed."""

        clean = self._clean_history(runtime)
        if clean.removed_messages or clean.removed_tool_calls:
            self._replace_history(runtime, clean.messages, clean.turn_start_index)
            self._record(
                runtime,
                "context_cleaned",
                "Incomplete context removed",
                {
                    "removed_messages": clean.removed_messages,
                    "removed_tool_calls": clean.removed_tool_calls,
                    "remaining_messages": len(clean.messages),
                },
            )

        suffix = list(extra or [])
        exposed_tools = list(tools or [])
        parameters = dict(request_parameters or {})
        messages = [system, *runtime.state.messages, *suffix]
        estimated = self.estimator.estimate_tokens(messages, exposed_tools, parameters)
        provider_total = self._provider_total(runtime)
        threshold_tokens = int(self.estimator.context_size * self.threshold)
        self._publish_usage(runtime, estimated, phase="before_compression")
        if max(estimated, provider_total or 0) < threshold_tokens:
            return messages

        boundary = min(max(runtime.run.turn_start_index, 0), len(runtime.state.messages))
        old_history = runtime.state.messages[:boundary]
        if old_history:
            transcript = self._transcript(old_history)
            try:
                summary = summarize(transcript).strip()
            except PlanningError:
                summary = ""
            if summary:
                retained = runtime.state.messages[boundary:]
                compressed = [
                    SystemMessage(
                        name=_CONTEXT_SUMMARY_NAME,
                        content=f"{_CONTEXT_SUMMARY_PREFIX}\n{summary}",
                    ),
                    *retained,
                ]
                self._replace_history(runtime, compressed, 1)
                messages = [system, *runtime.state.messages, *suffix]
                estimated_after = self.estimator.estimate_tokens(messages, exposed_tools, parameters)
                self._record(
                    runtime,
                    "context_compressed",
                    "Conversation context compressed",
                    {
                        "previous_messages": len(old_history),
                        "remaining_messages": len(compressed),
                        "provider_total_tokens": provider_total,
                        "estimated_tokens_before": estimated,
                        "estimated_tokens_after": estimated_after,
                        "threshold_tokens": threshold_tokens,
                    },
                )
                estimated = estimated_after
                self._publish_usage(runtime, estimated, phase="after_compression")

        if estimated >= self.estimator.context_size:
            raise PlanningError(
                "The current request still exceeds the model context window after context cleanup and compression."
            )
        return messages

    def _publish_usage(self, runtime: AgentRuntime, estimated: int, *, phase: str) -> None:
        context_size = self.estimator.context_size
        threshold_tokens = int(context_size * self.threshold)
        publish = runtime.services.publish or (lambda _event: None)
        publish(
            RuntimeEvent(
                "context_usage",
                "Context usage estimated",
                {
                    "estimated_tokens": estimated,
                    "context_size": context_size,
                    "threshold": self.threshold,
                    "threshold_tokens": threshold_tokens,
                    "ratio": estimated / context_size,
                    "phase": phase,
                },
            )
        )

    @staticmethod
    def _provider_total(runtime: AgentRuntime) -> int | None:
        usage = runtime.state.turn_usage or runtime.state.usage
        if not isinstance(usage, dict):
            return None
        total = usage.get("total_tokens")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return None
        return total

    @staticmethod
    def _clean_history(runtime: AgentRuntime) -> _CleanResult:
        boundary = min(max(runtime.run.turn_start_index, 0), len(runtime.state.messages))
        messages: list[ChatMessage] = []
        new_boundary = 0
        removed_messages = 0
        removed_tool_calls = 0

        for index, message in enumerate(runtime.state.messages):
            cleaned: ChatMessage | None = message
            if isinstance(message, SystemMessage | UserMessage):
                if not message.content or not message.content.strip():
                    cleaned = None
            elif isinstance(message, AssistantMessage):
                complete_tools = [
                    tool for tool in message.tool_messages if tool.status != "pending" and tool.content is not None
                ]
                removed_tool_calls += len(message.tool_messages) - len(complete_tools)
                if not (message.content and message.content.strip()) and not complete_tools:
                    cleaned = None
                elif len(complete_tools) != len(message.tool_messages):
                    cleaned = replace(message, tool_messages=complete_tools)

            if cleaned is None:
                removed_messages += 1
                continue
            messages.append(cleaned)
            if index < boundary:
                new_boundary += 1

        return _CleanResult(messages, new_boundary, removed_messages, removed_tool_calls)

    @staticmethod
    def _replace_history(runtime: AgentRuntime, messages: list[ChatMessage], turn_start_index: int) -> None:
        runtime.state.messages = messages
        runtime.run.history = runtime.state.messages
        runtime.run.turn_start_index = turn_start_index
        runtime.save()

    @staticmethod
    def _record(runtime: AgentRuntime, kind: str, message: str, data: dict[str, Any]) -> None:
        runtime.run.add_event(kind, message, **data)  # type: ignore[arg-type]
        (runtime.services.publish or (lambda _event: None))(RuntimeEvent(kind, message, data))  # type: ignore[arg-type]

    @staticmethod
    def _transcript(messages: list[ChatMessage]) -> str:
        transcript: list[dict[str, Any]] = []
        for message in messages:
            item: dict[str, Any] = {"role": message.role, "content": message.content}
            if isinstance(message, AssistantMessage) and message.tool_messages:
                item["tools"] = [
                    {
                        "name": tool.name,
                        "arguments": tool.arguments,
                        "status": tool.status,
                        "result": tool.content,
                    }
                    for tool in message.tool_messages
                ]
            transcript.append(item)
        return json.dumps(transcript, ensure_ascii=False, indent=2)
