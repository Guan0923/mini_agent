import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from backend.jobs import (
    AdmissionPolicy,
    JobKind,
    JobLane,
    JobLimitPolicy,
    JobRegistry,
    JobScopeKind,
    JobState,
    LaneLimits,
    ThreadJob,
)
from backend.tools import Tool, ToolError, ToolInvocationContext, ToolRegistry, WorkspaceCommand
from backend.tools.default_tools.command import command_tool


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        times_out: bool = False,
    ) -> None:
        self.stdout = stdout.encode()
        self.stderr = stderr.encode()
        self.returncode: int | None = None if times_out else returncode
        self.times_out = times_out
        self.pid = 1234
        self.communicate_calls: list[int | None] = []
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        self.communicate_calls.append(timeout)
        if self.times_out and len(self.communicate_calls) == 1 and timeout is not None:
            raise subprocess.TimeoutExpired(["shell"], timeout)
        return self.stdout, self.stderr

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


def test_command_tool_uses_powershell_on_windows_and_workspace_cwd(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    process = FakeProcess(stdout="created\n")

    def popen_factory(args: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return process

    output = WorkspaceCommand(
        tmp_path,
        is_windows=True,
        terminal_type="powershell",
        popen_factory=popen_factory,
        environment={"PATH": "C:\\Windows\\System32", "API_KEY": "secret"},
    ).run("New-Item -ItemType Directory demo")

    assert output == "stdout:\ncreated\n"
    assert calls[0][0] == [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "New-Item -ItemType Directory demo",
    ]
    options = calls[0][1]
    assert options["cwd"] == str(tmp_path.resolve())
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == subprocess.PIPE
    assert options["stderr"] == subprocess.PIPE
    assert options["env"] == {"PATH": "C:\\Windows\\System32"}
    assert options["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert "start_new_session" not in options
    assert process.communicate_calls == [30]


@pytest.mark.parametrize(
    ("terminal_type", "expected"),
    [
        ("cmd", ["cmd.exe", "/d", "/s", "/c", "echo hi"]),
        ("pwsh", ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "echo hi"]),
    ],
)
def test_command_tool_uses_selected_windows_terminal(tmp_path: Path, terminal_type: str, expected: list[str]) -> None:
    calls: list[list[str]] = []

    def popen_factory(args: list[str], **_kwargs: Any) -> FakeProcess:
        calls.append(args)
        return FakeProcess()

    WorkspaceCommand(
        tmp_path,
        is_windows=True,
        terminal_type=terminal_type,
        popen_factory=popen_factory,
        environment={"PATH": ""},
    ).run("echo hi")

    assert calls == [expected]


def test_command_tool_uses_git_bash_executable_when_selected(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr("backend.tools.command.terminal_executable", lambda *_args, **_kwargs: "git-bash.exe")

    def popen_factory(args: list[str], **_kwargs: Any) -> FakeProcess:
        calls.append(args)
        return FakeProcess()

    WorkspaceCommand(
        tmp_path,
        is_windows=True,
        terminal_type="git_bash",
        popen_factory=popen_factory,
    ).run("echo hi")

    assert calls == [["git-bash.exe", "-lc", "echo hi"]]


def test_command_tool_maps_workspace_for_wsl(tmp_path: Path, monkeypatch) -> None:
    from backend.tools.terminal import windows_workspace_to_wsl

    calls: list[list[str]] = []
    monkeypatch.setattr("backend.tools.command.terminal_executable", lambda *_args, **_kwargs: "wsl.exe")

    def popen_factory(args: list[str], **_kwargs: Any) -> FakeProcess:
        calls.append(args)
        return FakeProcess()

    WorkspaceCommand(
        tmp_path,
        is_windows=True,
        terminal_type="wsl",
        popen_factory=popen_factory,
    ).run("pwd")

    assert calls == [["wsl.exe", "--cd", windows_workspace_to_wsl(tmp_path.resolve()), "--", "sh", "-lc", "pwd"]]


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
    assert process.communicate_calls == [2, 30.0]


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


def test_command_job_reuses_parent_slot_and_is_visible_in_shared_registry(tmp_path: Path) -> None:
    limits = {lane: LaneLimits(max_running=1, max_queued=1) for lane in JobLane}
    registry = JobRegistry(policy=JobLimitPolicy(system=limits, user=limits, runner=limits))
    user_scope = registry.root_scope().child(
        JobScopeKind.USER,
        user_id="user-1",
        session_id="session-1",
    )
    runner_scope = user_scope.child(JobScopeKind.RUNNER)
    parent_job_id = registry.new_job_id()
    run_scope = runner_scope.child(
        JobScopeKind.RUN,
        run_id="run-1",
        parent_job_id=parent_job_id,
    )
    commands = WorkspaceCommand(
        tmp_path,
        is_windows=False,
        popen_factory=lambda _args, **_kwargs: FakeProcess(stdout="managed\n"),
        environment={},
    )
    tools = ToolRegistry([command_tool(commands)])
    result: dict[str, str] = {}

    def invoke_command() -> None:
        result["output"] = tools.invoke_with_context(
            "run_command",
            {"command": "echo managed"},
            ToolInvocationContext(
                session_id="session-1",
                job_scope=run_scope,
            ),
            confirmed=True,
        )

    parent_job = ThreadJob(parent_job_id, invoke_command)
    registry.submit(
        parent_job,
        scope=user_scope,
        lane=JobLane.FOREGROUND,
        admission=AdmissionPolicy(),
    )

    assert parent_job.wait(2.0)
    assert parent_job.info().state is JobState.SUCCEEDED
    assert result["output"] == "stdout:\nmanaged\n"
    command_records = [
        item
        for item in registry.list_for_user("user-1", session_id="session-1")
        if item.info.kind is JobKind.SUBPROCESS
    ]
    assert len(command_records) == 1
    assert command_records[0].parent_job_id == parent_job_id
    assert command_records[0].lane is JobLane.FOREGROUND
    assert command_records[0].info.state is JobState.SUCCEEDED
    assert command_records[0].slot_mode.value == "inherit"
    registry.close_all(timeout=2.0)


def test_command_runtime_cancellation_terminates_managed_process(tmp_path: Path) -> None:
    started = threading.Event()
    stopped = threading.Event()
    terminated: list[int] = []

    class BlockingProcess:
        pid = 4321
        returncode: int | None = None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            started.set()
            assert stopped.wait(2.0)
            return b"partial", b""

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int | None:
            stopped.wait(timeout)
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9
            stopped.set()

    process = BlockingProcess()

    def terminate(candidate: BlockingProcess) -> None:
        terminated.append(candidate.pid)
        candidate.returncode = -9
        stopped.set()

    command = WorkspaceCommand(
        tmp_path,
        is_windows=False,
        popen_factory=lambda _args, **_kwargs: process,
        tree_terminator=terminate,
        environment={},
    )

    with pytest.raises(ToolError, match="cancelled") as exc_info:
        command.run_with_context(
            ToolInvocationContext(cancel_requested=started.is_set),
            "slow command",
        )

    assert terminated == [4321]
    assert "stdout:\npartial" in str(exc_info.value)


def test_command_tool_requires_confirmation_and_validates_timeout(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    tools.invoke("run_command", {"command": "mkdir demo"}, confirmed=True)
    with pytest.raises(ToolError, match="between 1 and 120"):
        WorkspaceCommand(tmp_path, is_windows=False).run("mkdir demo", timeout_seconds=0)


def test_registry_wraps_unexpected_handler_exceptions_as_tool_error() -> None:
    def boom(**_arguments: Any) -> str:
        raise RuntimeError("boom")

    registry = ToolRegistry(
        [
            Tool(
                "boom_tool",
                "Always fails unexpectedly.",
                boom,
                {"type": "object", "properties": {}, "required": []},
            )
        ]
    )

    with pytest.raises(ToolError, match="RuntimeError: boom"):
        registry.invoke("boom_tool", {})


def test_upload_file_tool_reads_only_inside_uploads_root(tmp_path: Path) -> None:
    from backend.tools import WorkspaceFiles, build_tool_registry

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    (upload_root / "notes.md").write_text("line one\nline two\n", encoding="utf-8")
    registry = build_tool_registry(tmp_path, upload_files=WorkspaceFiles(upload_root))

    assert "read_upload_file" in registry.names()
    assert "read_upload_file" in registry.read_only_names()
    result = registry.invoke("read_upload_file", {"path": "notes.md"})
    assert "line one" in result
    assert "lines 1-2 of 2" in result

    with pytest.raises(ToolError, match="inside the workspace"):
        registry.invoke("read_upload_file", {"path": "../outside.txt"})


def test_build_application_registers_upload_tool_when_root_provided(tmp_path: Path) -> None:
    from backend.configuration import ClientPaths
    from backend.runtime import build_application

    paths = ClientPaths(tmp_path)
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    (upload_root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    application = build_application(tmp_path, paths=paths, planner_name="rule", upload_root=upload_root)
    try:
        runner = application.runner
        tool_names = runner.tools.names()
        assert "read_upload_file" in tool_names
        result = runner.tools.invoke("read_upload_file", {"path": "data.csv"})
        assert "1,2" in result
    finally:
        application.close()
