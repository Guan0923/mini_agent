"""Score aggregation for a task and for a whole benchmark session."""

from __future__ import annotations

from statistics import mean

from ..model import CheckerVerdict, TaskResult


def aggregate_score(verdicts: list[CheckerVerdict]) -> float | None:
    """Weighted mean of verdict scores; None when there are no checkers."""
    if not verdicts:
        return None
    total = sum(verdict.score * verdict.weight for verdict in verdicts)
    weights = sum(verdict.weight for verdict in verdicts)
    if not weights:
        return None
    return total / weights


def summarize(results: list[TaskResult]) -> dict:
    """Produce the aggregate report numbers: overall, per capability, pass rate."""
    scored = [result for result in results if result.score is not None]
    capabilities: dict[str, list[float]] = {}
    for result in scored:
        capabilities.setdefault(result.capability, []).append(result.score or 0.0)
    return {
        "tasks_run": len(results),
        "tasks_scored": len(scored),
        "tasks_failed_to_run": sum(1 for result in results if result.status == "error"),
        "overall_score": round(mean(result.score for result in scored), 4) if scored else None,
        "pass_rate": round(sum(1 for result in scored if result.score >= 0.9) / len(scored), 4) if scored else None,
        "by_capability": {
            capability: round(mean(scores), 4) for capability, scores in sorted(capabilities.items())
        },
        "duration_ms_mean": round(mean(result.metrics.duration_ms for result in results if result.metrics), 2),
        "total_tokens_mean": round(mean(result.metrics.total_tokens for result in results if result.metrics), 2),
    }
