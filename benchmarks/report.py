"""JSON report writer for a benchmark session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .grading.scoring import summarize
from .model import TaskResult


def build_report(results: list[TaskResult], *, meta: dict[str, Any] | None = None) -> dict:
    return {
        "meta": meta or {},
        "summary": summarize(results),
        "tasks": [result.to_dict() for result in results],
    }


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def print_summary(report: dict) -> None:
    summary = report["summary"]
    print("\n=== Benchmark summary ===")
    print(f"tasks: {summary['tasks_scored']} scored / {summary['tasks_run']} run")
    print(f"overall score: {summary['overall_score']}")
    print(f"pass rate (>=0.9): {summary['pass_rate']}")
    for capability, score in summary.get("by_capability", {}).items():
        print(f"  {capability}: {score}")
    print(f"avg duration: {summary['duration_ms_mean']} ms, avg tokens: {summary['total_tokens_mean']}")
    print("--- per-task ---")
    for task in report["tasks"]:
        status = task["status"]
        score = task["score"] if task["score"] is not None else "-"
        metrics = task["metrics"]
        detail = task.get("error") or ""
        print(
            f"  {task['task_name']:<24} status={status:<10} score={score} "
            f"duration={metrics['duration_ms']}ms calls={metrics['model_calls']} tokens={metrics['total_tokens']}"
            f"{('  ' + detail) if detail else ''}"
        )
