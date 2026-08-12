"""Benchmark harness as a separately mounted FastAPI sub-application.

Mounted at /benchmark by the main app. It imports the benchmark harness lazily
inside handlers, so starting the chat backend never pulls in the benchmark code.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from ..auth.dependencies import require_user
from ..state import WebAppState


class RunRequest(BaseModel):
    task: str
    planner: str = "llm"


class RunAllRequest(BaseModel):
    planner: str = "llm"


def _benchmark_sandbox(request: Request):
    """Create the benchmark sandbox once and cache it on the app state."""
    app = request.app
    identity = getattr(request.state, "user", None)
    if identity is None:
        raise HTTPException(status_code=401, detail="请先登录。")
    cached_by_user = getattr(app.state, "benchmark_by_user", {})
    cached = cached_by_user.get(identity.id)
    if cached is not None:
        return cached
    from benchmarks.sandbox import Sandbox

    web: WebAppState = app.state.web
    try:
        model_config = web.model_config_for_user(identity.id)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"模型未配置：{exc}") from exc
    sandbox = Sandbox(web.user_benchmark_root(identity.id), model_config=model_config)
    sandbox.prepare()
    cached_by_user[identity.id] = sandbox
    app.state.benchmark_by_user = cached_by_user
    return sandbox


# Mounted at /benchmark by the main app, so the router carries no prefix.
router = APIRouter()


@router.get("/tasks")
def list_tasks(request: Request) -> list[dict]:
    from benchmarks.tasks import ALL_TASKS

    return [
        {
            "name": task.name,
            "capability": task.capability,
            "description": task.description,
            "prompt": task.prompt,
            "difficulty": task.difficulty,
            "budgets": {
                "max_tool_calls": task.budgets.max_tool_calls,
            },
            "tags": list(task.tags),
            "source": {
                "benchmark": task.source.benchmark,
                "task_id": task.source.task_id,
                "url": task.source.url,
                "source_revision": task.source.source_revision,
                "license": task.source.license,
                "adaptation_notes": task.source.adaptation_notes,
            },
            "planner_modes": sorted(task.planner_modes),
        }
        for task in ALL_TASKS
    ]


@router.post("/run")
def run_benchmark(body: RunRequest, request: Request) -> dict:
    from benchmarks.runner import run_one_task
    from benchmarks.tasks import TASKS_BY_NAME

    task = TASKS_BY_NAME.get(body.task)
    if task is None:
        raise HTTPException(status_code=404, detail=f"未知任务：{body.task}")
    return run_one_task(task, planner=body.planner, sandbox=_benchmark_sandbox(request)).to_dict()


@router.post("/run-all")
def run_all_benchmark(body: RunAllRequest, request: Request) -> list[dict]:
    from benchmarks.runner import run_one_task
    from benchmarks.tasks import ALL_TASKS

    sandbox = _benchmark_sandbox(request)
    return [
        run_one_task(task, planner=body.planner, sandbox=sandbox).to_dict()
        for task in ALL_TASKS
        if body.planner in task.planner_modes
    ]


def create_benchmark_app(web_state: WebAppState) -> FastAPI:
    app = FastAPI(title="Mini-Agent Benchmark", version="0.2.0", dependencies=[Depends(require_user)])
    app.state.web = web_state
    app.include_router(router)
    return app
