"""Concurrent child-agent execution with shared-workspace write coordination."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition
from typing import Any, Protocol
from uuid import uuid4

from backend.domain import new_session_id
from backend.tools import ToolError

from .core.context import AgentRuntime
from .core.contracts import InterruptRequest
from .core.events import RuntimeEvent


class ChildRunner(Protocol):
    tools: object

    def new_runtime(
        self, *, task: str, session_id: str | None = None, on_event: object = None, interrupt: object = None
    ) -> AgentRuntime: ...

    def run(self, runtime: AgentRuntime) -> object: ...


@dataclass(frozen=True)
class SubagentTask:
    id: str
    task: str


class WorkspaceWriteLock:
    """Allow unrelated file writes while excluding commands and same-path writes."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._paths: set[str] = set()
        self._command_active = False

    @contextmanager
    def file(self, path: str) -> Iterator[None]:
        with self._condition:
            while self._command_active or path in self._paths:
                self._condition.wait()
            self._paths.add(path)
        try:
            yield
        finally:
            with self._condition:
                self._paths.remove(path)
                self._condition.notify_all()

    @contextmanager
    def command(self) -> Iterator[None]:
        with self._condition:
            while self._command_active or self._paths:
                self._condition.wait()
            self._command_active = True
        try:
            yield
        finally:
            with self._condition:
                self._command_active = False
                self._condition.notify_all()


