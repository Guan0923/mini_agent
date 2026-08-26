"""Provider-neutral conversation history compression."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from backend.domain import (
    CHECKPOINT_PREAMBLE,
    AssistantMessage,
    ChatMessage,
    PlanningError,
    SystemMessage,
    ToolSpec,
    UserMessage,
)
from backend.runtime.core.context import AgentRuntime
from backend.runtime.core.events import RuntimeEvent

_CONTEXT_SUMMARY_NAME = "context_summary"
_DEFAULT_TARGET_RATIO = 0.8


class TokenEstimator(Protocol):
    """Count the request tokens visible to one concrete model provider."""

    context_size: int

    def estimate_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int: ...

    def estimate_input_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int: ...


@dataclass(frozen=True)
class ContextCompactionResult:
    """Describe one explicit context compaction attempt."""

    compacted: bool
    previous_messages: int
    remaining_messages: int
    summary: str | None = None


class ContextManager:
    """Summarize completed conversation history without altering active work."""

    def __init__(self, estimator: TokenEstimator, target_ratio: float = _DEFAULT_TARGET_RATIO) -> None:
        if estimator.context_size < 1:
            raise ValueError("context_size must be positive.")
        if not 0 < target_ratio < 1:
            raise ValueError("context compression target ratio must be between zero and one.")
        self.estimator = estimator
        self.target_ratio = target_ratio

    @property
    def target_tokens(self) -> int:
        return int(self.estimator.context_size * self.target_ratio)

    def compact(
        self,
        runtime: AgentRuntime,
        *,
        summarize: Callable[[str], str],
    ) -> ContextCompactionResult:
        """Manually summarize all persisted, already-finished conversation history."""

        canonical_nodes = runtime.model_nodes()
        original = runtime.model_messages() if canonical_nodes else list(runtime.state.messages)
        if not original:
            return ContextCompactionResult(False, 0, 0)
        estimated_before = self._estimate_input_tokens(original, [], {})
        summary, compressed, estimated_after = self._summarize_candidate(
            runtime,
            source=original,
            retained=[],
            summarize=summarize,
            trigger="manual",
            estimated_before=estimated_before,
            estimate_candidate=lambda candidate: self._estimate_input_tokens(candidate, [], {}),
        )
        if not canonical_nodes:
            self._replace_history(runtime, compressed, 1)
        self._record(
            runtime,
            "context_compaction_completed",
            "Conversation context compacted manually",
            {
                "trigger": "manual",
                "source_messages": len(original),
                "previous_messages": len(original),
                "remaining_messages": len(compressed),
                "estimated_tokens_before": estimated_before,
                "estimated_tokens_after": estimated_after,
                "target_tokens": self.target_tokens,
                "summary": summary,
            },
        )
        runtime.save()
        return ContextCompactionResult(True, len(original), len(compressed), summary)

    def prepare(
        self,
        runtime: AgentRuntime,
        system: SystemMessage,
        *,
        history: list[ChatMessage] | None = None,
        extra: list[UserMessage] | None = None,
        tools: list[ToolSpec] | None = None,
        request_parameters: dict[str, Any] | None = None,
        summarize: Callable[[str], str],
    ) -> list[ChatMessage]:
        """Build a request, compacting only completed history when it would exceed the window."""

        suffix = list(extra or [])
        exposed_tools = list(tools or [])
        parameters = dict(request_parameters or {})
        source_history = list(history) if history is not None else list(runtime.state.messages)
        messages = [system, *source_history, *suffix]
        estimated_before = self._estimate_input_tokens(messages, exposed_tools, parameters)
        self._publish_usage(runtime, estimated_before, phase="before_compaction")
        if estimated_before < self.target_tokens:
            runtime.exchange.context["estimated_input_tokens"] = estimated_before
            return messages

        boundary = min(max(runtime.run.turn_start_index, 0), len(source_history))
        completed_history = source_history[:boundary]
        if not completed_history:
            if estimated_before >= self.estimator.context_size:
                raise PlanningError(
                    "The current turn exceeds the model context window and cannot be compacted before it finishes."
                )
            runtime.exchange.context["estimated_input_tokens"] = estimated_before
            return messages

        summary, compressed, estimated_after = self._summarize_candidate(
            runtime,
            source=completed_history,
            retained=source_history[boundary:],
            summarize=summarize,
            trigger="automatic",
            estimated_before=estimated_before,
            estimate_candidate=lambda candidate: self._estimate_input_tokens(
                [system, *candidate, *suffix], exposed_tools, parameters
            ),
        )
        previous_messages = len(source_history)
        # Legacy runtimes still own the mutable ChatMessage transcript.  A
        # canonical tree caller keeps its dynamic path authoritative and lets
        # the bridge persist the compaction node; mutating the legacy list here
        # would reintroduce a second source of truth.
        if history is None:
            self._replace_history(runtime, compressed, 1)
        else:
            # Canonical callers keep the tree authoritative, but the runner
            # still uses this boundary to distinguish completed history from
            # the active turn on the next request.  Rebase it onto the
            # compacted compatibility projection instead of the pre-summary
            # node count.
            runtime.run.turn_start_index = max(0, len(compressed) - len(source_history[boundary:]))
        self._record(
            runtime,
            "context_compaction_completed",
            "Conversation context compacted automatically",
            {
                "trigger": "automatic",
                "source_messages": len(completed_history),
                "previous_messages": previous_messages,
                "remaining_messages": len(compressed),
                "estimated_tokens_before": estimated_before,
                "estimated_tokens_after": estimated_after,
                "target_tokens": self.target_tokens,
                "summary": summary,
            },
        )
        runtime.save()
        self._publish_usage(runtime, estimated_after, phase="after_compaction")
        runtime.exchange.context["estimated_input_tokens"] = estimated_after
        return [system, *compressed, *suffix]

    def _summarize_candidate(
        self,
        runtime: AgentRuntime,
        *,
        source: list[ChatMessage],
        retained: list[ChatMessage],
        summarize: Callable[[str], str],
        trigger: str,
        estimated_before: int,
        estimate_candidate: Callable[[list[ChatMessage]], int],
    ) -> tuple[str, list[ChatMessage], int]:
        self._record(
            runtime,
            "context_compaction_started",
            "Conversation context compaction started",
            {
                "trigger": trigger,
                "source_messages": len(source),
                "retained_messages": len(retained),
                "estimated_tokens_before": estimated_before,
                "target_tokens": self.target_tokens,
            },
        )
        runtime.save()
        try:
            summary = summarize(self._transcript(source)).strip()
            if not summary:
                raise PlanningError("Context summarization returned no content.")
            compressed = [
                SystemMessage(name=_CONTEXT_SUMMARY_NAME, content=f"{CHECKPOINT_PREAMBLE}\n\n{summary}"),
                *retained,
            ]
            estimated_after = estimate_candidate(compressed)
            if estimated_after > self.target_tokens:
                raise PlanningError(
                    "Context compaction could not reduce the candidate request to the configured context target."
                )
        except Exception as exc:
            self._record(
                runtime,
                "context_compaction_failed",
                "Conversation context compaction failed",
                {
                    "trigger": trigger,
                    "source_messages": len(source),
                    "estimated_tokens_before": estimated_before,
                    "target_tokens": self.target_tokens,
                    "error": str(exc),
                },
            )
            runtime.save()
            raise
        return summary, compressed, estimated_after

    def _publish_usage(self, runtime: AgentRuntime, estimated: int, *, phase: str) -> None:
        publish = runtime.services.publish or (lambda _event: None)
        publish(
            RuntimeEvent(
                "context_usage",
                "Context usage estimated",
                {
                    "estimated_tokens": estimated,
                    "input_tokens": estimated,
                    "input_source": "estimated",
                    "context_size": self.estimator.context_size,
                    "target_ratio": self.target_ratio,
                    "target_tokens": self.target_tokens,
                    "ratio": estimated / self.estimator.context_size,
                    "phase": phase,
                },
            )
        )

    def _estimate_input_tokens(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        request_parameters: dict[str, Any],
    ) -> int:
        estimate = getattr(self.estimator, "estimate_input_tokens", None)
        if callable(estimate):
            return estimate(messages, tools, request_parameters)
        return self.estimator.estimate_tokens(messages, tools, request_parameters)

    @staticmethod
    def _replace_history(runtime: AgentRuntime, messages: list[ChatMessage], turn_start_index: int) -> None:
        runtime.state.messages = messages
        runtime.run.history = runtime.state.messages
        runtime.run.turn_start_index = turn_start_index

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
