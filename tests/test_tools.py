from pathlib import Path
from subprocess import CompletedProcess

import pytest

from mini_agent.tools import ConfirmationRequired, ToolError, ToolRegistry, WorkspaceCommand, calculate


def test_calculator_allows_arithmetic_only() -> None:
    assert calculate("(18 + 6) * 4") == "96"
    with pytest.raises(ToolError):
        calculate("__import__('os').system('echo unsafe')")


def test_file_tool_cannot_escape_workspace(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    with pytest.raises(ToolError):
        tools.invoke("read_file", {"path": "../outside.txt"})


def test_write_requires_confirmation(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    with pytest.raises(ConfirmationRequired):
        tools.invoke("write_file", {"path": "note.txt", "content": "hi"})


def test_command_tool_uses_powershell_on_windows_and_workspace_cwd(tmp_path: Path) -> None:
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return CompletedProcess(args, 0, stdout="created\n", stderr="")

    output = WorkspaceCommand(tmp_path, is_windows=True, runner=runner).run("New-Item -ItemType Directory demo")

    assert output == "stdout:\ncreated\n"
    assert calls == [
        (
            ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "New-Item -ItemType Directory demo"],
            {
                "cwd": tmp_path.resolve(),
                "capture_output": True,
                "check": False,
                "encoding": "utf-8",
                "errors": "replace",
                "text": True,
                "timeout": 30,
            },
        )
    ]


def test_command_tool_uses_bash_on_unix_and_reports_command_failures(tmp_path: Path) -> None:
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return CompletedProcess(args, 7, stdout="", stderr="bad command")

    command = WorkspaceCommand(tmp_path, is_windows=False, runner=runner)
    with pytest.raises(ToolError, match="code 7") as exc_info:
        command.run("mkdir demo")
    assert "stderr:\nbad command" in str(exc_info.value)
    assert calls[0][0] == ["bash", "-c", "mkdir demo"]


def test_command_tool_requires_confirmation_and_validates_timeout(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    with pytest.raises(ConfirmationRequired):
        tools.invoke("run_command", {"command": "mkdir demo"})
    with pytest.raises(ToolError, match="between 1 and 120"):
        WorkspaceCommand(tmp_path, is_windows=False).run("mkdir demo", timeout_seconds=0)
