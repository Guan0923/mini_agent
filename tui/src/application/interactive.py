"""Asynchronous Textual event-loop orchestration."""

from __future__ import annotations

import asyncio
from typing import Any

from ..components.completion import SlashCommandCompleter
from ..components.interactive_approval import InteractiveApproval
from ..screens.diagnostics import TuiDiagnosticLogger
from ..screens.exit_reporting import TuiExitReport, classify_tui_exit
from ..view import TerminalView
from .interactive_input import handle_submission
from .interactive_state import InteractiveRunState, LoopWaiters


class InteractiveAppMixin:
    async def _start_interactive(self) -> int:
        loop = asyncio.get_running_loop()
        log_dir = getattr(self, "_log_dir", None)
        diagnostics = TuiDiagnosticLogger(log_dir) if log_dir is not None else None
        self._tui_diagnostics = diagnostics
        session = self.active_session
        if diagnostics is not None:
            diagnostics.set_context(session_id=session.session_id if session is not None else None)
        runner = getattr(self, "runner", None)
        settings = getattr(runner, "settings", None)
        view = TerminalView(
            loop,
            completer=SlashCommandCompleter(),
            diagnostic_sink=diagnostics.record if diagnostics is not None else None,
            log_full_messages=getattr(settings, "log_full_messages", True),
            detail_level=getattr(self, "_display_mode", "medium"),
        )
        if diagnostics is not None:
            diagnostics.record("tui_started", {"mode": self.mode})
        self._view = view
        self._load_active_history()
        self._write("Mini-Agent TUI — type /help for commands, /quit to exit.")
        self._print_active_session()

        approval = InteractiveApproval(self._approval, loop, view)
        state = InteractiveRunState(self, view, approval)
        state.launch_startup_resume()
        view_task = asyncio.create_task(view.run_async())
        try:
            await self._run_interactive_loop(state, view_task, diagnostics)
        finally:
            report = await self._finish_interactive(state, view_task, diagnostics)
            self._tui_diagnostics = None
            self._view = None

        if report.exit_code:
            self._report_tui_error(report, diagnostics)
        self._print_resume_hint()
        return report.exit_code

    async def _run_interactive_loop(
        self,
        state: InteractiveRunState,
        view_task: asyncio.Task[None],
        diagnostics: TuiDiagnosticLogger | None,
    ) -> None:
        while True:
            self._update_view_state(
                state.view,
                state.active_run is not None,
                state.approval,
                state.permission_pending,
                compacting=state.active_compaction is not None,
                cancelling=state.cancellation_pending,
                suspending=state.exit_after_run,
            )
            waiters = state.create_waiters()
            done, _pending = await asyncio.wait(
                waiters.tasks(view_task, state.active_run, state.active_compaction),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if view_task in done:
                await self._handle_early_view_exit(state, view_task, waiters, done, diagnostics)
                return

            exit_requested = False
            run_finished = state.active_run is not None and state.active_run in done
            compaction_finished = state.active_compaction is not None and state.active_compaction in done

            if waiters.approval in done:
                state.approval.changed.clear()
                if (state.cancellation_pending or state.exit_after_run) and state.approval.pending:
                    state.approval.cancel_pending()

            if waiters.permission is not None and waiters.permission in done:
                self._apply_permission_choice(state, waiters.permission.result())

            if waiters.submission in done:
                exit_requested = handle_submission(self, state, waiters.submission.result())

            if waiters.interrupt in done:
                self._request_cancellation(state)

            if compaction_finished:
                self._finish_compaction(state)
                exit_requested = state.exit_after_run
                if not exit_requested:
                    state.active_run = state.launch_queued_messages()

            if run_finished:
                self._finish_active_run(state)
                exit_requested = state.exit_after_run
                if not exit_requested:
                    state.active_run = state.launch_queued_messages()

            await state.cancel_unused_waiters(waiters, done)
            if exit_requested:
                state.normal_exit_requested = True
                return

    def _apply_permission_choice(self, state: InteractiveRunState, selected: str) -> None:
        state.permission_pending = False
        if selected != "cancel":
            mode = self._approval.parse_permission(selected)
            if mode is None:
                raise ValueError(f"Invalid permission choice: {selected}")
            self._approval.set_permission(mode, announce=False)
            if state.deferred_task is not None:
                self._write_user_message(state.deferred_task)
                state.active_run = state.launch(state.deferred_task)
        state.deferred_task = None

    def _request_cancellation(self, state: InteractiveRunState) -> None:
        if (
            (state.active_run is not None or state.approval.pending)
            and not state.cancellation_pending
            and not state.exit_after_run
        ):
            state.cancellation_pending = True
            state.cancel_requested.set()
            state.approval.cancel_pending()
            self._write("CANCELLING — waiting for current operation")

    def _finish_compaction(self, state: InteractiveRunState) -> None:
        assert state.active_compaction is not None
        try:
            result = state.active_compaction.result()
        except Exception as exc:
            state.view.fail_compaction(str(exc))
        else:
            state.view.finish_compaction(
                compacted=result.compacted,
                previous_messages=result.previous_messages,
                remaining_messages=result.remaining_messages,
            )
        state.active_compaction = None

    def _finish_active_run(self, state: InteractiveRunState) -> None:
        assert state.active_run is not None
        try:
            state.active_run.result()
        except Exception as exc:
            self._write(f"ERROR {exc}")
        state.active_run = None
        state.cancel_requested.clear()
        state.suspend_requested.clear()
        state.cancellation_pending = False

    async def _handle_early_view_exit(
        self,
        state: InteractiveRunState,
        view_task: asyncio.Task[None],
        waiters: LoopWaiters,
        done: set[asyncio.Task[Any]],
        diagnostics: TuiDiagnosticLogger | None,
    ) -> None:
        state.view_ended_early = True
        if diagnostics is not None:
            diagnostics.record(
                "view_task_completed",
                {
                    "cancelled": view_task.cancelled(),
                    "cancelling": view_task.cancelling(),
                    "app_exit_requested": getattr(state.view, "_exit", None),
                    "message_pump_closed": getattr(state.view, "_closed", None),
                    "message_pump_closing": getattr(state.view, "_closing", None),
                    "textual_return_code": getattr(state.view, "return_code", None),
                },
            )
        try:
            view_task.result()
        except (EOFError, KeyboardInterrupt):
            state.normal_exit_requested = True
        except BaseException as error:
            state.view_task_error = error
        if state.active_run is not None:
            state.suspend_requested.set()
            state.approval.cancel_pending()
        await state.cancel_unused_waiters(waiters, done)
        if state.active_run is not None:
            try:
                await state.active_run
            except BaseException as error:
                if state.view_task_error is None:
                    state.view_task_error = error
            state.active_run = None

    async def _finish_interactive(
        self,
        state: InteractiveRunState,
        view_task: asyncio.Task[None],
        diagnostics: TuiDiagnosticLogger | None,
    ) -> TuiExitReport:
        try:
            state.view.stop()
        except BaseException as error:
            if state.view_task_error is None:
                state.view_task_error = error
        if not view_task.done():
            try:
                await view_task
            except (EOFError, KeyboardInterrupt):
                state.normal_exit_requested = True
            except BaseException as error:
                if state.view_task_error is None:
                    state.view_task_error = error
        report = classify_tui_exit(
            state.view,
            task_error=state.view_task_error,
            view_ended_early=state.view_ended_early,
            normal_exit_requested=state.normal_exit_requested,
        )
        if diagnostics is not None:
            diagnostics.record(
                "tui_exit",
                {
                    "reason": report.reason,
                    "exit_code": report.exit_code,
                    "textual_return_code": report.textual_return_code,
                    **report.snapshot,
                },
                report.error,
            )
            diagnostics.close()
        return report

    def _report_tui_error(
        self,
        report: TuiExitReport,
        diagnostics: TuiDiagnosticLogger | None,
    ) -> None:
        if report.error is not None:
            error_message = " ".join(str(report.error).splitlines())
            summary = f"{type(report.error).__name__}: {error_message}"
        else:
            summary = f"Textual return code {report.textual_return_code}"
        self._write(f"TUI ERROR {report.reason} — {summary}")
        session = self.active_session
        run_id = self.last_state.run_id if self.last_state is not None else getattr(self, "_last_tui_run_id", None)
        if session is not None or run_id is not None:
            self._write(f"TUI CONTEXT session={getattr(session, 'session_id', None)} run={run_id}")
        if diagnostics is not None:
            self._write(f"TUI DIAGNOSTICS {diagnostics.path.resolve()}")
