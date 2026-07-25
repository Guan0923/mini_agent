"""Asynchronous Textual event-loop orchestration."""

from __future__ import annotations

import asyncio
from queue import Queue
from threading import Event

from backend.domain import RunState

from ..components.completion import SlashCommandCompleter
from ..components.interactive_approval import InteractiveApproval as _InteractiveApproval
from ..screens.diagnostics import TuiDiagnosticLogger
from ..screens.exit_reporting import classify_tui_exit
from ..view import TerminalView
from ..widgets import ChoiceItem


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
        pending_messages: Queue[str] = Queue()
        permission_decisions: asyncio.Queue[str] = asyncio.Queue()
        approval = _InteractiveApproval(self._approval, loop, view)
        active_run: asyncio.Task[RunState | None] | None = None
        active_compaction: asyncio.Task[object] | None = None
        permission_pending = False
        deferred_task: str | None = None
        cancel_requested = Event()
        suspend_requested = Event()
        cancellation_pending = False
        exit_after_run = False
        normal_exit_requested = False
        view_ended_early = False
        view_task_error: BaseException | None = None

        def launch(task: str) -> asyncio.Task[RunState | None]:
            return asyncio.create_task(
                asyncio.to_thread(
                    self.run_task,
                    task,
                    interrupt=approval,
                    cancel_requested=cancel_requested.is_set,
                    suspend_requested=suspend_requested.is_set,
                )
            )

        def launch_resume(session_id: str | None) -> asyncio.Task[RunState | None]:
            return asyncio.create_task(
                asyncio.to_thread(
                    self.resume_session,
                    session_id,
                    interrupt=approval,
                    cancel_requested=cancel_requested.is_set,
                    suspend_requested=suspend_requested.is_set,
                )
            )

        def launch_compaction() -> asyncio.Task[object]:
            return asyncio.create_task(asyncio.to_thread(self._conversation_service.compact_context))

        def launch_queued_messages() -> asyncio.Task[RunState | None] | None:
            queued = self._drain_steering(pending_messages)
            if not queued:
                return None
            task = "\n\n".join(queued)
            self._clear_queued_messages()
            self._write(f"QUEUE STARTED — {len(queued)} queued message(s)")
            self._write_user_message(task)
            return launch(task)

        startup_resume_id = getattr(self, "_startup_resume_id", None)
        if startup_resume_id is not None:
            active_run = launch_resume(startup_resume_id)
            self._startup_resume_id = None

        view_task = asyncio.create_task(view.run_async())
        try:
            while True:
                self._update_view_state(
                    view,
                    active_run is not None,
                    approval,
                    permission_pending,
                    compacting=active_compaction is not None,
                    cancelling=cancellation_pending,
                    suspending=exit_after_run,
                )
                submission = asyncio.create_task(view.submissions.get())
                interrupt_request = asyncio.create_task(view.interrupts.get())
                approval_change = asyncio.create_task(approval.changed.wait())
                permission_change = asyncio.create_task(permission_decisions.get()) if permission_pending else None
                waiters: set[asyncio.Task[object]] = {  # type: ignore[arg-type]
                    submission,
                    interrupt_request,
                    approval_change,
                    view_task,
                }
                if active_run is not None:
                    waiters.add(active_run)  # type: ignore[arg-type]
                if active_compaction is not None:
                    waiters.add(active_compaction)  # type: ignore[arg-type]
                if permission_change is not None:
                    waiters.add(permission_change)  # type: ignore[arg-type]
                done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

                if view_task in done:
                    view_ended_early = True
                    if diagnostics is not None:
                        diagnostics.record(
                            "view_task_completed",
                            {
                                "cancelled": view_task.cancelled(),
                                "cancelling": view_task.cancelling(),
                                "app_exit_requested": getattr(view, "_exit", None),
                                "message_pump_closed": getattr(view, "_closed", None),
                                "message_pump_closing": getattr(view, "_closing", None),
                                "textual_return_code": getattr(view, "return_code", None),
                            },
                        )
                    try:
                        view_task.result()
                    except (EOFError, KeyboardInterrupt):
                        normal_exit_requested = True
                    except BaseException as error:
                        view_task_error = error
                    if active_run is not None:
                        suspend_requested.set()
                        approval.cancel_pending()
                    if submission not in done:
                        await self._cancel_task(submission)
                    if interrupt_request not in done:
                        await self._cancel_task(interrupt_request)
                    if approval_change not in done:
                        await self._cancel_task(approval_change)
                    if permission_change is not None and permission_change not in done:
                        await self._cancel_task(permission_change)
                    if active_run is not None:
                        try:
                            await active_run
                        except BaseException as error:
                            if view_task_error is None:
                                view_task_error = error
                        active_run = None
                    break

                exit_requested = False
                run_finished = active_run is not None and active_run in done
                compaction_finished = active_compaction is not None and active_compaction in done

                approval_changed = approval_change in done
                if approval_changed:
                    approval.changed.clear()
                    if (cancellation_pending or exit_after_run) and approval.pending:
                        approval.cancel_pending()

                if permission_change is not None and permission_change in done:
                    selected = permission_change.result()
                    permission_pending = False
                    if selected != "cancel":
                        mode = self._approval.parse_permission(selected)
                        if mode is None:
                            raise ValueError(f"Invalid permission choice: {selected}")
                        self._approval.set_permission(mode, announce=False)
                        if deferred_task is not None:
                            self._write_user_message(deferred_task)
                            active_run = launch(deferred_task)
                    deferred_task = None

                if submission in done:
                    submitted = submission.result()
                    if submitted is None:
                        if active_run is not None or approval.pending or active_compaction is not None:
                            self._write("Agent is still running; finish the active review before exiting.")
                        elif permission_pending:
                            permission_pending = False
                            deferred_task = None
                            self._write("Permission selection cancelled.")
                        else:
                            exit_requested = True
                    else:
                        task = submitted.strip()
                        if task == "/quit":
                            if active_run is not None or approval.pending or active_compaction is not None:
                                if not exit_after_run:
                                    exit_after_run = True
                                    suspend_requested.set()
                                    self._drain_steering(pending_messages)
                                    approval.cancel_pending()
                                    self._write("SUSPENDING — waiting for a safe checkpoint")
                            else:
                                if permission_pending:
                                    permission_pending = False
                                    deferred_task = None
                                self._write("Bye.")
                                exit_requested = True
                        elif exit_after_run:
                            pass
                        elif permission_pending:
                            pass
                        elif active_run is not None or active_compaction is not None:
                            if task:
                                if task == "/history":
                                    self._show_history()
                                elif any(kind == "command" for kind, _value, _argument in self._split_input(task)):
                                    self._write("Commands are unavailable while the agent is running.")
                                else:
                                    pending_messages.put(task)
                                    self._write_queued_message(task)
                                    self._write("MESSAGE QUEUED")
                        else:
                            parts = self._split_input(task)
                            has_compact = any(
                                kind == "command" and value == "compact" for kind, value, _argument in parts
                            )
                            if has_compact:
                                if task != "/compact":
                                    self._write("Usage: /compact")
                                else:
                                    view.begin_compaction()
                                    active_compaction = launch_compaction()
                            else:
                                keep_running, next_task, request_permission = self._handle_view_input(task)
                                if not keep_running:
                                    exit_requested = True
                                if request_permission:
                                    permission_pending = True
                                    deferred_task = next_task
                                    current = self._approval.permission_mode.replace("_", " ").title()
                                    view.begin_review(
                                        "PERMISSION",
                                        f"Current: {current}",
                                        "Choose how tools that require confirmation are handled.",
                                        (
                                            ChoiceItem(
                                                "approval_for_me",
                                                "Approval for me",
                                                "Ask before tools that require confirmation.",
                                            ),
                                            ChoiceItem(
                                                "full_access",
                                                "Full access",
                                                "Automatically approve tool calls.",
                                            ),
                                            ChoiceItem("cancel", "Cancel"),
                                        ),
                                        lambda choice, _supplement: permission_decisions.put_nowait(choice),
                                    )
                                elif next_task is not None:
                                    self._write_user_message(next_task)
                                    active_run = launch(next_task)
                                pending_resume_id = getattr(self, "_pending_resume_id", ...)
                                if pending_resume_id is not ...:
                                    self._pending_resume_id = ...
                                    active_run = launch_resume(pending_resume_id)

                if interrupt_request in done:
                    if (active_run is not None or approval.pending) and not cancellation_pending and not exit_after_run:
                        cancellation_pending = True
                        cancel_requested.set()
                        approval.cancel_pending()
                        self._write("CANCELLING — waiting for current operation")

                if compaction_finished:
                    assert active_compaction is not None
                    try:
                        result = active_compaction.result()
                    except Exception as exc:
                        view.fail_compaction(str(exc))
                    else:
                        view.finish_compaction(
                            compacted=result.compacted,
                            previous_messages=result.previous_messages,
                            remaining_messages=result.remaining_messages,
                        )
                    active_compaction = None
                    if exit_after_run:
                        exit_requested = True
                    else:
                        active_run = launch_queued_messages()

                if run_finished:
                    assert active_run is not None
                    try:
                        active_run.result()
                    except Exception as exc:
                        self._write(f"ERROR {exc}")
                    active_run = None
                    cancel_requested.clear()
                    suspend_requested.clear()
                    cancellation_pending = False
                    if exit_after_run:
                        exit_requested = True
                    else:
                        active_run = launch_queued_messages()

                if submission not in done:
                    await self._cancel_task(submission)
                if interrupt_request not in done:
                    await self._cancel_task(interrupt_request)
                if approval_change not in done:
                    await self._cancel_task(approval_change)
                if permission_change is not None and permission_change not in done:
                    await self._cancel_task(permission_change)
                if exit_requested:
                    normal_exit_requested = True
                    break
        finally:
            try:
                view.stop()
            except BaseException as error:
                if view_task_error is None:
                    view_task_error = error
            if not view_task.done():
                try:
                    await view_task
                except (EOFError, KeyboardInterrupt):
                    normal_exit_requested = True
                except BaseException as error:
                    if view_task_error is None:
                        view_task_error = error
            report = classify_tui_exit(
                view,
                task_error=view_task_error,
                view_ended_early=view_ended_early,
                normal_exit_requested=normal_exit_requested,
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
            self._tui_diagnostics = None
            self._view = None
        if report.exit_code:
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
        self._print_resume_hint()
        return report.exit_code
