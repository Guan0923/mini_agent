"""Provider-neutral, Turn-centered model request audit snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TurnTraceRequest:
    """One exact model request snapshot persisted before transport starts."""

    turn_id: str
    thread_id: str
    data_idx: int
    exchange_id: str
    sequence: int
    timestamp: str
    provider: str
    provider_name: str
    model: str
    operation: str
    output_mode: str
    stream: bool
    base_system_prompt: str
    effective_system_prompt: str
    messages: list[dict[str, Any]]
    user_preferences: str
    skills: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    request_parameters: dict[str, Any]

    @property
    def object_id(self) -> str:
        return f"{self.turn_id}:{self.data_idx}:{self.exchange_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "turn_id": self.turn_id,
            "thread_id": self.thread_id,
            "data_idx": self.data_idx,
            "exchange_id": self.exchange_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "provider": self.provider,
            "provider_name": self.provider_name,
            "model": self.model,
            "operation": self.operation,
            "output_mode": self.output_mode,
            "stream": self.stream,
            "base_system_prompt": self.base_system_prompt,
            "effective_system_prompt": self.effective_system_prompt,
            "messages": self.messages,
            "user_preferences": self.user_preferences,
            "skills": self.skills,
            "tools": self.tools,
            "request_parameters": self.request_parameters,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TurnTraceRequest:
        return cls(
            turn_id=str(value["turn_id"]),
            thread_id=str(value["thread_id"]),
            data_idx=int(value["data_idx"]),
            exchange_id=str(value["exchange_id"]),
            sequence=int(value["sequence"]),
            timestamp=str(value["timestamp"]),
            provider=str(value.get("provider") or "unknown"),
            provider_name=str(value.get("provider_name") or "unknown"),
            model=str(value.get("model") or "unknown"),
            operation=str(value.get("operation") or "unknown"),
            output_mode=str(value.get("output_mode") or "text"),
            stream=bool(value.get("stream", False)),
            base_system_prompt=str(value.get("base_system_prompt") or ""),
            effective_system_prompt=str(value.get("effective_system_prompt") or ""),
            messages=[dict(item) for item in value.get("messages", []) if isinstance(item, Mapping)],
            user_preferences=str(value.get("user_preferences") or ""),
            skills=[dict(item) for item in value.get("skills", []) if isinstance(item, Mapping)],
            tools=[dict(item) for item in value.get("tools", []) if isinstance(item, Mapping)],
            request_parameters=dict(value.get("request_parameters") or {}),
        )


__all__ = ["TurnTraceRequest"]
