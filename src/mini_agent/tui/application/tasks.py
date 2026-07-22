"""Task execution and terminal output helpers."""

from __future__ import annotations

import asyncio
from queue import Empty, Queue

from mini_agent.domain import RunState
from mini_agent.runtime import TaskPreparationError
from mini_agent.runtime.core.contracts import CancellationHandler, InterruptHandler, SteeringHandler

from ..components.interactive_approval import InteractiveApproval as _InteractiveApproval
from ..view import TerminalView


class TaskAppMixin:
    def run_task(
        self,
        task: str,
        *,
        steering: SteeringHandler | None = None,
        interrupt: InterruptHandler | None = None,
        cancel_requested: CancellationHandler | None = None,
    ) -> RunState | None:
        previous_session_id = self.active_session.session_id if self.active_session is not None else None
        try:
            self.last_state = self._conversation_service.run_task(
                task,
                mode=self.mode,
                on_event=self._event_sink,
                interrupt=interrupt or self._approval,
                steering=steering,
                cancel_requested=cancel_requested,
            )
        except TaskPreparationError as exc:
            self._write(f"REFERENCE ERROR {exc}")
            return None
        if self.active_session is not None and self.active_session.session_id != previous_session_id:
            self._print_active_session()
        if self.last_state is not None and self.last_state.mode == "agent" and self.mode == "plan":
            self.mode = "agent"
            self._write("Agent mode enabled after Plan Review implementation handoff.")
        return self.last_state

    def _handle_idle_input(self, task: str) -> tuple[bool, str | None]:
        if not task:
            return True, None
        next_task: str | None = None
        parts = self._split_input(task)
        if any(kind == "command" and value == "history" for kind, value, _argument in parts):
            if task.strip() != "/history":
                self._write("Usage: /history")
                return True, None
        for kind, value, argument in parts:
            if kind == "task":
                next_task = value
                continue
            if not self._handle_command(value, argument):
                return False, None
        return True, next_task

    def _handle_view_input(self, task: str) -> tuple[bool, str | None, bool]:
        if not task:
            return True, None, False
        next_task: str | None = None
        permission_requested = False
        parts = self._split_input(task)
        if any(kind == "command" and value == "history" for kind, value, _argument in parts):
            if task.strip() != "/history":
                self._write("Usage: /history")
                return True, None, False
        for kind, value, argument in parts:
            if kind == "task":
                next_task = value
                continue
            if value == "permission":
                if argument:
                    self._write("Usage: /permission")
                else:
                    permission_requested = True
                continue
            if not self._handle_command(value, argument):
                return False, None, False
        return True, next_task, permission_requested

    def _update_view_state(
        self,
        view: TerminalView,
        running: bool,
        approval: _InteractiveApproval,
        permission_pending: bool,
        *,
        compacting: bool = False,
        cancelling: bool = False,
    ) -> None:
        mode = self.mode.upper()
        if cancelling:
            status = f"{mode} | CANCELLING"
            interrupt_enabled = False
        elif approval.pending:
            status = approval.status
            interrupt_enabled = True
        elif permission_pending:
            status = "PERMISSION | Select mode"
            interrupt_enabled = False
        elif compacting:
            status = "COMPACT | RUNNING"
            interrupt_enabled = False
        elif running:
            status = f"{mode} | RUNNING"
            interrupt_enabled = True
        else:
            status = f"{mode} | IDLE"
            interrupt_enabled = False
        view.set_ui(status=self._status_with_permission(status), interrupt_enabled=interrupt_enabled)

    def _status_with_permission(self, status: str) -> str:
        permission = self._approval.permission_mode.replace("_", " ").upper()
        return f"{status} | PERMISSION: {permission}"

    def _write(self, text: str, end: str = "\n") -> None:
        view = getattr(self, "_view", None)
        if view is not None:
            write_system = getattr(view, "write_system", None)
            if callable(write_system):
                write_system(text, end)
            else:
                view.write(text, end)
            return
        print(text, end=end, flush=end == "")

    def _write_user_message(self, content: str) -> None:
        """Echo only content that is about to enter the conversation."""

        view = getattr(self, "_view", None)
        begin_conversation = getattr(view, "begin_conversation", None)
        if callable(begin_conversation):
            begin_conversation(content)
            return
        self._write(f"USER\n{content}")

    def _clear_display(self) -> None:
        view = getattr(self, "_view", None)
        if view is not None:
            view.clear()
            return
        self._clear_terminal()

    def _print_resume_hint(self) -> None:
        if self.active_session is None:
            self._write("No saved session.")
            return
        session_id = self.active_session.session_id
        self._write(f"SESSION {session_id}")
        self._write(f"RESUME IN TUI /use {session_id}")
        self._write(f"RESUME FROM SHELL mini-agent --session-id {session_id}")

    @staticmethod
    def _drain_steering(messages: Queue[str]) -> list[str]:
        drained: list[str] = []
        while True:
            try:
                drained.append(messages.get_nowait())
            except Empty:
                return drained

    @staticmethod
    async def _cancel_task(task: asyncio.Task[object]) -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
