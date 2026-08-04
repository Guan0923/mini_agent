"""Execute one benchmark task against the real or rule-based agent."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from backend.providers import ModelConfigurationError
from backend.runtime import RunnerSettings, build_application
from backend.runtime.core.contracts import InterruptDecision, InterruptRequest

from .event_collector import EventCollector
from .grading.programmatic import run_checkers
from .grading.scoring import aggregate_score
from .metrics import RunMetrics
from .model import BenchmarkTask, Budgets, CheckContext, CheckerVerdict, TaskResult
from .sandbox import Sandbox, trust_project_mcp


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
        replans=int(finished.get("replans", state.replan_count) or 0),
        prompt_tokens=collector.prompt_tokens,
        completion_tokens=collector.completion_tokens,
        total_tokens=collector.total_tokens,
        active_skill_names=[skill.name for skill in state.active_skills],
        subagent_completed=collector.subagent_completed,
        subagent_failed=collector.subagent_failed,
    )


def apply_budget_overrides(
    task: BenchmarkTask, *, max_model_turns: int | None, max_tool_calls: int | None
) -> BenchmarkTask:
    if max_model_turns is None and max_tool_calls is None:
        return task
    budgets = task.budgets
    return replace(
        task,
        budgets=Budgets(
            max_model_turns=max_model_turns or budgets.max_model_turns,
            max_tool_calls=max_tool_calls or budgets.max_tool_calls,
            max_replans=budgets.max_replans,
            max_retries=budgets.max_retries,
        ),
    )


def _error_result(task: BenchmarkTask, message: str, *, attempt: int = 1) -> TaskResult:
    return TaskResult(
        task_name=task.name,
        capability=task.capability,
        status="error",
        score=None,
        final_answer="",
        metrics=RunMetrics(0.0, 0, 0, 0, 0, 0, 0, 0, []),
        verdicts=[],
        error=message,
        passed=False,
        attempt=attempt,
    )


def run_one_task(
    task: BenchmarkTask,
    *,
    planner: str,
    sandbox: Sandbox,
    keep_workspaces: bool = False,
    max_model_turns: int | None = None,
    max_tool_calls: int | None = None,
    attempt: int = 1,
) -> TaskResult:
    """Run one task end to end and return its graded result."""
    if planner not in task.planner_modes:
        return _error_result(task, f"planner {planner!r} is not supported by this task", attempt=attempt)

    task = apply_budget_overrides(task, max_model_turns=max_model_turns, max_tool_calls=max_tool_calls)
    workspace: Path | None = None
    app = None
    collector = EventCollector()
    try:
        workspace = sandbox.materialize_workspace(task)
        if task.seed.mcp is not None:
            trust_project_mcp(sandbox.paths, workspace)

        settings = RunnerSettings(
            max_model_turns=task.budgets.max_model_turns,
            max_tool_calls=task.budgets.max_tool_calls,
            max_replans=task.budgets.max_replans,
            max_retries=task.budgets.max_retries,
            log_full_messages=True,
        )
        app = build_application(
            workspace,
            planner_name=planner,
            settings=settings,
            project_mcp_enabled=True,
        )
        conversation = app.open_conversation()
        started = perf_counter()
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
        )
    except ModelConfigurationError as exc:
        return _error_result(task, f"model is not configured: {exc}", attempt=attempt)
    except Exception as exc:  # keep the harness alive across task failures
        return _error_result(task, f"{type(exc).__name__}: {exc}", attempt=attempt)
    finally:
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if workspace is not None and not keep_workspaces:
            shutil.rmtree(workspace, ignore_errors=True)
