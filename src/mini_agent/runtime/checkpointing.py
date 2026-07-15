"""Storage boundary for durable runtime checkpoints."""

from __future__ import annotations

from typing import Protocol

from mini_agent.domain import RunState


class CheckpointStore(Protocol):
    """Persist snapshots without exposing a concrete database to the runner."""

    def save(self, state: RunState, reason: str) -> None: ...
