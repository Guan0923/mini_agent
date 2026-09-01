"""Persistent same-Session Agent Threads and Redis mailbox coordination."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import BoundedSemaphore, Event, RLock, Thread, local
from typing import Any

from backend.jobs import ThreadJob
from backend.tools import ToolError

from .capability_settings import SubagentSettings
from .core.context import AgentRuntime
from .core.contracts import InterruptRequest
from .node_bridge import RuntimeEventNodeBridge
from .subagent.contracts import AgentThreadEvents, ChildRunner, _SessionBinding, _StatusControl
from .subagent.execution import _SubagentExecutionMixin
from .subagent.reports import _SubagentReportDeliveryMixin
from .subagent.tool_actions import _SubagentToolActionsMixin
from .subagent.tool_executor import LockedToolExecutor, WorkspaceWriteLock


class SubagentCoordinator(_SubagentToolActionsMixin, _SubagentReportDeliveryMixin, _SubagentExecutionMixin):
    """Process-owned coordinator backed by SQLite and Redis rather than batch state."""

    _TOOLS = {
        "delegate_tasks",
        "send_agent_message",
        "set_thread_node_status",
        "get_thread_node",
        "pause_current_turn",
    }

    def __init__(
        self,
        child_runner_factory: Callable[[], ChildRunner] | None = None,
        workspace: Path | None = None,
        settings: SubagentSettings | None = None,
        *,
        store: object | None = None,
        message_queue: object | None = None,
        index: object | None = None,
        job_registry: object | None = None,
        thread_events: AgentThreadEvents | None = None,
    ) -> None:
        self._settings = settings or SubagentSettings()
        self._store = store
        self._queue = message_queue
        self._index = index
        self._job_registry = job_registry
        self._thread_events = thread_events
        self._bindings: dict[str, _SessionBinding] = {}
        self._jobs: dict[str, ThreadJob] = {}
        self._active_bridges: dict[str, RuntimeEventNodeBridge] = {}
        self._approval_channels: dict[str, Callable[[InterruptRequest], object]] = {}
        self._status_controls: dict[str, _StatusControl] = {}
        self._locks = WorkspaceWriteLock()
        self._state_lock = RLock()
        self._worker_slots = BoundedSemaphore(self._settings.max_workers)
        self._report_dispatch_stop = Event()
        self._report_dispatch_wakeup = Event()
        self._report_dispatcher: Thread | None = None
        self._report_retry: dict[str, tuple[int, float]] = {}
        self._reply_context = local()
        if child_runner_factory is not None:
            self._bindings["*"] = _SessionBinding(child_runner_factory, (workspace or Path(".")).resolve())

    def bind_session(
        self,
        session_id: str,
        runner_factory: Callable[[], ChildRunner],
        workspace: Path,
        project_workspace: Path | None = None,
    ) -> None:
        with self._state_lock:
            self._bindings[session_id] = _SessionBinding(
                runner_factory,
                workspace.resolve(),
                project_workspace.resolve() if project_workspace is not None else None,
            )
        self._ensure_report_dispatcher()
        self.recover_session(session_id)

    def close(self) -> None:
        self._report_dispatch_stop.set()
        self._report_dispatch_wakeup.set()
        dispatcher = self._report_dispatcher
        if dispatcher is not None and dispatcher.is_alive():
            dispatcher.join(timeout=2.0)

    @classmethod
    def handles(cls, name: str) -> bool:
        return name in cls._TOOLS

    def invoke(self, runtime: AgentRuntime, name: str, arguments: dict[str, Any]) -> str:
        self._require_services()
        if name == "delegate_tasks":
            return self._delegate(runtime, arguments)
        if name == "send_agent_message":
            return self._send(runtime, arguments)
        if name == "set_thread_node_status":
            return self._set_status(runtime, arguments)
        if name == "get_thread_node":
            return self._get_nodes(runtime, arguments)
        if name == "pause_current_turn":
            return self._pause_current_turn(runtime)
        raise ToolError(f"Unknown subagent tool: {name}")


__all__ = ["LockedToolExecutor", "SubagentCoordinator", "WorkspaceWriteLock"]
