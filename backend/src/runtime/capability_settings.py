"""Validated client settings for optional agent capabilities."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from backend.configuration import ConfigurationError, section


@dataclass(frozen=True)
class SkillSettings:
    auto_select: bool = False

    @classmethod
    def from_config(cls, values: Mapping[str, object]) -> SkillSettings:
        raw = section(values, "skills").get("auto_select", False)
        if not isinstance(raw, bool):
            raise ConfigurationError("skills.auto_select must be boolean.")
        return cls(raw)


@dataclass(frozen=True)
class SubagentSettings:
    max_tasks_per_batch: int = 8
    max_workers: int = 4
    task_timeout_seconds: float = 300.0
    batch_timeout_seconds: float = 600.0
    max_depth: int = 2

    @classmethod
    def from_config(cls, values: Mapping[str, object]) -> SubagentSettings:
        configured = section(values, "subagents")
        max_tasks = _positive_int(configured, "max_tasks_per_batch", 8)
        max_workers = _positive_int(configured, "max_workers", 4)
        if max_workers > max_tasks:
            raise ConfigurationError("subagents.max_workers must not exceed max_tasks_per_batch.")
        return cls(
            max_tasks_per_batch=max_tasks,
            max_workers=max_workers,
            task_timeout_seconds=_positive_number(configured, "task_timeout_seconds", 300.0),
            batch_timeout_seconds=_positive_number(configured, "batch_timeout_seconds", 600.0),
            max_depth=_positive_int(configured, "max_depth", 2),
        )


def _positive_int(values: Mapping[str, object], name: str, default: int) -> int:
    raw = values.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ConfigurationError(f"subagents.{name} must be a positive integer.")
    return raw


def _positive_number(values: Mapping[str, object], name: str, default: float) -> float:
    raw = values.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ConfigurationError(f"subagents.{name} must be a finite positive number.")
    resolved = float(raw)
    if not math.isfinite(resolved) or resolved <= 0:
        raise ConfigurationError(f"subagents.{name} must be a finite positive number.")
    return resolved
