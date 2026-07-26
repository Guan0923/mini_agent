"""Storage boundary for durable runtime checkpoints."""

from __future__ import annotations

from typing import Protocol

from ..core.context import AgentRuntime


class CheckpointStore(Protocol):
    def save(self, runtime: AgentRuntime, reason: str) -> None: ...
