"""Registry for the source-backed Mini-Agent benchmark suite."""

from __future__ import annotations

from ..model import BenchmarkTask
from .open_source import TASKS as _OPEN_SOURCE_TASKS

ALL_TASKS: tuple[BenchmarkTask, ...] = _OPEN_SOURCE_TASKS

TASKS_BY_NAME: dict[str, BenchmarkTask] = {task.name: task for task in ALL_TASKS}


def resolve_tasks(
    names: list[str],
    *,
    capability: str | None = None,
    planner: str = "llm",
) -> list[BenchmarkTask]:
    """Apply name, capability, and planner filters to the full registry."""
    selected = list(ALL_TASKS)
    if names:
        wanted = {item for group in names for item in group.split(",") if item}
        unknown = sorted(wanted - set(TASKS_BY_NAME))
        if unknown:
            raise ValueError(f"unknown task(s): {', '.join(unknown)}")
        selected = [task for task in selected if task.name in wanted]
    if capability is not None:
        selected = [task for task in selected if task.capability == capability]
    selected = [task for task in selected if planner in task.planner_modes]
    return selected


__all__ = ["ALL_TASKS", "TASKS_BY_NAME", "resolve_tasks"]
