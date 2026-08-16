from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import sleep
from types import SimpleNamespace

import pytest

from backend.domain import RunState
from backend.runtime.core.context import AgentRuntime
from backend.runtime.subagents import LockedToolExecutor, SubagentCoordinator, WorkspaceWriteLock
from backend.tools import ToolError


class _IdleTools:
    def names(self) -> list[str]:
        return []

    def read_only_names(self) -> list[str]:
        return []

    def specs(self) -> list[object]:
        return []

    def read_only_specs(self) -> list[object]:
        return []

    def is_read_only(self, _name: str) -> bool:
        return False

    def requires_confirmation(self, _name: str) -> bool:
        return False

    def is_retryable(self, _name: str) -> bool:
        return False

    def validate_arguments(self, _name: str, _arguments: dict[str, object]) -> None:
        return None

    def invoke(self, _name: str, _arguments: dict[str, object], confirmed: bool = False) -> str:
        return "ok"


class _ChildRunner:
    def __init__(self) -> None:
        self.tools = _IdleTools()
        self.task = ""

    def new_runtime(self, *, task: str, session_id: str | None = None, **_kwargs: object) -> AgentRuntime:
        self.task = task
        return AgentRuntime.ephemeral(session_id=session_id or "child", planner=object(), tools=self.tools)

    def run(self, _runtime: AgentRuntime) -> object:
        return SimpleNamespace(status="completed", final_answer=f"completed {self.task}")


def _parent_runtime() -> AgentRuntime:
    runtime = AgentRuntime.ephemeral(session_id="parent", planner=object(), tools=_IdleTools())
    runtime.state.current_run = RunState(task="parent task", mode="agent")
    return runtime


def test_subagent_coordinator_runs_tasks_and_pages_results() -> None:
    coordinator = SubagentCoordinator(_ChildRunner)
    runtime = _parent_runtime()

    summary = json.loads(
        coordinator.invoke(
            runtime,
            "delegate_tasks",
            {"tasks": [{"id": "one", "task": "first"}, {"id": "two", "task": "second"}]},
        )
    )

    assert summary["total"] == 2
    assert [item["answer"] for item in summary["results"]] == ["completed first", "completed second"]
    assert runtime.run.subagent_batches[summary["batch_id"]]["status"] == "completed"
    page = json.loads(
        coordinator.invoke(runtime, "get_subagent_results", {"batch_id": summary["batch_id"], "limit": 1})
    )
    assert page["results"][0]["id"] == "one"
    assert page["next_cursor"] == 1


def test_subagent_coordinator_rejects_duplicate_task_ids() -> None:
    coordinator = SubagentCoordinator(_ChildRunner)
    with pytest.raises(ToolError, match="unique"):
        coordinator.invoke(
            _parent_runtime(),
            "delegate_tasks",
            {"tasks": [{"id": "same", "task": "one"}, {"id": "same", "task": "two"}]},
        )


def test_locked_executor_serializes_same_path_writes() -> None:
    active = 0
    maximum = 0
    guard = Lock()

    class Tools(_IdleTools):
        def invoke(self, _name: str, _arguments: dict[str, object], confirmed: bool = False) -> str:
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            sleep(0.02)
            with guard:
                active -= 1
            return "written"

    executor = LockedToolExecutor(Tools(), WorkspaceWriteLock())
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(executor.invoke, "write_file", {"path": "same.txt"}, True) for _ in range(2)]
        assert [future.result() for future in futures] == ["written", "written"]
    assert maximum == 1


def test_run_state_persists_subagent_batches() -> None:
    state = RunState(task="persist", mode="agent", subagent_batches={"batch": {"status": "completed", "tasks": []}})
    restored = RunState.from_dict(state.to_dict())
    assert restored.subagent_batches == state.subagent_batches


def test_recovery_marks_running_subagent_batch_indeterminate() -> None:
    from backend.runtime.conversation.recovery import reconstruct_attempt
    from backend.runtime.core.context import RuntimeState

    state = RuntimeState(
        session_id="resume",
        status="running",
        current_run=RunState(
            task="resume",
            mode="agent",
            subagent_batches={"batch": {"status": "running", "tasks": [{"status": "running"}]}},
        ),
    )

    source, resumed = reconstruct_attempt(state)

    assert source.current_run is not None
    assert source.current_run.subagent_batches["batch"]["status"] == "indeterminate"
    assert resumed.current_run is not None
    assert resumed.current_run.subagent_batches["batch"]["tasks"][0]["status"] == "indeterminate"
