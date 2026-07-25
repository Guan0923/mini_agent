from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.domain import RunState
from tui import cli
from tui.components.approval import TerminalApproval


class StubConversation:
    def __init__(self, result: RunState | Exception) -> None:
        self.active_session = None
        self.pending_session_title = None
        self.next_active_session = None
        self.result = result

    def run_task(self, task: str, **kwargs) -> RunState:
        if isinstance(self.result, Exception):
            raise self.result
        if self.next_active_session is not None:
            self.active_session = self.next_active_session
        return self.result


def build_terminal_app(result: RunState | Exception) -> cli.TerminalApp:
    app = object.__new__(cli.TerminalApp)
    app.last_state = None
    app.mode = "agent"
    app._display_mode = "medium"
    app._conversation_service = StubConversation(result)
    app._event_sink = lambda event: None
    app._approval = TerminalApproval()
    return app


def test_run_task_returns_run_state() -> None:
    state = RunState(task="calculate", mode="agent", status="completed")
    app = build_terminal_app(state)

    assert app.run_task("calculate") is state
    assert app.last_state is state


class StubTerminalApp:
    result: RunState | None = None
    started = False

    def __init__(self, conversation, log_dir: Path) -> None:
        self.conversation = conversation
        self.log_dir = log_dir

    def run_task(self, task: str) -> RunState | None:
        return self.result

    def start(self) -> int:
        type(self).started = True
        return 0


@pytest.fixture
def stub_cli(monkeypatch):
    application = SimpleNamespace(open_conversation=lambda session_id: object())
    monkeypatch.setattr(cli, "build_application", lambda *args: application)
    monkeypatch.setattr(cli, "TerminalApp", StubTerminalApp)
    StubTerminalApp.result = None
    StubTerminalApp.started = False
    return StubTerminalApp


@pytest.mark.parametrize(
    ("status", "expected"),
    [("completed", 0), ("failed", 1), ("cancelled", 1)],
)
def test_main_maps_one_shot_status_to_exit_code(tmp_path, stub_cli, status: str, expected: int) -> None:
    stub_cli.result = RunState(task="task", mode="agent", status=status)

    assert cli.main(["--workspace", str(tmp_path), "--planner", "rule", "task"]) == expected


def test_main_resumes_idle_session_before_running_positional_task(tmp_path, monkeypatch) -> None:
    opened: list[str | None] = []
    conversation = SimpleNamespace(
        prepare_resume=lambda _session_id: SimpleNamespace(requires_action=False),
    )
    application = SimpleNamespace(open_conversation=lambda session_id: opened.append(session_id) or conversation)
    monkeypatch.setattr(cli, "build_application", lambda *args: application)
    monkeypatch.setattr(cli, "TerminalApp", StubTerminalApp)
    StubTerminalApp.result = RunState(task="next", mode="agent", status="completed")

    result = cli.main(["--workspace", str(tmp_path), "--planner", "rule", "--resume", "session_1", "next"])

    assert result == 0
    assert opened == ["session_1"]


def test_main_refuses_positional_task_until_resumable_workflow_is_handled(tmp_path, monkeypatch) -> None:
    conversation = SimpleNamespace(
        prepare_resume=lambda _session_id: SimpleNamespace(requires_action=True),
    )
    application = SimpleNamespace(open_conversation=lambda _session_id: conversation)
    monkeypatch.setattr(cli, "build_application", lambda *args: application)
    monkeypatch.setattr(cli, "TerminalApp", StubTerminalApp)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--workspace", str(tmp_path), "--planner", "rule", "--resume", "session_1", "next"])

    assert exc_info.value.code == 2


def test_main_rejects_removed_session_id_option() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--session-id", "session_1"])

    assert exc_info.value.code == 2


def test_view_routes_conversations_system_output_and_history() -> None:
    class TranscriptView:
        def __init__(self) -> None:
            self.conversations: list[str] = []
            self.queued: list[str] = []
            self.queue_cleared = False
            self.system: list[tuple[str, str]] = []
            self.histories: list[tuple[str, list[dict[str, str]]]] = []

        def begin_conversation(self, content: str) -> None:
            self.conversations.append(content)

        def queue_message(self, content: str) -> None:
            self.queued.append(content)

        def clear_queued_messages(self) -> None:
            self.queue_cleared = True

        def write_system(self, text: str, end: str = "\n") -> None:
            self.system.append((text, end))

        def show_history(self, label: str, messages: list[dict[str, str]]) -> None:
            self.histories.append((label, messages))

    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    view = TranscriptView()
    app._view = view
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    app._conversation_service.session_store = object()
    app._conversation_service.active_session = SimpleNamespace(session_id="session_1", title="Session 1")
    app._conversation_service.history = lambda: history

    app._write_user_message("hello")
    app._write_queued_message("later")
    app._clear_queued_messages()
    app._write("SYSTEM EVENT", end="")
    app._show_history()

    assert view.conversations == ["hello"]
    assert view.queued == ["later"]
    assert view.queue_cleared is True
    assert view.system == [("SYSTEM EVENT", "")]
    assert view.histories == [("session_1 — Session 1", history)]


def test_display_command_updates_view_and_status() -> None:
    class DisplayView:
        def __init__(self) -> None:
            self.levels: list[str] = []
            self.system: list[tuple[str, str]] = []
            self.reviews: list[dict[str, object]] = []
            self.ui_states: list[tuple[str, bool]] = []

        def set_detail_level(self, detail_level: str) -> None:
            self.levels.append(detail_level)

        def write_system(self, text: str, end: str = "\n") -> None:
            self.system.append((text, end))

        def begin_review(self, *args, **kwargs) -> None:
            self.reviews.append({"args": args, "kwargs": kwargs})

        def set_ui(self, *, status: str, interrupt_enabled: bool = False) -> None:
            self.ui_states.append((status, interrupt_enabled))

    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    view = DisplayView()
    app._view = view

    assert cli.TerminalApp._split_input("/display") == [("command", "display", "")]
    assert app._handle_command("display", "") is True
    assert len(view.reviews) == 1
    review = view.reviews[0]
    assert review["args"][:3] == (
        "DISPLAY MODE",
        "Current: Medium",
        "Choose how much detail future agent runs show.",
    )
    assert review["kwargs"] == {"initial_choice_id": "medium"}
    callback = review["args"][4]
    callback("minimal", None)
    assert app._display_mode == "minimal"
    assert view.levels == ["minimal"]
    assert view.ui_states[-1] == ("AGENT | IDLE | DISPLAY: MINIMAL | PERMISSION: APPROVAL FOR ME", False)

    assert cli.TerminalApp._split_input("/display verbose") == [("command", "display", "verbose")]
    assert app._handle_command("display", "verbose") is True
    assert app._display_mode == "verbose"
    assert view.levels == ["minimal", "verbose"]
    assert view.ui_states[-1] == ("AGENT | IDLE | DISPLAY: VERBOSE | PERMISSION: APPROVAL FOR ME", False)
    assert view.system[-1] == ("Display mode set to verbose.", "\n")
    assert "DISPLAY: VERBOSE" in app._status_with_permission("AGENT | IDLE")

    assert app._handle_command("display", "unknown") is True
    assert app._display_mode == "verbose"
    assert view.system[-1] == ("Usage: /display <minimal|medium|verbose>", "\n")
