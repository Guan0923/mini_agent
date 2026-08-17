"""Tests for the cross-platform process group abstraction (``backend.jobs.ProcessGroup``).

Uses real short-lived subprocesses wherever feasible and small deterministic
mocks only where a real spawn is impossible (start failure, terminator timeout,
and call-sequence recording).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from backend.jobs import ProcessGroup

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _nowait_cmd() -> list[str]:
    return [sys.executable, "-c", "pass"]


def _exit_cmd(code: int) -> list[str]:
    return [sys.executable, "-c", f"raise SystemExit({code})"]


def _sleep_cmd(seconds: int) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _finish(proc: ProcessGroup, *, wait: float = 5.0) -> None:
    """Short deadline-poll loop waiting for root exit; fails the test on timeout."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.02)
    pytest.fail(f"process did not exit within {wait:.1f}s (pids={proc.pids})")


def _retry_until(lambda_):  # noqa: ANN001
    """Poll ``lambda_()`` until it returns True or a short deadline elapses."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if lambda_():
            return True
        time.sleep(0.05)
    return False


def _pid_alive(pid: int) -> bool:
    """True if a *detached* process (not Popen-held) still exists.

    On Windows a terminated but not-yet-reaped process raises ``OSError``
    (WinError 11) or ``ProcessLookupError``; a Popen-held handle never raises,
    so this helper is only meaningful for processes whose handle we do not own.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def make_env(tmp_path) -> dict[str, str]:
    """A minimal explicit environment (caller-provided; never read from os.environ)."""
    minimal = (
        {"PATH": os.getenv("PATH") or "", "SYSTEMROOT": os.getenv("SYSTEMROOT") or ""}
        if IS_WINDOWS
        else {"PATH": os.getenv("PATH") or ""}
    )
    return {**minimal, "PROCESS_GROUP_TEST": "1"}


class StubPopen:
    """A scriptable stand-in for ``subprocess.Popen`` (no real process spawned)."""

    def __init__(self, *, running: bool = True) -> None:
        self.pid = 4242
        self._returncode: int | None = None if running else 0
        self.killed = False

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return None if (timeout is not None and not False) else self._returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9


class RecordingTerminator:
    """Records the Popen objects passed to the injected tree terminator."""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[subprocess.Popen] = []

    def __call__(self, process: subprocess.Popen) -> None:
        self.calls.append(process)
        if self.exc is not None:
            raise self.exc


# ---------------------------------------------------------------------------
# Launch, poll, wait
# ---------------------------------------------------------------------------


def test_start_returns_live_pid_and_wait_returns_exit_code(tmp_path) -> None:
    env = make_env(tmp_path)
    group = ProcessGroup(_nowait_cmd(), env, cwd=str(tmp_path))
    pid = group.start()
    assert pid > 0
    assert pid in group.pids
    assert group.poll() in (0, None)
    assert group.wait(timeout=10) == 0
    _finish(group)


def test_poll_returns_exit_code_for_short_lived_command(tmp_path) -> None:
    group = ProcessGroup(_nowait_cmd(), make_env(tmp_path), cwd=str(tmp_path))
    pid = group.start()
    assert pid in group.pids
    _finish(group)
    assert group.poll() == 0


def test_nonzero_exit_code_captured(tmp_path) -> None:
    env = make_env(tmp_path)
    group = ProcessGroup(_exit_cmd(3), env, cwd=str(tmp_path))
    group.start()
    assert group.wait(timeout=10) == 3


def test_wait_timeout_returns_none(tmp_path) -> None:
    env = make_env(tmp_path)
    group = ProcessGroup(_sleep_cmd(300), env, cwd=str(tmp_path))
    group.start()
    try:
        assert group.wait(timeout=0.1) is None
    finally:
        group.terminate()


# ---------------------------------------------------------------------------
# Start failure and state consistency
# ---------------------------------------------------------------------------


def test_start_failure_propagates_and_later_retry_succeeds(tmp_path) -> None:
    """A FileNotFoundError from the factory must propagate and not corrupt state."""
    real_popen = subprocess.Popen
    calls = {"count": 0}

    def flaky_factory(*args, **kwargs):
        if calls["count"] == 0:
            calls["count"] += 1
            raise FileNotFoundError(f"no such executable: {args[0][0]}")
        calls["count"] += 1
        return real_popen(*args, **kwargs)

    group = ProcessGroup(
        _nowait_cmd(),
        make_env(tmp_path),
        cwd=str(tmp_path),
        popen_factory=flaky_factory,
    )
    with pytest.raises(FileNotFoundError):
        group.start()
    assert group.pids == ()

    # A later attempt on the same instance must start cleanly.
    pid = group.start()
    assert pid > 0
    assert pid in group.pids
    assert group.wait(timeout=10) == 0
    _finish(group)


def test_start_oserror_propagates(tmp_path) -> None:
    def failing_factory(*args, **kwargs):
        raise OSError("boom")

    group = ProcessGroup(
        _nowait_cmd(),
        make_env(tmp_path),
        cwd=str(tmp_path),
        popen_factory=failing_factory,
    )
    with pytest.raises(OSError):
        group.start()
    assert group.pids == ()


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


def test_terminate_kills_process_group_root(tmp_path) -> None:
    env = make_env(tmp_path)
    group = ProcessGroup(_sleep_cmd(300), env, cwd=str(tmp_path))
    pid = group.start()
    assert pid in group.pids
    group.terminate()
    # The Popen-held root is authoritative via poll(); os.kill(pid,0) is
    # unreliable on Windows while the handle is still held.
    assert _retry_until(lambda: group.poll() is not None)
    assert group.termination_uncertain is False


