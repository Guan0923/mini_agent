"""Aggregated performance metrics for one benchmark task run."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class RunMetrics:
    duration_ms: float
    model_calls: int
    tool_calls: int
    retries: int
    replans: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    active_skill_names: list[str]

    def to_dict(self) -> dict:
        return asdict(self)
