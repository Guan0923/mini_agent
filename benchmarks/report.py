"""JSON report writer for a benchmark session."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .grading.scoring import summarize
from .model import TaskResult


def build_report(results: list[TaskResult], *, meta: dict[str, Any] | None = None) -> dict:
    groups: dict[str, list[TaskResult]] = defaultdict(list)
    for result in results:
        groups[result.task_name].append(result)
    try:
        from .tasks import TASKS_BY_NAME
    except ImportError:
        TASKS_BY_NAME = {}
    tasks: list[dict[str, Any]] = []
    for name, attempts in groups.items():
        definition = TASKS_BY_NAME.get(name)
        task = {
            "task_name": name,
            "capability": attempts[0].capability,
            "attempts": len(attempts),
            "passes": sum(1 for attempt in attempts if attempt.passed),
            "pass_rate": round(sum(1 for attempt in attempts if attempt.passed) / len(attempts), 4),
            "source": definition.source.__dict__ if definition is not None else None,
            "difficulty": definition.difficulty if definition is not None else None,
        }
        tasks.append(task)
    return {
        "meta": meta or {},
        "summary": summarize(results),
        "tasks": tasks,
        "runs": [result.to_dict() for result in results],
    }


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def print_summary(report: dict) -> None:
    summary = report["summary"]
    print("\n=== Benchmark summary ===")
    print(
        f"tasks: {summary['tasks_scored']} scored / {summary['tasks_run']} unique, "
        f"{summary.get('attempts_run', 0)} attempts"
    )
    print(f"overall score: {summary['overall_score']}")
    print(f"task pass rate (all attempts passed): {summary['pass_rate']}")
    for capability, score in summary.get("by_capability", {}).items():
        print(f"  {capability}: {score}")
    print(f"avg duration: {summary['duration_ms_mean']} ms, avg tokens: {summary['total_tokens_mean']}")
    print("--- per-task ---")
    for task in report["tasks"]:
        print(
            f"  {task['task_name']:<30} passes={task['passes']}/{task['attempts']} "
            f"pass_rate={task['pass_rate']} source={task['source']['benchmark'] if task['source'] else '-'}"
        )
