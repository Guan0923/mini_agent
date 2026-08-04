"""Score aggregation for a task and for a whole benchmark session."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean

from ..model import CheckerVerdict, TaskResult


def aggregate_score(verdicts: list[CheckerVerdict]) -> float | None:
    """Return a strict binary task score; None when no verifier ran."""
    if not verdicts:
        return None
    return 1.0 if all(verdict.score >= 1.0 for verdict in verdicts) else 0.0


def summarize(results: list[TaskResult]) -> dict:
    """Summarize attempts while giving every unique task equal weight."""
    by_task: dict[str, list[TaskResult]] = defaultdict(list)
    for result in results:
        by_task[result.task_name].append(result)
    task_rates = {
        name: mean(1.0 if result.passed else 0.0 for result in attempts) for name, attempts in by_task.items()
    }
    task_capabilities = {name: attempts[0].capability for name, attempts in by_task.items() if attempts}
    capabilities: dict[str, list[float]] = defaultdict(list)
    for task_name, rate in task_rates.items():
        capabilities[task_capabilities[task_name]].append(rate)
    duration_values = [result.metrics.duration_ms for result in results]
    token_values = [result.metrics.total_tokens for result in results]
    return {
        "tasks_run": len(by_task),
        "attempts_run": len(results),
        "tasks_scored": sum(any(result.score is not None for result in attempts) for attempts in by_task.values()),
        "tasks_failed_to_run": sum(
            all(result.status == "error" for result in attempts) for attempts in by_task.values()
        ),
        "overall_score": round(mean(task_rates.values()), 4) if task_rates else None,
        "pass_rate": round(sum(1 for score in task_rates.values() if score >= 1.0) / len(task_rates), 4)
        if task_rates
        else None,
        "by_capability": {capability: round(mean(scores), 4) for capability, scores in sorted(capabilities.items())},
        "duration_ms_mean": round(mean(duration_values), 2) if duration_values else 0.0,
        "total_tokens_mean": round(mean(token_values), 2) if token_values else 0.0,
    }
