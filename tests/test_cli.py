from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_agent.domain import RunState
from mini_agent.providers import ModelConfigurationError
from mini_agent.runtime import TaskPreparationError
from mini_agent.runtime.contracts import InterruptRequest
from mini_agent.tui import cli
from mini_agent.tui.approval import TerminalApproval


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
    app._conversation_service = StubConversation(result)
    app._event_sink = lambda event: None
    app._approval = object()
    return app


def test_run_task_returns_run_state() -> None:
    state = RunState(task="calculate", mode="agent", status="completed")
    app = build_terminal_app(state)

    assert app.run_task("calculate") is state
    assert app.last_state is state


def test_run_task_returns_none_when_preparation_fails(capsys) -> None:
    app = build_terminal_app(TaskPreparationError("missing.txt does not exist"))

    assert app.run_task("summarize @missing.txt") is None
    assert capsys.readouterr().out == "REFERENCE ERROR missing.txt does not exist\n"


def test_run_task_prints_and_activates_an_isolated_handoff_session(capsys) -> None:
    state = RunState(task="Implement the plan", mode="agent", status="completed")
    app = build_terminal_app(state)
    app.mode = "plan"
    app._conversation_service.active_session = SimpleNamespace(session_id="session_old", title="Plan")
    app._conversation_service.next_active_session = SimpleNamespace(
        session_id="session_new",
        title="Implement: Plan",
    )

    assert app.run_task("make a plan") is state

    assert app.mode == "agent"
    assert app.active_session.session_id == "session_new"
    assert capsys.readouterr().out == (
        "SESSION session_new — Implement: Plan\nAgent mode enabled after Plan Review implementation handoff.\n"
    )


def test_interactive_start_uses_full_screen_view_and_prints_resume_status(monkeypatch, capsys) -> None:
    commands: list[str] = []

    class ExitView:
        last = None

        def __init__(self, loop, **kwargs) -> None:
            del loop, kwargs
            type(self).last = self
            self.submissions = asyncio.Queue()
            self.submissions.put_nowait(None)
            self.stopped = asyncio.Event()
            self.writes: list[str] = []

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            self.writes.append(f"{text}{end}")

        def set_ui(self, **kwargs) -> None:
            del kwargs

        def clear(self) -> None:
            self.writes.clear()

    monkeypatch.setattr(cli.os, "system", lambda command: commands.append(command) or 0)
    monkeypatch.setattr(cli, "TerminalView", ExitView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))

    app.start()

    assert commands == []
    assert ExitView.last is not None
    assert ExitView.last.writes[0].startswith("Mini-Agent TUI")
    assert capsys.readouterr().out == "No saved session.\n"


def test_interactive_start_accepts_messages_while_run_is_active(monkeypatch, capsys) -> None:
    captured: list[str] = []

    class BlockingConversation(StubConversation):
        def run_task(self, task: str, **kwargs) -> RunState:
            assert task == "initial task"
            time.sleep(0.05)
            captured.extend(kwargs["steering"]())
            return RunState(task=task, mode="agent", status="completed")

    class InteractiveView:
        def __init__(self, loop, **kwargs) -> None:
            del loop, kwargs
            self.submissions = asyncio.Queue()
            self.stopped = asyncio.Event()
            self.initial_sent = False
            self.running_messages = 0
            self.quit_sent = False
            self.writes: list[str] = []

        async def run_async(self) -> None:
            await self.stopped.wait()

        def stop(self) -> None:
            self.stopped.set()

        def write(self, text: str, end: str = "\n") -> None:
            self.writes.append(f"{text}{end}")

        def clear(self) -> None:
            self.writes.clear()

        def set_ui(self, *, status: str, prompt: str) -> None:
            del prompt
            if status.endswith("IDLE") and not self.initial_sent:
                self.initial_sent = True
                self.submissions.put_nowait("initial task")
                return
            if status.endswith("RUNNING"):
                if self.running_messages == 0:
                    self.running_messages += 1
                    self.submissions.put_nowait("first steering")
                    return
                if self.running_messages == 1:
                    self.running_messages += 1
                    self.submissions.put_nowait("second steering")
                    return
            if status.endswith("IDLE") and self.running_messages == 2 and not self.quit_sent:
                self.quit_sent = True
                self.submissions.put_nowait("/quit")

    monkeypatch.setattr(cli.os, "system", lambda _command: 0)
    monkeypatch.setattr(cli, "TerminalView", InteractiveView)
    app = build_terminal_app(RunState(task="unused", mode="agent", status="completed"))
    app._conversation_service = BlockingConversation(RunState(task="unused", mode="agent"))
    app._approval = TerminalApproval()

    app.start()

    assert captured == ["first steering", "second steering"]
    output = capsys.readouterr().out
    assert output == "No saved session.\n"


def test_interactive_approval_bridge_resolves_on_prompt_loop(capsys) -> None:
    async def scenario():
        bridge = cli._InteractiveApproval(TerminalApproval(), asyncio.get_running_loop())
        request = InterruptRequest("tool", "Call tool?", {"tool": "write_file", "arguments": {}})
        decision_task = asyncio.create_task(asyncio.to_thread(bridge, request))
        await bridge.changed.wait()
        bridge.changed.clear()
        assert bridge.pending is True
        bridge.submit("1")
        return await decision_task

    decision = asyncio.run(scenario())

    assert decision.choice == "continue"
    assert "TOOL REVIEW" in capsys.readouterr().out


class StubTerminalApp:
    result: RunState | None = None
    started = False

    def __init__(self, conversation, log_dir: Path) -> None:
        self.conversation = conversation
        self.log_dir = log_dir

    def run_task(self, task: str) -> RunState | None:
        return self.result

    def start(self) -> None:
        type(self).started = True


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


def test_main_returns_failure_when_task_preparation_fails(tmp_path, stub_cli) -> None:
    stub_cli.result = None

    assert cli.main(["--workspace", str(tmp_path), "--planner", "rule", "task"]) == 1


def test_main_returns_success_after_interactive_session(tmp_path, stub_cli) -> None:
    assert cli.main(["--workspace", str(tmp_path), "--planner", "rule"]) == 0
    assert stub_cli.started is True


@pytest.mark.parametrize("workspace_kind", ["missing", "file"])
def test_main_rejects_non_directory_workspace(tmp_path, capsys, workspace_kind: str) -> None:
    workspace = tmp_path / workspace_kind
    if workspace_kind == "file":
        workspace.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--workspace", str(workspace), "--planner", "rule"])

    error = capsys.readouterr().err
    assert exc_info.value.code == 2
    assert "workspace" in error
    assert "--planner rule" not in error


def test_main_suggests_rule_planner_only_for_model_configuration(tmp_path, monkeypatch, capsys) -> None:
    def fail_to_build(*args):
        raise ModelConfigurationError("Missing API_KEY.")

    monkeypatch.setattr(cli, "build_application", fail_to_build)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--workspace", str(tmp_path)])

    assert exc_info.value.code == 2
    assert "Use --planner rule for offline mode." in capsys.readouterr().err


def test_main_does_not_suggest_rule_planner_for_invalid_runtime_settings(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--workspace", str(tmp_path), "--max-actions", "0"])

    assert exc_info.value.code == 2
    assert "--planner rule" not in capsys.readouterr().err
