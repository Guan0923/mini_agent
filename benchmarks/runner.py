"""Execute one benchmark task against the real or rule-based agent."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from backend.providers import ModelConfigurationError
from backend.runtime import RunnerSettings, build_application
from backend.runtime.core.contracts import InterruptDecision, InterruptRequest
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.persistence.recording import persistent_event

from .event_collector import EventCollector
from .grading.programmatic import run_checkers
from .grading.scoring import aggregate_score
from .metrics import RunMetrics
from .model import BenchmarkTask, Budgets, CheckContext, CheckerVerdict, TaskResult
from .sandbox import Sandbox


def auto_approve(request: InterruptRequest) -> InterruptDecision:
    """Approve every tool call and plan review so a headless run never stalls."""
    if request.kind == "plan":
        return InterruptDecision("implement")
    if request.kind == "tool":
        return InterruptDecision("continue")
    if request.kind == "question":
        answers = {question.id: [question.options[0].label] for question in request.questions}
        return InterruptDecision("answer", answers=answers)
    return InterruptDecision("continue")  # resume and anything unexpected


def build_metrics(collector: EventCollector, state, duration_ms: float) -> RunMetrics:
    finished = collector.run_finished or {}
    return RunMetrics(
        duration_ms=round(duration_ms, 3),
        model_calls=int(finished.get("model_calls", state.model_turns) or 0),
        tool_calls=int(finished.get("tool_calls", len(state.actions)) or 0),
        retries=int(finished.get("retries", 0) or 0),
        prompt_tokens=collector.prompt_tokens,
        completion_tokens=collector.completion_tokens,
        total_tokens=collector.total_tokens,
        active_skill_names=[skill.name for skill in state.active_skills],
        subagent_completed=collector.subagent_completed,
        subagent_failed=collector.subagent_failed,
    )


def apply_budget_overrides(task: BenchmarkTask, *, max_tool_calls: int | None) -> BenchmarkTask:
    if max_tool_calls is None:
        return task
    budgets = task.budgets
    return replace(
        task,
        budgets=Budgets(
            max_tool_calls=max_tool_calls or budgets.max_tool_calls,
        ),
    )


def _error_result(
    task: BenchmarkTask,
    message: str,
    *,
    attempt: int = 1,
    collector: EventCollector | None = None,
    failure_phase: str | None = None,
) -> TaskResult:
    diagnostic_event = RuntimeEvent(
        "error",
        message,
        {"failure_phase": failure_phase or "unknown"},
    )
    if collector is not None and (not collector.events or collector.events[-1].kind != "error"):
        collector(diagnostic_event)
    safe_message, safe_data = persistent_event(diagnostic_event, include_full_messages=True)
    return TaskResult(
        task_name=task.name,
        capability=task.capability,
        status="error",
        score=None,
        final_answer="",
        metrics=RunMetrics(0.0, 0, 0, 0, 0, 0, 0, 0, []),
        verdicts=[],
        error=safe_message,
        passed=False,
        attempt=attempt,
        trace=collector.trace()
        if collector is not None
        else [
            {
                "kind": diagnostic_event.kind,
                "timestamp": diagnostic_event.timestamp,
                "message": safe_message,
                "data": safe_data,
            }
        ],
        failure_phase=failure_phase,
    )


def run_one_task(
    task: BenchmarkTask,
    *,
    planner: str,
    sandbox: Sandbox,
    keep_workspaces: bool = False,
    max_tool_calls: int | None = None,
    attempt: int = 1,
) -> TaskResult:
    """Run one task end to end and return its graded result."""
    if planner not in task.planner_modes:
        return _error_result(
            task,
            f"planner {planner!r} is not supported by this task",
            attempt=attempt,
            failure_phase="configuration",
        )

    task = apply_budget_overrides(task, max_tool_calls=max_tool_calls)
    workspace: Path | None = None
    app = None
    collector = EventCollector()
    phase = "workspace"
    try:
        workspace = sandbox.materialize_workspace(task)

        settings = RunnerSettings(
            max_tool_calls=task.budgets.max_tool_calls,
            log_full_messages=True,
        )
        phase = "application"
        app = build_application(
            workspace,
            planner_name=planner,
            settings=settings,
            paths=sandbox.paths,
            model_config=sandbox.model_config,
        )
        conversation = app.open_conversation()
        started = perf_counter()
        phase = "agent"
        state = conversation.run_task(
            task.prompt,
            mode="agent",
            on_event=collector,
            interrupt=auto_approve,
        )
        duration_ms = (perf_counter() - started) * 1000.0

        metrics = build_metrics(collector, state, duration_ms)
        context = CheckContext(
            task_name=task.name,
            workspace=workspace,
            status=state.status,
            final_answer=state.final_answer or "",
            metrics=metrics,
            tool_calls_by_name=dict(collector.tool_calls_by_name),
        )
        phase = "grading"
        verdicts = run_checkers(task, context)
        if state.status != "completed":
            verdicts = [CheckerVerdict(0.0, detail=f"agent run status: {state.status}")]
        score = aggregate_score(verdicts)
        return TaskResult(
            task_name=task.name,
            capability=task.capability,
            status=state.status,
            score=score,
            final_answer=state.final_answer or "",
            metrics=metrics,
            verdicts=verdicts,
            run_id=state.run_id,
            passed=score == 1.0,
            attempt=attempt,
            trace=collector.trace(),
            failure_phase="agent" if state.status != "completed" else None,
        )
    except ModelConfigurationError as exc:
        return _error_result(
            task,
            f"model is not configured: {exc}",
            attempt=attempt,
            collector=collector,
            failure_phase="configuration",
        )
    except Exception as exc:  # keep the harness alive across task failures
        return _error_result(
            task,
            f"{type(exc).__name__}: {exc}",
            attempt=attempt,
            collector=collector,
            failure_phase=phase,
        )
    finally:
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if workspace is not None and not keep_workspaces:
            shutil.rmtree(workspace, ignore_errors=True)
