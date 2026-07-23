"""Interactive terminal UI; it delegates all application setup to runtime.factory."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from mini_agent.observability import EventFanout, JsonlRunLogger
from mini_agent.providers import ModelConfigurationError
from mini_agent.runtime import (
    AgentRunner,
    ConversationService,
    RunnerSettings,
    RuntimeEvent,
    SessionStore,
    build_application,
    log_full_messages_from_env,
)

from .application.commands import CommandAppMixin
from .application.interactive import InteractiveAppMixin
from .application.tasks import TaskAppMixin
from .components.approval import TerminalApproval
from .rendering.presenter import TerminalPresenter
from .screens.diagnostics import TuiDiagnosticLogger
from .view import TerminalView


class TerminalApp(InteractiveAppMixin, TaskAppMixin, CommandAppMixin):
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
        if isinstance(estimated, int) and isinstance(context_size, int) and isinstance(threshold, int | float):
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
        default=2,
        help="Retries for malformed model output (default: 2).",
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
    parser.add_argument("--resume", metavar="SESSION_ID", help="Resume an existing workspace session by ID.")
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
        conversation = application.open_conversation(args.resume)
    except ModelConfigurationError as exc:
        parser.error(f"{exc} Use --planner rule for offline mode.")
    except ValueError as exc:
        parser.error(str(exc))
    app = TerminalApp(conversation, workspace / args.log_dir)
    app._run_trigger = "cli" if args.task else "tui"
    if args.task:
        if args.resume and conversation.prepare_resume(args.resume).requires_action:
            parser.error("The resumed session has suspended work; run without a task and choose Continue or Terminate.")
        state = app.run_task(" ".join(args.task))
        return 0 if state is not None and state.status == "completed" else 1
    if args.resume:
        app._startup_resume_id = args.resume
    return app.start()
