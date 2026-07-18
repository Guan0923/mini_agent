"""Interactive terminal UI; it delegates all application setup to runtime.factory."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from concurrent.futures import Future
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
from mini_agent.runtime.contracts import (
    CancellationHandler,
    InterruptDecision,
    InterruptHandler,
    InterruptRequest,
    SteeringHandler,
)

from .approval import TerminalApproval
from .commands import COMMAND_ARGUMENT_NAMES, COMMAND_PATTERN, render_help
from .completion import SlashCommandCompleter
from .presenter import TerminalPresenter
from .view import ChoiceItem, TerminalView

HELP = render_help()


class _InteractiveApproval:
    """Move blocking runtime approvals onto the active Textual input loop."""

    def __init__(
        self,
        approval: TerminalApproval,
        loop: asyncio.AbstractEventLoop,
        view: TerminalView | None = None,
    ) -> None:
        self._approval = approval
        self._loop = loop
        self._view = view
        self._pending: tuple[InterruptRequest, Future[InterruptDecision]] | None = None
        self._supplement = False
        self.changed = asyncio.Event()

    @property
    def pending(self) -> bool:
        return self._pending is not None

    @property
    def prompt(self) -> str:
        if self._pending is None:
            return "mini-agent[review]> "
        return self._approval.input_prompt(self._pending[0], supplement=self._supplement)

    @property
    def status(self) -> str:
        if self._pending is None:
            return "REVIEW"
        request = self._pending[0]
        if self._supplement:
            return "TOOL REVIEW | Enter supplement"
        if request.kind == "question":
            return "PLAN QUESTIONS | Select answers"
        if request.kind == "plan":
            return "PLAN REVIEW | Select action"
        return "TOOL REVIEW | Select action"

    def __call__(self, request: InterruptRequest) -> InterruptDecision:
        automatic = self._approval.automatic_decision(request)
        if automatic is not None:
            return automatic
        decision: Future[InterruptDecision] = Future()
        self._loop.call_soon_threadsafe(self._open, request, decision)
        return decision.result()

    def submit(self, value: str) -> None:
        if self._pending is None:
            return
        request, future = self._pending
        decision, wants_supplement = self._approval.parse_input(
            request,
            value,
            supplement=self._supplement,
        )
        if decision is not None:
            self._pending = None
            self._supplement = False
            future.set_result(decision)
            self.changed.set()
            return
        if self._supplement and wants_supplement:
            self._approval.notify("Supplement cannot be empty.")
        elif not wants_supplement:
            self._approval.notify("Choose 1, 2, or 3.")
        self._supplement = wants_supplement

    def _open(self, request: InterruptRequest, decision: Future[InterruptDecision]) -> None:
        if self._pending is not None:
            decision.set_exception(RuntimeError("Only one terminal approval can be pending at a time."))
            return
        self._pending = (request, decision)
        self._supplement = False
        if (
            self._view is None
            or request.kind == "question"
            and not callable(getattr(self._view, "begin_questionnaire", None))
            or request.kind in {"plan", "tool"}
            and not callable(getattr(self._view, "begin_review", None))
        ):
            self._approval.render_request(request)
        elif request.kind == "question":
            self._view.begin_questionnaire(request.questions, self._complete_questionnaire)
        elif request.kind == "plan":
            self._approval.render_request(request)
            self._view.begin_review(
                "PLAN REVIEW",
                request.message,
                (
                    ChoiceItem("implement", "Implement", "Implement in the current session."),
                    ChoiceItem(
                        "implement_clear_session",
                        "Implement and Clear Session",
                        "Start implementation in a new session.",
                    ),
                    ChoiceItem("cancel", "Cancel and Stay in plan mode", "Do not implement this plan."),
                ),
                self._complete_review,
            )
        else:
            self._approval.render_request(request)
            self._view.begin_review(
                "TOOL REVIEW",
                request.message,
                (
                    ChoiceItem("continue", "Continue", "Run this tool call."),
                    ChoiceItem("cancel", "Cancel", "Stop the current run."),
                    ChoiceItem("supplement", "Supplement", "Send additional instructions.", custom=True),
                ),
                self._complete_review,
            )
        self.changed.set()

    def _complete_questionnaire(self, answers: dict[str, list[str]]) -> None:
        if self._pending is None or self._pending[0].kind != "question":
            return
        _request, future = self._pending
        self._pending = None
        self._supplement = False
        future.set_result(InterruptDecision("answer", answers=answers))
        self.changed.set()

    def _complete_review(self, choice: str, supplement: str | None) -> None:
        if self._pending is None or self._pending[0].kind not in {"plan", "tool"}:
            return
        request, future = self._pending
        allowed = (
            {"implement", "implement_clear_session", "cancel"}
            if request.kind == "plan"
            else {"continue", "cancel", "supplement"}
        )
        self._pending = None
        self._supplement = False
        if choice not in allowed:
            future.set_exception(ValueError(f"Invalid {request.kind} review choice: {choice}"))
        else:
            future.set_result(InterruptDecision(choice, supplement=supplement))
        self.changed.set()

    def cancel_pending(self) -> None:
        """Resolve an active review so cooperative run cancellation can continue."""

        if self._pending is None:
            return
        _request, future = self._pending
        self._pending = None
        self._supplement = False
        cancel_prompt = getattr(self._view, "cancel_choice_prompt", None)
        if callable(cancel_prompt):
            cancel_prompt()
        future.set_result(InterruptDecision("cancel"))
        self.changed.set()


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
        if isinstance(runner, ConversationService):
            if session_store is not None or session_id is not None:
                raise ValueError("Provide a composed conversation or legacy runner/session arguments, not both.")
            self._conversation_service = runner
            self.runner = runner.runner
        else:
            # Compatibility for direct embedding callers. The normal CLI uses
            # ``AgentApplication`` so the TUI does not construct runtime dependencies.
            from mini_agent.runtime.references import FileReferenceExpander

            self.runner = runner
            self._conversation_service = ConversationService(
                runner,
                session_store,
                FileReferenceExpander(runner.tools),
                session_id,
            )
        self.presenter = TerminalPresenter(self._write)
        self._approval = TerminalApproval(write=self._write)
        sinks = [self._handle_runtime_event, self.presenter.on_event]
        if log_dir is not None:
            sinks.append(JsonlRunLogger(log_dir, include_full_messages=self.runner.settings.log_full_messages))
        self._event_sink = EventFanout(sinks)

    def _handle_runtime_event(self, event: RuntimeEvent) -> None:
        if event.kind != "context_usage" or self._view is None:
            return
        estimated = event.data.get("estimated_tokens")
        context_size = event.data.get("context_size")
        threshold = event.data.get("threshold", 0.8)
        if (
            isinstance(estimated, int)
            and isinstance(context_size, int)
            and isinstance(threshold, int | float)
        ):
            self._view.set_context_usage(estimated, context_size, float(threshold))

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

    def start(self) -> None:
        asyncio.run(self._start_interactive())

    async def _start_interactive(self) -> None:
        loop = asyncio.get_running_loop()
        view = TerminalView(loop, completer=SlashCommandCompleter())
        self._view = view
        self._write("Mini-Agent TUI — type /help for commands, /quit to exit.")
        self._print_active_session()
        pending_messages: Queue[str] = Queue()
        approval = _InteractiveApproval(self._approval, loop, view)
        active_run: asyncio.Task[RunState | None] | None = None
        permission_pending = False
        deferred_task: str | None = None
        cancel_requested = Event()
        cancellation_pending = False
        exit_after_run = False

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
                waiters: set[asyncio.Task[object]] = {  # type: ignore[arg-type]
                    submission,
                    interrupt_request,
                    approval_change,
                    view_task,
                }
                if active_run is not None:
                    waiters.add(active_run)  # type: ignore[arg-type]
                done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)

                if view_task in done:
                    try:
                        view_task.result()
                    except (EOFError, KeyboardInterrupt):
                        pass
                    if submission not in done:
                        await self._cancel_task(submission)
                    if interrupt_request not in done:
                        await self._cancel_task(interrupt_request)
                    if approval_change not in done:
                        await self._cancel_task(approval_change)
                    break

                exit_requested = False
                run_finished = active_run is not None and active_run in done

                approval_changed = approval_change in done
                if approval_changed:
                    approval.changed.clear()
                    if (cancellation_pending or exit_after_run) and approval.pending:
                        approval.cancel_pending()

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
                            selected = self._approval.parse_permission(task)
                            if selected is None:
                                self._write("Choose 1 or 2.")
                            else:
                                self._approval.set_permission(selected)
                                permission_pending = False
                                if deferred_task is not None:
                                    self._write_user_message(deferred_task)
                                    active_run = launch(deferred_task)
                                    deferred_task = None
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
                                self._approval.render_permission()
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
                if exit_requested:
                    break
        finally:
            view.stop()
            if not view_task.done():
                await view_task
            self._view = None
        self._print_resume_hint()

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
        for kind, value, argument in self._split_input(task):
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
        for kind, value, argument in self._split_input(task):
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
            status = "PERMISSION | 1 Approval for me | 2 Full access"
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
            view.write(text, end)
            return
        print(text, end=end, flush=end == "")

    def _write_user_message(self, content: str) -> None:
        """Echo only content that is about to enter the conversation."""

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
        for kind, value, argument in self._split_input(task):
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
        if self.session_store is None:
            self._write("Session storage is not configured.")
            return
        if self.active_session is None:
            self._write(
                "No conversation history."
                if self._conversation_service.pending_session_title is not None
                else "No active session."
            )
            return
        messages = self._conversation_service.history()
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
    parser.add_argument("--max-actions", type=int, default=8, help="Maximum model decisions per task (default: 8).")
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
    workspace = args.workspace
    try:
        settings = RunnerSettings(
            max_actions=args.max_actions,
            max_retries=args.max_retries,
            max_model_repairs=args.max_model_repairs,
            max_transport_retries=args.max_transport_retries,
            max_tool_recoveries=args.max_tool_recoveries,
            max_replans=args.max_replans,
            strategy=args.strategy,
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
    app.start()
    return 0
