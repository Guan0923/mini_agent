"""Interactive terminal UI; it delegates all application setup to runtime.factory."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from backend.configuration import ClientPaths, initialize_config
from backend.observability import EventFanout, JsonlRunLogger
from backend.providers import ModelConfigurationError
from backend.runtime import (
    AgentRunner,
    ConversationService,
    RunnerSettings,
    RuntimeEvent,
    SessionStore,
    build_application,
)
from backend.storage.local_settings import LocalSettingsStore
from backend.tools import ToolError

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
        self._display_mode = "medium"
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
            from backend.runtime.conversation.references import FileReferenceExpander

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
        estimated = event.data.get(
            "current_input_tokens", event.data.get("input_tokens", event.data.get("estimated_tokens"))
        )
        cumulative = event.data.get("cumulative_input_tokens")
        context_size = event.data.get("context_size")
        target_ratio = event.data.get("target_ratio", 0.8)
        if isinstance(estimated, int) and isinstance(context_size, int) and isinstance(target_ratio, int | float):
            view.set_context_usage(
                estimated,
                context_size,
                float(target_ratio),
                cumulative_tokens=cumulative if isinstance(cumulative, int) else None,
            )

    def _present_runtime_event(self, event: RuntimeEvent) -> None:
        """Keep the console presenter as the non-interactive fallback."""

        if self._view is None:
            self.presenter.on_event(event)

    def _reset_context_usage(self) -> None:
        if self._view is not None:
            self._view.clear_context_usage()

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
    parser.add_argument("--max-tool-calls", type=int, help="Maximum accepted tool calls per task (default: 32).")
    parser.add_argument("--resume", metavar="SESSION_ID", help="Resume an existing workspace session by ID.")
    parser.add_argument(
        "--server",
        metavar="URL",
        default=None,
        help="Run as a network client against a backend server (e.g. http://127.0.0.1:8000).",
    )
    args = parser.parse_args(argv)
    tool_budget: dict[str, int] = {}
    if args.max_tool_calls is not None:
        tool_budget["max_tool_calls"] = args.max_tool_calls
    if args.server:
        return _run_network_task(args)
    workspace = args.workspace
    paths = ClientPaths.from_home()
    try:
        initialize_config(paths, workspace)
        local_settings = LocalSettingsStore(paths.state_db, paths.config_file)
        runtime_config = local_settings.runtime_config()
        model_config = local_settings.model_config() if args.planner == "llm" else None
        settings = RunnerSettings(
            max_transport_retries=5,
            strategy=args.strategy,
            max_tool_calls=tool_budget.get("max_tool_calls", int(runtime_config["max_tool_calls"])),
            log_full_messages=bool(runtime_config["log_full_messages"]),
        )
        application = build_application(
            workspace,
            args.planner,
            settings,
            paths=paths,
            user_preferences=local_settings.agent_preferences(),
            model_config=model_config,
            config_override={"runtime": runtime_config, "sandbox_config": local_settings.sandbox_config()},
            default_timezone=str(local_settings.agent_config()["timezone"]),
        )
        conversation = application.open_conversation(args.resume)
    except ModelConfigurationError as exc:
        parser.error(f"{exc} Use --planner rule for offline mode.")
    except (ToolError, ValueError) as exc:
        parser.error(str(exc))
    app = TerminalApp(conversation)
    try:
        app._run_trigger = "cli" if args.task else "tui"
        if args.task:
            if args.resume and conversation.prepare_resume(args.resume).requires_action:
                parser.error(
                    "The resumed session has recoverable work; run without a task and choose Continue or Back."
                )
            state = app.run_task(" ".join(args.task))
            return 0 if state is not None and state.status == "completed" else 1
        if args.resume:
            app._startup_resume_id = args.resume
        return app.start()
    finally:
        close = getattr(application, "close", None)
        if callable(close):
            close()


def _run_network_task(args) -> int:
    """Explain the remaining network-client boundary."""
    print("[client] 网络 TUI 任务执行已移除；请使用 Web 客户端创建和运行 Turn。")
    return 1
