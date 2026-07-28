"""Concurrent child-agent execution with parent-thread runtime coordination."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from backend.domain import new_session_id
from backend.tools import ToolError

from .capability_settings import SubagentSettings
from .core.context import AgentRuntime
from .core.contracts import InterruptRequest
from .core.events import RuntimeEvent
from .persistence.recording import persistent_event
from .subagent_bridge import ParentRuntimeBridge
from .subagent_tools import LockedToolExecutor, WorkspaceWriteLock


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


@dataclass
class _TaskControl:
    cancel: Event = field(default_factory=Event)
    started_at: float | None = None
    paused_at: float | None = None
    paused_seconds: float = 0.0
    pause_count: int = 0
    lock: Lock = field(default_factory=Lock)

    def start(self) -> None:
        with self.lock:
            self.started_at = monotonic()

    def pause(self) -> None:
        with self.lock:
            self.pause_count += 1
            if self.pause_count == 1:
                self.paused_at = monotonic()

    def resume(self) -> None:
        with self.lock:
            if self.pause_count == 0:
                return
            self.pause_count -= 1
            if self.pause_count == 0 and self.paused_at is not None:
                self.paused_seconds += monotonic() - self.paused_at
                self.paused_at = None

    def elapsed(self, now: float) -> float:
        with self.lock:
            if self.started_at is None:
                return 0.0
            current_pause = now - self.paused_at if self.paused_at is not None else 0.0
            return max(0.0, now - self.started_at - self.paused_seconds - current_pause)


class SubagentCoordinator:
    """Create child runs while serializing parent state changes."""

    def __init__(
        self,
        child_runner_factory: Callable[[], ChildRunner],
        workspace: Path | None = None,
        settings: SubagentSettings | None = None,
    ) -> None:
        self._child_runner_factory = child_runner_factory
        self._workspace = (workspace or Path(".")).resolve()
        self._settings = settings or SubagentSettings()
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

        parent_interrupt = runtime.services.interrupt
        bridge = ParentRuntimeBridge(
            lambda kind, message, **data: self._event(runtime, kind, message, **data),
            parent_interrupt or (lambda _request: (_ for _ in ()).throw(RuntimeError("No parent interrupt handler."))),
        )
        controls = {task.id: _TaskControl() for task in tasks}
        batch_control = _TaskControl()
        batch_control.start()
        executor = ThreadPoolExecutor(
            max_workers=min(len(tasks), self._settings.max_workers),
            thread_name_prefix="mini-agent-subagent",
        )
        futures: dict[Future[dict[str, Any]], SubagentTask] = {
            executor.submit(
                self._run_task,
                parent_interrupt is not None,
                bridge,
                batch_id,
                task,
                controls[task.id],
                batch_control,
            ): task
            for task in tasks
        }
        pending = set(futures)
        results: dict[str, dict[str, Any]] = {}
        cancelled = False
        try:
            while pending:
                bridge.drain()
                now = monotonic()
                cancel_requested = runtime.services.cancel_requested
                cancelled = bool(cancel_requested is not None and cancel_requested())
                expired_batch = batch_control.elapsed(now) >= self._settings.batch_timeout_seconds
                for future in list(pending):
                    task = futures[future]
                    control = controls[task.id]
                    timed_out = expired_batch or control.elapsed(now) >= self._settings.task_timeout_seconds
                    if cancelled or timed_out:
                        control.cancel.set()
                        future.cancel()
                        status = "cancelled" if cancelled else "timed_out"
                        results[task.id] = {"id": task.id, "status": status, "answer": "", "error": ""}
                        pending.remove(future)
                        self._event(
                            runtime,
                            "subagent_failed",
                            f"Subagent {status}",
                            batch_id=batch_id,
                            task_id=task.id,
                            status=status,
                        )
                        continue
                    if not future.done():
                        continue
                    pending.remove(future)
                    try:
                        results[task.id] = future.result()
                    except Exception as exc:
                        error = self._safe_error(exc)
                        results[task.id] = {
                            "id": task.id,
                            "status": "failed",
                            "answer": "",
                            "error": error,
                        }
                        self._event(
                            runtime,
                            "subagent_failed",
                            "Subagent failed",
                            batch_id=batch_id,
                            task_id=task.id,
                            error=error,
                        )
                if pending:
                    bridge.wait(0.01)
            bridge.drain()
        finally:
            for control in controls.values():
                control.cancel.set()
            bridge.close()
            executor.shutdown(wait=False, cancel_futures=True)

        ordered = [results[task.id] for task in tasks]
        batch["status"] = (
            "cancelled"
            if cancelled
            else "completed"
            if all(item["status"] == "completed" for item in ordered)
            else "failed"
        )
        batch["tasks"] = ordered
        return self._page(batch, 0, 20)

    def _run_task(
        self,
        has_parent_interrupt: bool,
        bridge: ParentRuntimeBridge,
        batch_id: str,
        task: SubagentTask,
        control: _TaskControl,
        batch_control: _TaskControl,
    ) -> dict[str, Any]:
        control.start()
        bridge.event("subagent_started", "Subagent started", batch_id=batch_id, task_id=task.id)
        runner = self._child_runner_factory()
        runner.tools = LockedToolExecutor(runner.tools, self._locks, self._workspace)
        child_runtime: AgentRuntime

        def interrupt(request: InterruptRequest):
            if request.kind == "tool":
                bridge.event(
                    "subagent_write_requested",
                    "Subagent tool approval requested",
                    batch_id=batch_id,
                    task_id=task.id,
                    tool=request.data.get("tool"),
                    arguments=request.data.get("arguments", {}),
                )
            if not has_parent_interrupt:
                return runner._default_interrupt(child_runtime)(request)  # type: ignore[attr-defined]
            enriched = InterruptRequest(
                request.kind,
                request.message,
                {**request.data, "subagent_batch_id": batch_id, "subagent_task_id": task.id},
                request.questions,
            )
            control.pause()
            batch_control.pause()
            try:
                return bridge.approval(enriched)
            finally:
                control.resume()
                batch_control.resume()

        child_runtime = runner.new_runtime(task=task.task, session_id=new_session_id(), interrupt=interrupt)
        child_runtime.services.cancel_requested = control.cancel.is_set
        try:
            child_run = runner.run(child_runtime)
            status = str(getattr(child_run, "status", "failed"))
            answer = str(getattr(child_run, "final_answer", "") or "")
            result = {"id": task.id, "status": status, "answer": self._clip(answer), "error": ""}
            event = "subagent_completed" if status == "completed" else "subagent_failed"
            bridge.event(event, f"Subagent {status}", batch_id=batch_id, task_id=task.id, status=status)
            return result
        finally:
            close = getattr(runner, "close", None)
            if callable(close):
                close()

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

    def _parse_tasks(self, arguments: dict[str, Any]) -> list[SubagentTask]:
        raw = arguments.get("tasks")
        if not isinstance(raw, list) or not raw:
            raise ToolError("tasks must be a non-empty array.")
        if len(raw) > self._settings.max_tasks_per_batch:
            raise ToolError(f"tasks must contain at most {self._settings.max_tasks_per_batch} items.")
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
    def _safe_error(error: Exception) -> str:
        redacted, _data = persistent_event(RuntimeEvent("error", str(error)), True)
        return SubagentCoordinator._clip(redacted, 2_000)

    @staticmethod
    def _event(runtime: AgentRuntime, kind: str, message: str, **data: Any) -> None:
        runtime.run.add_event(kind, message, **data)
        publish = runtime.services.publish
        if publish is not None:
            publish(RuntimeEvent(kind, message, data))
