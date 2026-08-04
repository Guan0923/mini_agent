"""Benchmark harness as a separately mounted FastAPI sub-application.

Mounted at /benchmark by the main app. It imports the benchmark harness lazily
inside handlers, so starting the chat backend never pulls in the benchmark code.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel

from .state import WebAppState


class RunRequest(BaseModel):
    task: str
    planner: str = "llm"


class RunAllRequest(BaseModel):
    planner: str = "llm"


def _benchmark_sandbox(request: Request):
    """Create the benchmark sandbox once and cache it on the app state."""
    app = request.app
    cached = getattr(app.state, "benchmark", None)
    if cached is not None:
        return cached
    from benchmarks.sandbox import Sandbox

    web: WebAppState = app.state.web
    sandbox = Sandbox(web.data_root / "sandbox", web.config_path)
    sandbox.prepare()
    app.state.benchmark = sandbox
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
            "difficulty": task.difficulty,
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
    app = FastAPI(title="Mini-Agent Benchmark", version="0.1.0")
    app.state.web = web_state
    app.include_router(router)
    return app