class LockedToolExecutor:
    """Delegate the tool port while locking only workspace mutation operations."""

    def __init__(self, tools: object, locks: WorkspaceWriteLock) -> None:
        self._tools = tools
        self._locks = locks

    def names(self) -> list[str]:
        return self._tools.names()

    def read_only_names(self) -> list[str]:
        return self._tools.read_only_names()

    def specs(self):
        return self._tools.specs()

    def read_only_specs(self):
        return self._tools.read_only_specs()

    def is_read_only(self, name: str) -> bool:
        return self._tools.is_read_only(name)

    def requires_confirmation(self, name: str) -> bool:
        return self._tools.requires_confirmation(name)

    def is_retryable(self, name: str) -> bool:
        return self._tools.is_retryable(name)

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        self._tools.validate_arguments(name, arguments)

    def invoke(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str:
        if name in {"write_file", "edit_file"}:
            path = arguments.get("path")
            if not isinstance(path, str):
                raise ToolError("Workspace mutation requires a path.")
            with self._locks.file(path.replace("\\", "/").casefold()):
                return self._tools.invoke(name, arguments, confirmed=confirmed)
        if name == "run_command":
            with self._locks.command():
                return self._tools.invoke(name, arguments, confirmed=confirmed)
        return self._tools.invoke(name, arguments, confirmed=confirmed)


class SubagentCoordinator:
    """Create child runs and expose their persisted summaries to the parent run."""

    def __init__(self, child_runner_factory: Callable[[], ChildRunner]) -> None:
        self._child_runner_factory = child_runner_factory
        self._locks = WorkspaceWriteLock()

    @staticmethod
    def handles(name: str) -> bool:
        return name in {"delegate_tasks", "get_subagent_results"}

    def invoke(self, runtime: AgentRuntime, name: str, arguments: dict[str, Any]) -> str:
        if name == "delegate_tasks":
            return self._delegate(runtime, arguments)
        if name == "get_subagent_results":
            return self._results(runtime, arguments)
        raise ToolError(f"Unknown subagent tool: {name}")

    def _delegate(self, runtime: AgentRuntime, arguments: dict[str, Any]) -> str:
        tasks = self._parse_tasks(arguments)
        batch_id = f"subagents_{uuid4().hex}"
        batch: dict[str, Any] = {
            "batch_id": batch_id,
            "status": "running",
            "tasks": [{"id": task.id, "task": task.task, "status": "queued"} for task in tasks],
        }
        runtime.run.subagent_batches[batch_id] = batch
        self._event(runtime, "subagent_queued", "Subagent batch queued", batch_id=batch_id, count=len(tasks))

        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="mini-agent-subagent") as executor:
            futures = {executor.submit(self._run_task, runtime, batch_id, task): task.id for task in tasks}
            for future in as_completed(futures):
                task_id = futures[future]
                try:
                    results[task_id] = future.result()
                except BaseException as exc:  # Keep sibling results available to the parent model.
                    results[task_id] = {"id": task_id, "status": "failed", "answer": "", "error": str(exc)}
                    self._event(
                        runtime,
                        "subagent_failed",
                        "Subagent failed",
                        batch_id=batch_id,
                        task_id=task_id,
                        error=str(exc),
                    )

        ordered = [results[task.id] for task in tasks]
        batch["status"] = "completed"
        batch["tasks"] = ordered
        return self._page(batch, 0, 20)

    def _run_task(self, parent: AgentRuntime, batch_id: str, task: SubagentTask) -> dict[str, Any]:
        self._event(parent, "subagent_started", "Subagent started", batch_id=batch_id, task_id=task.id)
        runner = self._child_runner_factory()
        runner.tools = LockedToolExecutor(runner.tools, self._locks)

        def interrupt(request: InterruptRequest):
            if request.kind == "tool":
                tool = request.data.get("tool")
                self._event(
                    parent,
                    "subagent_write_requested",
                    "Subagent tool approval requested",
                    batch_id=batch_id,
                    task_id=task.id,
                    tool=tool,
                    arguments=request.data.get("arguments", {}),
                )
            parent_handler = parent.services.interrupt
            if parent_handler is None:
                return runner._default_interrupt(child_runtime)(request)  # type: ignore[attr-defined]
            enriched = InterruptRequest(
                request.kind,
                request.message,
                {**request.data, "subagent_batch_id": batch_id, "subagent_task_id": task.id},
                request.questions,
            )
            return parent_handler(enriched)

        child_runtime = runner.new_runtime(
            task=task.task,
            session_id=new_session_id(),
            interrupt=interrupt,
        )
        child_runtime.services.cancel_requested = parent.services.cancel_requested
        child_run = runner.run(child_runtime)
        status = getattr(child_run, "status", "failed")
        answer = str(getattr(child_run, "final_answer", "") or "")
        result = {"id": task.id, "status": status, "answer": self._clip(answer), "error": ""}
        event = "subagent_completed" if status == "completed" else "subagent_failed"
        self._event(parent, event, f"Subagent {status}", batch_id=batch_id, task_id=task.id, status=status)
        return result

    def _results(self, runtime: AgentRuntime, arguments: dict[str, Any]) -> str:
        batch_id = arguments.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            raise ToolError("batch_id must be a non-empty string.")
        batch = runtime.run.subagent_batches.get(batch_id)
        if batch is None:
            raise ToolError(f"Unknown subagent batch: {batch_id}")
        cursor = arguments.get("cursor", 0)
        limit = arguments.get("limit", 20)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ToolError("cursor must be a non-negative integer.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ToolError("limit must be between 1 and 100.")
        return self._page(batch, cursor, limit)

    @staticmethod
    def _parse_tasks(arguments: dict[str, Any]) -> list[SubagentTask]:
        raw = arguments.get("tasks")
        if not isinstance(raw, list) or not raw:
            raise ToolError("tasks must be a non-empty array.")
        tasks: list[SubagentTask] = []
        ids: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ToolError("Each subagent task must be an object.")
            task_id, task = item.get("id"), item.get("task")
            if not isinstance(task_id, str) or not task_id.strip() or task_id in ids:
                raise ToolError("Each subagent task needs a unique non-empty id.")
            if not isinstance(task, str) or not task.strip():
                raise ToolError("Each subagent task needs non-empty task text.")
            ids.add(task_id)
            tasks.append(SubagentTask(task_id, task))
        return tasks

    @staticmethod
    def _page(batch: dict[str, Any], cursor: int, limit: int) -> str:
        tasks = list(batch.get("tasks", []))
        page = tasks[cursor : cursor + limit]
        next_cursor = cursor + len(page)
        return json.dumps(
            {
                "batch_id": batch["batch_id"],
                "status": batch["status"],
                "total": len(tasks),
                "results": page,
                "next_cursor": next_cursor if next_cursor < len(tasks) else None,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _clip(value: str, limit: int = 4_000) -> str:
        return value if len(value) <= limit else f"{value[:limit]}… ({len(value) - limit} characters omitted)"

    @staticmethod
    def _event(runtime: AgentRuntime, kind: str, message: str, **data: Any) -> None:
        runtime.run.add_event(kind, message, **data)
        publish = runtime.services.publish
        if publish is not None:
            publish(RuntimeEvent(kind, message, data))
