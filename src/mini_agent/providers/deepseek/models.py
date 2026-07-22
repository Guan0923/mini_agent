"""DeepSeek response value objects."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mini_agent.runtime.core.context import PreparedResponse


@dataclass(frozen=True)
class DeepSeekToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class DeepSeekUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    completion_tokens_details: dict[str, Any] | None = None


@dataclass
class DeepSeekCompletion(PreparedResponse):
    """Compatibility view over the provider-neutral prepared response."""

    @property
    def content(self) -> str | None:
        return self.message.content

    @property
    def reasoning_content(self) -> str | None:
        return self.message.reasoning

    @property
    def tool_calls(self) -> list[DeepSeekToolCall]:
        return [
            DeepSeekToolCall(tool.call_id, tool.name, json.dumps(tool.arguments, ensure_ascii=False))
            for tool in self.message.tool_messages
        ]


@dataclass(frozen=True)
class DeepSeekStreamDelta:
    content: str | None
    reasoning_content: str | None
    finish_reason: str | None = None
    role: str | None = None
    index: int = 0
    tool_calls: list[dict[str, Any]] | None = None
