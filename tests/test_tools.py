import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend.tools import ToolError, ToolRegistry, WorkspaceCommand


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        times_out: bool = False,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.times_out = times_out
        self.pid = 1234
        self.communicate_calls: list[int | None] = []
        self.killed = False

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        self.communicate_calls.append(timeout)
        if self.times_out and timeout is not None:
            raise subprocess.TimeoutExpired(["shell"], timeout)
        return self.stdout, self.stderr

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_command_tool_uses_powershell_on_windows_and_workspace_cwd(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    process = FakeProcess(stdout="created\n")

    def popen_factory(args: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return process

    output = WorkspaceCommand(
        tmp_path,
        is_windows=True,
        popen_factory=popen_factory,
        environment={"PATH": "C:\\Windows\\System32", "API_KEY": "secret"},
    ).run("New-Item -ItemType Directory demo")

    assert output == "stdout:\ncreated\n"
    assert calls[0][0] == [
        "powershell",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "New-Item -ItemType Directory demo",
    ]
    options = calls[0][1]
    assert options["cwd"] == tmp_path.resolve()
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == subprocess.PIPE
    assert options["stderr"] == subprocess.PIPE
    assert options["env"] == {"PATH": "C:\\Windows\\System32"}
    assert options["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert "start_new_session" not in options
    assert process.communicate_calls == [30]


def test_command_tool_uses_bash_on_unix_and_reports_command_failures(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    process = FakeProcess(stderr="bad command", returncode=7)

    def popen_factory(args: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return process

    command = WorkspaceCommand(tmp_path, is_windows=False, popen_factory=popen_factory)
    with pytest.raises(ToolError, match="code 7") as exc_info:
        command.run("mkdir demo")

    assert "stderr:\nbad command" in str(exc_info.value)
    assert calls[0][0] == ["bash", "-c", "mkdir demo"]
    assert calls[0][1]["start_new_session"] is True
    assert "creationflags" not in calls[0][1]


@pytest.mark.parametrize("is_windows", [False, True])
def test_command_timeout_terminates_the_process_tree(tmp_path: Path, is_windows: bool) -> None:
    process = FakeProcess(stdout="partial", times_out=True)
    terminated: list[int] = []

    def terminate(candidate: FakeProcess) -> None:
        terminated.append(candidate.pid)
        candidate.returncode = -9

    command = WorkspaceCommand(
        tmp_path,
        is_windows=is_windows,
        popen_factory=lambda _args, **_kwargs: process,
        tree_terminator=terminate,
    )

    with pytest.raises(ToolError, match="timed out") as exc_info:
        command.run("slow", timeout_seconds=2)

    assert "stdout:\npartial" in str(exc_info.value)
    assert terminated == [1234]
    assert process.communicate_calls == [2, None]


def test_command_output_uses_one_shared_limit_and_preserves_both_streams(tmp_path: Path) -> None:
    process = FakeProcess(stdout="x" * 30_000, stderr="y" * 30_000)
    command = WorkspaceCommand(
        tmp_path,
        popen_factory=lambda _args, **_kwargs: process,
        environment={},
    )

    output = command.run("large output")

    assert len(output) <= 20_000
    assert output.startswith("stdout:\n")
    assert "\nstderr:\n" in output
    assert "output truncated" in output


def test_command_tool_requires_confirmation_and_validates_timeout(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    tools.invoke("run_command", {"command": "mkdir demo"}, confirmed=True)
    with pytest.raises(ToolError, match="between 1 and 120"):
        WorkspaceCommand(tmp_path, is_windows=False).run("mkdir demo", timeout_seconds=0)
