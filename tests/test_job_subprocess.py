"""Tests for the one-shot subprocess job carrier (``backend.jobs.SubprocessJob``).

Uses real short-lived ``sys.executable`` children for lifecycle behaviour, a
cross-platform grandchild-spawning child for tree-termination, and a fake
``popen_factory`` for start failures. Output formatting is compared against
the private ``WorkspaceCommand._format_output`` to prove algorithm parity.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.jobs import (
    MessageErrorFormatter,
    SubprocessJob,
    format_command_output,
)
from backend.jobs.base import JobKind, JobState
from backend.tools.command import WorkspaceCommand

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nowait_cmd() -> list[str]:
    return [sys.executable, "-c", "pass"]


def _print_cmd(text: str) -> list[str]:
    return [sys.executable, "-c", f"print({text!r}, flush=True)"]


def _exit_cmd(code: int) -> list[str]:
    import_code = (
        f"import sys;print('oops', file=sys.stderr, flush=True);print('boom', flush=True);raise SystemExit({code})"
    )
    return [sys.executable, "-c", import_code]


def _sleep_cmd(seconds: int) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _sleeper_with_grandchild() -> list[str]:
    code = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']);"
        "print(p.pid, flush=True);"
        "time.sleep(300)"
    )
    return [sys.executable, "-c", code]


def make_env(tmp_path) -> dict[str, str]:  # noqa: ANN001
    """A minimal explicit environment (caller-provided; never read from os.environ)."""
    minimal = (
        {"PATH": os.getenv("PATH") or "", "SYSTEMROOT": os.getenv("SYSTEMROOT") or ""}
        if IS_WINDOWS
        else {"PATH": os.getenv("PATH") or ""}
    )
    return {**minimal, "MINI_AGENT_JOB_TEST": "1"}


def make_job(
    tmp_path,
    job_id: str = "job-sub-1",
    argv: list[str] | None = None,
    *,
    timeout_seconds: float = 30.0,
    error_formatter=None,
    max_output_chars: int = 20_000,
    popen_factory=None,
    **kwargs: object,
):  # noqa: ANN001
    return SubprocessJob(
        job_id,
        argv if argv is not None else _nowait_cmd(),
        make_env(tmp_path),
        str(tmp_path),
        timeout_seconds,
        error_formatter=error_formatter,
        max_output_chars=max_output_chars,
        popen_factory=popen_factory or subprocess.Popen,
        **kwargs,
    )


def wait_until(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached in time")


def _pid_alive(pid: int) -> bool:
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return False
        except PermissionError:
            return True
        return True
    output = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return str(pid) in output


# ---------------------------------------------------------------------------
# Start failure
# ---------------------------------------------------------------------------


def test_start_failure_marks_failed_with_formatted_error(tmp_path) -> None:
    def failing_factory(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise FileNotFoundError(f"no such executable: {args[0][0]}")

    job = make_job(tmp_path, popen_factory=failing_factory)
    with pytest.raises(FileNotFoundError):
        job.start()
    info = job.info()
    assert info.state is JobState.FAILED
    # Default ClassNameErrorFormatter: only the class name, never the message.
    assert info.error == "FileNotFoundError"
    assert info.kind is JobKind.SUBPROCESS


# ---------------------------------------------------------------------------
# Normal / non-zero exit
# ---------------------------------------------------------------------------


def test_exit_zero_succeeds_with_captured_stdout(tmp_path) -> None:
    job = make_job(tmp_path, argv=_print_cmd("hello stdout"))
    job.start()
    wait_until(lambda: job.info().state is JobState.SUCCEEDED)
    info = job.info()
    assert info.exit_code == 0
    assert "stdout:\nhello stdout" in job.output


def test_nonzero_exit_fails_with_exit_code_and_compatible_message(tmp_path) -> None:
    job = make_job(tmp_path, argv=_exit_cmd(4), error_formatter=MessageErrorFormatter())
    job.start()
    wait_until(lambda: job.info().state is JobState.FAILED)
    info = job.info()
    assert info.exit_code == 4
    assert info.error == "Command exited with code 4."
    assert "stderr:\noops" in job.output
    assert "stdout:\nboom" in job.output


# ---------------------------------------------------------------------------
# Timeout: real short timeout, whole tree terminated
# ---------------------------------------------------------------------------


def test_timeout_terminates_tree_and_marks_failed(tmp_path) -> None:
    job = make_job(
        tmp_path,
        argv=_sleeper_with_grandchild(),
        timeout_seconds=0.5,
        error_formatter=MessageErrorFormatter(),
    )
    job.start()
    wait_until(lambda: job.info().state is JobState.FAILED)
    info = job.info()
    assert info.error == "Command timed out after 0.5 seconds."
    # The grandchild pid was printed to stdout and must now be gone.
    grandchild = int(job.output.split("stdout:\n", 1)[1].strip().splitlines()[0])
    wait_until(lambda: not _pid_alive(grandchild), timeout=5.0)


def test_timeout_default_formatter_only_reports_class_name(tmp_path) -> None:
    job = make_job(tmp_path, argv=_sleep_cmd(300), timeout_seconds=0.3)
    job.start()
    wait_until(lambda: job.info().state is JobState.FAILED)
    assert job.info().error == "CommandError"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancel_terminates_tree_and_marks_cancelled(tmp_path) -> None:
    job = make_job(tmp_path, argv=_sleep_cmd(300), timeout_seconds=60)
    job.start()
    wait_until(lambda: job._group.pids != ())
    pid = job._group.pids[0]
    assert job.cancel() is True
    wait_until(lambda: job.info().state is JobState.CANCELLED)
    assert job.info().cancel_requested_at is not None
    wait_until(lambda: not _pid_alive(pid), timeout=5.0)


# ---------------------------------------------------------------------------
# Lifecycle idempotency / thread hygiene
# ---------------------------------------------------------------------------


def test_close_is_idempotent_and_safe(tmp_path) -> None:
    job = make_job(tmp_path, argv=_print_cmd("done"))
    job.start()
    wait_until(lambda: job.info().state is JobState.SUCCEEDED)
    job.close(timeout=5)
    job.close(timeout=5)
    assert job.info().state is JobState.SUCCEEDED


def test_close_after_terminal_is_a_noop(tmp_path) -> None:
    job = make_job(tmp_path, argv=_sleep_cmd(300))
    job.start()
    info = job.info()
    assert info.state is JobState.RUNNING
    job.close(timeout=5)
    assert job.cancel() is False
    assert job.info().state is JobState.CANCELLED


def test_monitor_thread_exits_after_terminal(tmp_path) -> None:
    job = make_job(tmp_path, argv=_print_cmd("done"))
    job.start()
    while job.info().state is JobState.RUNNING and job._monitor_thread is not None:
        time.sleep(0.01)
    monitor = job._monitor_thread
    assert monitor is not None
    monitor.join(timeout=5)
    assert not monitor.is_alive()


def test_repeated_start_rejected(tmp_path) -> None:
    job = make_job(tmp_path)
    job.start()
    with pytest.raises(Exception):
        job.start()
    assert job.info().state in (JobState.SUCCEEDED, JobState.RUNNING)


# ---------------------------------------------------------------------------
# Output truncation and parity with tools.command
# ---------------------------------------------------------------------------


def test_output_truncated_at_budget_with_omitted_marker(tmp_path) -> None:
    job = make_job(
        tmp_path,
        argv=_print_cmd("x" * 30_000),
        max_output_chars=20_000,
    )
    job.start()
    wait_until(lambda: job.info().state is JobState.SUCCEEDED)
    assert len(job.output) <= 20_000
    assert "… output truncated" in job.output


def test_output_algorithm_matches_tools_command() -> None:
    wc = WorkspaceCommand(Path.cwd())
    samples = [
        (b"hello\n", b""),
        (b"", b"error text"),
        (b"a" * 30_000, b"b" * 30_000),
        (b"short", b"x" * 50_000),
        (None, b"only stderr"),
        (b"stdout only", None),
        (b"", b""),
    ]
    for stdout, stderr in samples:
        assert format_command_output(stdout, stderr) == wc._format_output(stdout, stderr)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


def test_registry_submit_releases_slot_on_terminal(tmp_path) -> None:
    from backend.jobs import AdmissionPolicy, JobLane, JobRegistry

    registry = JobRegistry()
    scope = registry.root_scope()
    job = make_job(tmp_path, job_id=registry.new_job_id(), argv=_print_cmd("ok"))
    info = registry.submit(job, scope=scope, lane=JobLane.FOREGROUND, admission=AdmissionPolicy())
    assert info.info.state is JobState.RUNNING
    assert info.holds_slot
    wait_until(lambda: job.info().state is JobState.SUCCEEDED)
    snapshot = registry.get(job._id)
    assert snapshot is not None
    assert snapshot.info.state is JobState.SUCCEEDED
    assert not snapshot.holds_slot
    assert registry.active_count() == 0
