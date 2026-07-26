"""Mutable state and task launching for one interactive TUI run."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from queue import Queue
from threading import Event
from typing import Any, Protocol

from backend.domain import RunState

from ..components.interactive_approval import InteractiveApproval
from ..view import TerminalView


class InteractiveHost(Protocol):
    """Application operations required by the interactive coordinator."""

    _approval: Any
    _conversation_service: Any

    def run_task(self, task: str, **kwargs: Any) -> RunState | None: ...
    def resume_session(self, session_id: str | None, **kwargs: Any) -> RunState | None: ...
    def _write(self, text: str, end: str = "\n") -> None: ...
    def _write_user_message(self, content: str) -> None: ...
    def _clear_queued_messages(self) -> None: ...
    def _drain_steering(self, messages: Queue[str]) -> list[str]: ...
    async def _cancel_task(self, task: asyncio.Task[Any]) -> None: ...


@dataclass(frozen=True)
class LoopWaiters:
    """Queue and lifecycle tasks awaited during one loop iteration."""

    submission: asyncio.Task[str | None]
    interrupt: asyncio.Task[None]
    approval: asyncio.Task[bool]
    permission: asyncio.Task[str] | None

    def tasks(
        self,
        view_task: asyncio.Task[None],
        active_run: asyncio.Task[RunState | None] | None,
        active_compaction: asyncio.Task[Any] | None,
    ) -> set[asyncio.Task[Any]]:
        tasks: set[asyncio.Task[Any]] = {self.submission, self.interrupt, self.approval, view_task}
        if active_run is not None:
            tasks.add(active_run)
        if active_compaction is not None:
            tasks.add(active_compaction)
        if self.permission is not None:
            tasks.add(self.permission)
        return tasks


@dataclass
class InteractiveRunState:
    """Own transient state that must not leak into the long-lived TerminalApp."""

    host: InteractiveHost
    view: TerminalView
    approval: InteractiveApproval
    pending_messages: Queue[str] = field(default_factory=Queue)
    permission_decisions: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    active_run: asyncio.Task[RunState | None] | None = None
    active_compaction: asyncio.Task[Any] | None = None
    permission_pending: bool = False
    deferred_task: str | None = None
    cancel_requested: Event = field(default_factory=Event)
    suspend_requested: Event = field(default_factory=Event)
    cancellation_pending: bool = False
    exit_after_run: bool = False
    normal_exit_requested: bool = False
    view_ended_early: bool = False
    view_task_error: BaseException | None = None

    def launch(self, task: str) -> asyncio.Task[RunState | None]:
        return asyncio.create_task(
            asyncio.to_thread(
                self.host.run_task,
                task,
                interrupt=self.approval,
                cancel_requested=self.cancel_requested.is_set,
                suspend_requested=self.suspend_requested.is_set,
            )
        )

    def launch_resume(self, session_id: str | None) -> asyncio.Task[RunState | None]:
        return asyncio.create_task(
            asyncio.to_thread(
                self.host.resume_session,
                session_id,
                interrupt=self.approval,
                cancel_requested=self.cancel_requested.is_set,
                suspend_requested=self.suspend_requested.is_set,
            )
        )

    def launch_compaction(self) -> asyncio.Task[Any]:
        return asyncio.create_task(asyncio.to_thread(self.host._conversation_service.compact_context))

    def launch_queued_messages(self) -> asyncio.Task[RunState | None] | None:
        queued = self.host._drain_steering(self.pending_messages)
        if not queued:
            return None
        task = "\n\n".join(queued)
        self.host._clear_queued_messages()
        self.host._write(f"QUEUE STARTED — {len(queued)} queued message(s)")
        self.host._write_user_message(task)
        return self.launch(task)

    def launch_startup_resume(self) -> None:
        session_id = getattr(self.host, "_startup_resume_id", None)
        if session_id is not None:
            self.active_run = self.launch_resume(session_id)
            self.host._startup_resume_id = None

    def create_waiters(self) -> LoopWaiters:
        return LoopWaiters(
            submission=asyncio.create_task(self.view.submissions.get()),
            interrupt=asyncio.create_task(self.view.interrupts.get()),
            approval=asyncio.create_task(self.approval.changed.wait()),
            permission=asyncio.create_task(self.permission_decisions.get()) if self.permission_pending else None,
        )

    async def cancel_unused_waiters(
        self,
        waiters: LoopWaiters,
        done: set[asyncio.Task[Any]],
    ) -> None:
        for task in (waiters.submission, waiters.interrupt, waiters.approval, waiters.permission):
            if task is not None and task not in done:
                await self.host._cancel_task(task)