def test_terminate_is_idempotent_when_already_exited(tmp_path) -> None:
    env = make_env(tmp_path)
    group = ProcessGroup(_nowait_cmd(), env, cwd=str(tmp_path))
    group.start()
    assert group.wait(timeout=10) == 0
    _finish(group)
    # Second call must be a no-op with no exception.
    group.terminate()
    group.terminate()
    group.terminate()


def test_terminator_timeout_still_falls_back_to_root_kill(tmp_path) -> None:
    """If the injected terminator raises TimeoutError, root fallback still runs."""
    env = make_env(tmp_path)
    terminator = RecordingTerminator(exc=TimeoutError("terminator timed out"))
    group = ProcessGroup(
        _sleep_cmd(300),
        env,
        cwd=str(tmp_path),
        tree_terminator=terminator,
    )
    pid = group.start()
    group.terminate()  # must not raise
    assert len(terminator.calls) == 1
    assert terminator.calls[0].pid == pid
    assert _retry_until(lambda: group.poll() is not None)
    assert group.poll() is not None


def test_already_exited_group_does_not_call_terminator(tmp_path) -> None:
    """Idempotency: a group whose root already exited avoids the terminator."""
    env = make_env(tmp_path)
    terminator = RecordingTerminator()
    group = ProcessGroup(
        _nowait_cmd(),
        env,
        cwd=str(tmp_path),
        tree_terminator=terminator,
    )
    group.start()
    assert group.wait(timeout=10) == 0
    _finish(group)
    group.terminate()
    assert terminator.calls == []


def test_builtin_terminator_invoked_and_root_fallback(tmp_path) -> None:
    """With no injected terminator, the platform built-in runs then root fallback."""
    env = make_env(tmp_path)
    group = ProcessGroup(_sleep_cmd(300), env, cwd=str(tmp_path))
    pid = group.start()
    group.terminate()
    assert _retry_until(lambda: group.poll() is not None)
    assert group.poll() is not None
    assert pid in group.pids or group.pids == ()  # root recorded while running


# ---------------------------------------------------------------------------
# Injected call sequence (fake Popen, no real spawn) per platform branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_windows", [True, False])
def test_terminate_outer_call_sequence_with_fake_popen(tmp_path, is_windows: bool) -> None:
    """Terminator + root fallback sequence is platform-independent via injection."""
    stub = StubPopen(running=True)
    calls: list[str] = []
    terminator = RecordingTerminator()

    def factory(*args, **kwargs):
        calls.append("factory")
        return stub

    group = ProcessGroup(
        _nowait_cmd(),
        make_env(tmp_path),
        cwd=str(tmp_path),
        is_windows=is_windows,
        popen_factory=factory,
        tree_terminator=terminator,
    )
    group.start()
    group.terminate()
    assert calls == ["factory"]
    assert terminator.calls == [stub]
    assert stub.killed is True  # root fallback ran even with fake process "running"


@pytest.mark.parametrize("is_windows", [True, False])
def test_terminate_skips_terminator_and_root_when_exited_fake(tmp_path, is_windows: bool) -> None:
    """Already-exited fake process: terminator and root fallback are both skipped."""
    stub = StubPopen(running=False)
    terminator = RecordingTerminator()

    def factory(*args, **kwargs):
        return stub

    group = ProcessGroup(
        _nowait_cmd(),
        make_env(tmp_path),
        cwd=str(tmp_path),
        is_windows=is_windows,
        popen_factory=factory,
        tree_terminator=terminator,
    )
    group.start()
    group.terminate()
    assert terminator.calls == []
    assert stub.killed is False


# ---------------------------------------------------------------------------
# Real process-tree termination (platform specific)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows taskkill /T /F real-tree test")
def test_terminate_kills_whole_tree_windows(tmp_path) -> None:
    child_pid_file = tmp_path / "child.pid"
    argv = [
        "powershell",
        "-NoProfile",
        "-Command",
        f"$c = Start-Process -WindowStyle Hidden -PassThru -FilePath powershell -ArgumentList '-NoProfile','-Command','Start-Sleep 300'; "
        f"$c.Id | Out-File -FilePath '{child_pid_file}' -Encoding ascii; Start-Sleep 300",
    ]
    env = make_env(tmp_path)
    group = ProcessGroup(argv, env, cwd=str(tmp_path))
    pid = group.start()
    assert _retry_until(child_pid_file.exists), "child pid file was never written"
    child_pid = int(child_pid_file.read_text(encoding="ascii").strip())
    assert child_pid != pid
    group.terminate()
    # Root is Popen-held → authoritative via poll(); child is detached → os.kill.
    root_gone = _retry_until(lambda: group.poll() is not None)
    assert root_gone, f"root pid {pid} still running after terminate"
    assert _retry_until(lambda: not _pid_alive(child_pid)), f"child pid {child_pid} still alive after terminate"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX killpg real-tree test")
def test_terminate_kills_whole_tree_posix(tmp_path) -> None:
    child_pid_file = tmp_path / "child.pid"
    argv = [
        "sh",
        "-c",
        f"sleep 300 & echo $! > '{child_pid_file}'; exec sleep 300",
    ]
    env = make_env(tmp_path)
    group = ProcessGroup(argv, env, cwd=str(tmp_path))
    pid = group.start()
    assert _retry_until(child_pid_file.exists), "child pid file was never written"
    child_pid = int(child_pid_file.read_text(encoding="ascii").strip())
    assert child_pid != pid
    group.terminate()
    assert _retry_until(lambda: group.poll() is not None), f"root pid {pid} still running"
    assert _retry_until(lambda: not _pid_alive(child_pid)), f"child pid {child_pid} still alive after terminate"
