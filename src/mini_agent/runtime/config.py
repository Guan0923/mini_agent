"""Runtime limits kept separate from CLI argument parsing."""

from __future__ import annotations

from dataclasses import dataclass
from mini_agent.domain import StrategyPolicy


@dataclass(frozen=True)
class RunnerSettings:
    max_retries: int = 1
    max_actions: int = 8
    max_replans: int = 2
    strategy: StrategyPolicy = "auto"

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be zero or greater.")
        if self.max_actions < 1:
            raise ValueError("max_actions must be at least one.")
        if self.max_replans < 0:
            raise ValueError("max_replans must be zero or greater.")
        if self.strategy not in {"auto", "reactive", "plan_execute", "dynamic_replan"}:
            raise ValueError("strategy must be 'auto', 'reactive', 'plan_execute', or 'dynamic_replan'.")
