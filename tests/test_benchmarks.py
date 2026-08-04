"""Fast, offline coverage for the benchmark registry, grading, and metrics."""

from __future__ import annotations

from types import SimpleNamespace

from backend.runtime.core.events import RuntimeEvent

from benchmarks.event_collector import EventCollector
from benchmarks.grading.programmatic import (
    content_equals,
    run_checkers,
    subagents_completed,
    subagents_failed,
)
from benchmarks.metrics import RunMetrics
from benchmarks.model import BenchmarkTask, CheckContext, CheckerVerdict
from benchmarks.runner import build_metrics, run_one_task
from benchmarks.sandbox import Sandbox
from benchmarks.tasks import ALL_TASKS, TASKS_BY_NAME


def test_registry_contains_ten_unique_tasks_across_four_capabilities() -> None:
    assert len(ALL_TASKS) == 10
    assert len(TASKS_BY_NAME) == len(ALL_TASKS)
    assert {task.capability for task in ALL_TASKS} == {"tools", "skills", "mcp", "subagents"}
    assert {task.name for task in ALL_TASKS} >= {
        "tools-list-files",
        "tools-search-edit",
        "tools-command-sum",
        "skills-release-notes",
        "mcp-missing-sku",
        "subagents-parallel-summary",
    }


def test_new_checkers_cover_exact_content_and_subagent_outcomes(tmp_path) -> None:
    path = tmp_path / "result.txt"
    path.write_text("expected", encoding="utf-8")
    context = CheckContext(
        task_name="checker-test",
        workspace=tmp_path,
        status="completed",
        final_answer="",
        metrics=RunMetrics(0, 0, 0, 0, 0, 0, 0, 0, [], 2, 0),
        tool_calls_by_name={},
    )

    assert content_equals("result.txt", "expected")(context).passed
    assert subagents_completed(2)(context).passed
    assert subagents_failed(0)(context).passed
    path.write_text("expected plus an unsafe edit", encoding="utf-8")
    assert content_equals("result.txt", "expected")(context).passed is False


def test_checker_exceptions_are_isolated() -> None:
    def exploding(_context) -> CheckerVerdict:
        raise RuntimeError("broken checker")

    task = BenchmarkTask(
        name="checker-isolation",
        description="test",
        capability="tools",
        prompt="test",
        checkers=(exploding,),
    )
    context = CheckContext(
        task_name=task.name,
        workspace=__import__("pathlib").Path("."),
        status="completed",
        final_answer="",
        metrics=RunMetrics(0, 0, 0, 0, 0, 0, 0, 0, []),
        tool_calls_by_name={},
    )

    verdicts = run_checkers(task, context)
    assert len(verdicts) == 1
    assert verdicts[0].score == 0.0
    assert "checker raised RuntimeError" in verdicts[0].detail


def test_event_collector_publishes_subagent_metrics() -> None:
    collector = EventCollector()
    collector(RuntimeEvent("subagent_completed", "done"))
    collector(RuntimeEvent("subagent_failed", "failed"))
    state = SimpleNamespace(model_turns=0, actions=[], replan_count=0, active_skills=[])

    metrics = build_metrics(collector, state, 12.5)
    assert metrics.subagent_completed == 1
    assert metrics.subagent_failed == 1
    assert metrics.to_dict()["subagent_completed"] == 1


def test_rule_tasks_are_free_offline_smoke_runs(tmp_path, monkeypatch) -> None:
    sandbox = Sandbox(tmp_path / "sandbox")
    sandbox.prepare()
    import backend.runtime.application.factory as factory

    monkeypatch.setattr(factory, "client_paths", lambda: sandbox.paths)
    for name in ("tools-read-file", "tools-list-files"):
        result = run_one_task(TASKS_BY_NAME[name], planner="rule", sandbox=sandbox)
        assert result.status == "completed"
        assert result.score == 1.0
