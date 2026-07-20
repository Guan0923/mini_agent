"""Interactive terminal UI; it delegates all application setup to runtime.factory."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Event

from mini_agent.domain import RunState
from mini_agent.observability import EventFanout, JsonlRunLogger
from mini_agent.providers import ModelConfigurationError
from mini_agent.runtime import (
    AgentRunner,
    ConversationService,
    RunnerSettings,
    RuntimeEvent,
    SessionStore,
    TaskPreparationError,
    build_application,
    log_full_messages_from_env,
)
from mini_agent.runtime.core.contracts import CancellationHandler, InterruptHandler, SteeringHandler

from .approval import TerminalApproval
from .commands import COMMAND_ARGUMENT_NAMES, COMMAND_PATTERN, render_help
from .completion import SlashCommandCompleter
from .diagnostics import TuiDiagnosticLogger
from .exit_reporting import classify_tui_exit
from .interactive_approval import InteractiveApproval as _InteractiveApproval
from .presenter import TerminalPresenter
from .view import TerminalView
from .widgets import ChoiceItem

HELP = render_help()


class TerminalApp:
    def __init__(
        self,
        runner: AgentRunner | ConversationService,
        log_dir: Path | None = None,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
    ) -> None:
        self.last_state = None
        self.mode = "agent"
        self._view: TerminalView | None = None
        self._last_tui_run_id: str | None = None
        self._log_dir = log_dir
        self._tui_diagnostics: TuiDiagnosticLogger | None = None
        if isinstance(runner, ConversationService):
            if session_store is not None or session_id is not None:
                raise ValueError("Provide a composed conversation or legacy runner/session arguments, not both.")
            self._conversation_service = runner
            self.runner = runner.runner
        else:
            # Compatibility for direct embedding callers. The normal CLI uses
            # ``AgentApplication`` so the TUI does not construct runtime dependencies.
            from mini_agent.runtime.conversation.references import FileReferenceExpander

            self.runner = runner
            self._conversation_service = ConversationService(
                runner,
                session_store,
                FileReferenceExpander(runner.tools),
                session_id,
            )
        self.presenter = TerminalPresenter(self._write)
        self._approval = TerminalApproval(write=self._write)
        sinks = [self._handle_runtime_event, self._present_runtime_event]
        if log_dir is not None:
            sinks.append(JsonlRunLogger(log_dir, include_full_messages=self.runner.settings.log_full_messages))
        self._event_sink = EventFanout(sinks)

    def _handle_runtime_event(self, event: RuntimeEvent) -> None:
        view = self._view
        diagnostics = getattr(self, "_tui_diagnostics", None)
        if diagnostics is not None:
            run_id = event.data.get("run_id")
            if isinstance(run_id, str):
                self._last_tui_run_id = run_id
            session = self.active_session
            diagnostics.set_context(
                session_id=session.session_id if session is not None else None,
                run_id=run_id if isinstance(run_id, str) else None,
            )
        if view is None:
            return
        handle_event = getattr(view, "handle_runtime_event", None)
        if callable(handle_event):
            handle_event(event)
        if event.kind != "context_usage":
            return
        estimated = event.data.get("estimated_tokens")
        context_size = event.data.get("context_size")
        threshold = event.data.get("threshold", 0.8)
        if (
            isinstance(estimated, int)
            and isinstance(context_size, int)
            and isinstance(threshold, int | float)
        ):
            view.set_context_usage(estimated, context_size, float(threshold))

    def _present_runtime_event(self, event: RuntimeEvent) -> None:
        """Keep the console presenter as the non-interactive fallback."""

        if self._view is None:
            self.presenter.on_event(event)

    def _reset_context_usage(self) -> None:
        if self._view is not None:
            self._view.set_context_usage()

    @property
    def session_store(self) -> SessionStore | None:
        """Expose session storage for existing embedders without owning it in the TUI."""

        return self._conversation_service.session_store

    @property
    def active_session(self):
        """Return the currently active conversation session, if persistence is enabled."""

        return self._conversation_service.active_session

    @property
    def conversation(self) -> list[dict[str, str]]:
        """Return the in-memory context maintained by the application service."""

        return self._conversation_service.conversation

    def start(self) -> int:
        return asyncio.run(self._start_interactive())

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
        )
        if diagnostics is not None:
            diagnostics.record("tui_started", {"mode": self.mode})
        self._view = view
        self._write("Mini-Agent TUI — type /help for commands, /quit to exit.")
        self._print_active_session()
        pending_messages: Queue[str] = Queue()
        permission_decisions: asyncio.Queue[str] = asyncio.Queue()
        approval = _InteractiveApproval(self._approval, loop, view)
        active_run: asyncio.Task[RunState | None] | None = None
        permission_pending = False
        deferred_task: str | None = None
        cancel_requested = Event()
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
                )
            )

        view_task = asyncio.create_task(view.run_async())
        try:
            while True:
                self._update_view_state(
                    view,
                    active_run is not None,
                    approval,
                    permission_pending,
                    cancelling=cancellation_pending or exit_after_run,
                )
                submission = asyncio.create_task(view.submissions.get())
                interrupt_request = asyncio.create_task(view.interrupts.get())
                approval_change = asyncio.create_task(approval.changed.wait())
                permission_change = (
                    asyncio.create_task(permission_decisions.get()) if permission_pending else None
                )
                waiters: set[asyncio.Task[object]] = {  # type: ignore[arg-type]
                    submission,
                    interrupt_request,
                    approval_change,
                    view_task,
                }
                if active_run is not None:
                    waiters.add(active_run)  # type: ignore[arg-type]
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
                    if submission not in done:
                        await self._cancel_task(submission)
                    if interrupt_request not in done:
                        await self._cancel_task(interrupt_request)
                    if approval_change not in done:
                        await self._cancel_task(approval_change)
                    if permission_change is not None and permission_change not in done:
                        await self._cancel_task(permission_change)
                    break

                exit_requested = False
                run_finished = active_run is not None and active_run in done

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
                        if active_run is not None or approval.pending:
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
                            if active_run is not None or approval.pending:
                                if not exit_after_run:
                                    exit_after_run = True
                                    cancel_requested.set()
                                    self._drain_steering(pending_messages)
                                    approval.cancel_pending()
                                    self._write("CANCELLING — waiting for current operation")
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
                        elif active_run is not None:
                            if task:
                                if any(kind == "command" for kind, _value, _argument in self._split_input(task)):
                                    self._write("Commands are unavailable while the agent is running.")
                                else:
                                    self._write_user_message(task)
                                    pending_messages.put(task)
                                    self._write("MESSAGE QUEUED")
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

                if interrupt_request in done:
                    if (active_run is not None or approval.pending) and not cancellation_pending and not exit_after_run:
                        cancellation_pending = True
                        cancel_requested.set()
                        approval.cancel_pending()
                        self._write("CANCELLING — waiting for current operation")

                if run_finished:
                    assert active_run is not None
                    try:
                        active_run.result()
                    except Exception as exc:
                        self._write(f"ERROR {exc}")
                    active_run = None
                    cancel_requested.clear()
                    cancellation_pending = False
                    if exit_after_run:
                        exit_requested = True
                    else:
                        queued = self._drain_steering(pending_messages)
                        if queued:
                            self._write(f"QUEUE STARTED — {len(queued)} queued message(s)")
                            active_run = launch("\n\n".join(queued))

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
            run_id = (
                self.last_state.run_id if self.last_state is not None else getattr(self, "_last_tui_run_id", None)
            )
            if session is not None or run_id is not None:
                self._write(f"TUI CONTEXT session={getattr(session, 'session_id', None)} run={run_id}")
            if diagnostics is not None:
                self._write(f"TUI DIAGNOSTICS {diagnostics.path.resolve()}")
        self._print_resume_hint()
        return report.exit_code

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

    def _handle(self, task: str) -> bool:
        if not task:
            return True
        parts = self._split_input(task)
        if any(kind == "command" and value == "history" for kind, value, _argument in parts):
            if task.strip() != "/history":
                self._write("Usage: /history")
                return True
        for kind, value, argument in parts:
            if kind == "task":
                self.run_task(value)
                continue
            if not self._handle_command(value, argument):
                return False
        return True

    @staticmethod
    def _split_input(value: str) -> list[tuple[str, str, str]]:
        """Run recognized commands first, then return at most one merged task."""

        matches = list(COMMAND_PATTERN.finditer(value))
        if not matches:
            return [("task", value, "")]

        commands: list[tuple[str, str, str]] = []
        task_parts = [value[: matches[0].start()]]
        for index, match in enumerate(matches):
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            command = match.group("name")
            following_text = value[match.end() : next_start]
            if command == "quit":
                commands.append(("command", command, ""))
                return commands
            if command in COMMAND_ARGUMENT_NAMES:
                commands.append(("command", command, following_text.strip()))
            else:
                commands.append(("command", command, ""))
                task_parts.append(following_text)

        merged_task = " ".join(part.strip() for part in task_parts if part.strip())
        if merged_task:
            commands.append(("task", merged_task, ""))
        return commands

    def _handle_command(self, command: str, argument: str) -> bool:
        if command == "agent":
            if argument:
                self._write("Usage: /agent")
                return True
            self.mode = "agent"
            self._write("Agent mode enabled.")
            return True
        if command == "plan":
            if argument:
                self._write("Usage: /plan")
                return True
            self.mode = "plan"
            self._write("Plan mode enabled: read-only discussion with Plan Review available when needed. Use /agent to return to Agent mode.")
            return True
        if command == "permission":
            if argument:
                self._write("Usage: /permission")
                return True
            self._approval.configure_permission()
            return True
        if command == "sessions":
            if argument:
                self._write("Usage: /sessions")
                return True
            self._show_sessions()
            return True
        if command == "session":
            if argument:
                self._write("Usage: /session")
                return True
            self._show_session()
            return True
        if command == "history":
            if argument:
                self._write("Usage: /history")
                return True
            self._show_history()
            return True
        if command in {"new", "clear"}:
            self._new_session(argument or None)
            return True
        if command == "use":
            self._use_session(argument)
            return True
        if command == "quit":
            if argument:
                self._write("Usage: /quit")
                return True
            self._write("Bye.")
            return False
        if command == "help":
            if argument:
                self._write("Usage: /help")
                return True
            self._write(HELP)
            return True
        if command == "tools":
            if argument:
                self._write("Usage: /tools")
                return True
            self._write("\n".join(self.runner.tools.names()))
            return True
        if command == "trace":
            if argument:
                self._write("Usage: /trace")
                return True
            self._write(
                json.dumps(self.last_state.to_dict(), ensure_ascii=False, indent=2)
                if self.last_state
                else "No run yet."
            )
            return True
        return True

    def _ensure_session(self, title: str | None = None):
        if self.session_store is None:
            raise RuntimeError("Session storage is not configured.")
        created = self.active_session is None
        session = self._conversation_service.ensure_session(title)
        if created:
            self._print_active_session()
        return session

    def _new_session(self, title: str | None) -> None:
        if self.session_store is None:
            self._write("Session storage is not configured.")
            return
        self._clear_display()
        self._reset_context_usage()
        self._conversation_service.prepare_new_session(title)
        self.last_state = None
        self._print_active_session()

    def _use_session(self, session_id: str) -> None:
        if self.session_store is None:
            self._write("Session storage is not configured.")
            return
        if not session_id:
            self._write("Usage: /use <session_id>")
            return
        try:
            self._conversation_service.use_session(session_id)
        except ValueError:
            self._write(f"Unknown session: {session_id}")
            return
        self._reset_context_usage()
        self.last_state = None
        self._print_active_session()

    def _show_sessions(self) -> None:
        if self.session_store is None:
            self._write("Session storage is not configured.")
            return
        sessions = self._conversation_service.list_sessions()
        if not sessions:
            self._write("No saved sessions.")
            return
        for session in sessions:
            marker = "*" if self.active_session and session.session_id == self.active_session.session_id else " "
            self._write(
                f"{marker} {session.session_id} — {session.title} "
                f"({session.message_count} messages, updated {session.updated_at})"
            )

    def _show_session(self) -> None:
        if self.session_store is None:
            self._write("Session storage is not configured.")
            return
        pending_title = self._conversation_service.pending_session_title
        if pending_title is not None:
            self._write("SESSION PENDING")
            self._write(f"TITLE {pending_title}")
            self._write("MESSAGES 0")
            self._write("STATUS Not saved yet")
            return
        if self.active_session is None:
            self._write("No active session.")
            return
        summary = self._conversation_service.current_summary()
        if summary is None:
            self._write("No active session.")
            return
        self._write(f"SESSION {summary.session_id}")
        self._write(f"TITLE {summary.title}")
        self._write(f"MESSAGES {summary.message_count}")
        self._write(f"CREATED {summary.created_at}")
        self._write(f"UPDATED {summary.updated_at}")
        if summary.last_run_id:
            self._write(f"LAST RUN {summary.last_run_id} {summary.last_run_status}")

    def _show_history(self) -> None:
        view = getattr(self, "_view", None)
        show_history = getattr(view, "show_history", None)
        if self.session_store is None:
            if callable(show_history):
                show_history("No session storage", [])
            else:
                self._write("Session storage is not configured.")
            return
        if self.active_session is None:
            if callable(show_history):
                pending = self._conversation_service.pending_session_title
                show_history(f"Pending: {pending}" if pending else "No active session", [])
            else:
                self._write(
                    "No conversation history."
                    if self._conversation_service.pending_session_title is not None
                    else "No active session."
                )
            return
        messages = self._conversation_service.history()
        if callable(show_history):
            session = self.active_session
            show_history(f"{session.session_id} — {session.title}", messages)
            return
        if not messages:
            self._write("No conversation history.")
            return
        self._write(f"HISTORY {self.active_session.session_id}")
        for message in messages:
            role = message["role"].upper()
            self._write(f"{role}\n{message['content']}")
    def _print_active_session(self) -> None:
        if self.active_session is not None:
            self._write(f"SESSION {self.active_session.session_id} — {self.active_session.title}")
            return
        pending_title = self._conversation_service.pending_session_title
        if pending_title is not None:
            self._write(f"SESSION PENDING — {pending_title} (not saved yet)")

    @staticmethod
    def _clear_terminal() -> None:
        os.system("cls" if os.name == "nt" else "clear")


def _existing_directory(value: str) -> Path:
    workspace = Path(value).expanduser()
    if not workspace.exists():
        raise argparse.ArgumentTypeError(f"workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise argparse.ArgumentTypeError(f"workspace is not a directory: {workspace}")
    return workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mini-Agent terminal lab")
    parser.add_argument("task", nargs="*", help="Run one task and exit.")
    parser.add_argument(
        "--workspace",
        type=_existing_directory,
        default=Path("."),
        help="Existing workspace directory available to tools (default: current directory).",
    )
    parser.add_argument("--planner", choices=("llm", "rule"), default="llm", help="Planning strategy (default: llm).")
    parser.add_argument(
        "--strategy",
        choices=("auto", "reactive", "dynamic_replan"),
        default="auto",
        help="Execution strategy override (default: auto).",
    )
    parser.add_argument(
        "--max-model-turns", type=int, default=8, help="Maximum logical model turns per task (default: 8)."
    )
    parser.add_argument("--max-tool-calls", type=int, help="Maximum accepted tool calls per task (default: 32).")
    parser.add_argument(
        "--max-actions",
        type=int,
        help="Deprecated alias for --max-tool-calls; cannot be combined with it.",
    )
    parser.add_argument("--max-retries", type=int, default=1, help="Retries for a failed tool call (default: 1).")
    parser.add_argument(
        "--max-model-repairs",
        type=int,
        default=1,
        help="Retries for malformed model output (default: 1).",
    )
    parser.add_argument(
        "--max-transport-retries",
        type=int,
        default=2,
        help="Retries for transient model transport failures (default: 2).",
    )
    parser.add_argument(
        "--max-tool-recoveries",
        type=int,
        default=2,
        help="Consecutive LLM recovery decisions after tool failures (default: 2).",
    )
    parser.add_argument("--max-replans", type=int, default=2, help="Maximum dynamic replans per task (default: 2).")
    parser.add_argument("--log-dir", default="logs", help="Directory for persistent JSONL run logs (default: logs).")
    parser.add_argument("--session-id", help="Resume an existing workspace session by ID.")
    args = parser.parse_args(argv)
    if args.max_actions is not None and args.max_tool_calls is not None:
        parser.error("--max-actions and --max-tool-calls cannot be used together.")
    tool_budget: dict[str, int] = {}
    if args.max_actions is not None:
        tool_budget["max_actions"] = args.max_actions
    elif args.max_tool_calls is not None:
        tool_budget["max_tool_calls"] = args.max_tool_calls
    workspace = args.workspace
    try:
        settings = RunnerSettings(
            max_model_turns=args.max_model_turns,
            max_retries=args.max_retries,
            max_model_repairs=args.max_model_repairs,
            max_transport_retries=args.max_transport_retries,
            max_tool_recoveries=args.max_tool_recoveries,
            max_replans=args.max_replans,
            strategy=args.strategy,
            **tool_budget,
            log_full_messages=log_full_messages_from_env(workspace / ".env"),
        )
        application = build_application(workspace, args.planner, settings)
        conversation = application.open_conversation(args.session_id)
    except ModelConfigurationError as exc:
        parser.error(f"{exc} Use --planner rule for offline mode.")
    except ValueError as exc:
        parser.error(str(exc))
    app = TerminalApp(conversation, workspace / args.log_dir)
    if args.task:
        state = app.run_task(" ".join(args.task))
        return 0 if state is not None and state.status == "completed" else 1
    return app.start()
