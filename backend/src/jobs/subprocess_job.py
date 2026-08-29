"""One-shot subprocess job carrier built on :class:`ProcessGroup`.

A :class:`SubprocessJob` launches a single explicitly-specified command line
and drives it to a terminal state from a daemon monitor thread: exit ``0``
becomes ``succeeded``, a non-zero exit or a launch failure becomes ``failed``,
a timeout terminates the whole process tree and fails, and a requested
cancellation terminates the tree and is marked ``cancelled``.

It is intentionally neutral: it never reads ``os.environ``, never stores or
echoes command lines or environments, keeps captured output out of
``JobInfo.error``, and depends only on the standard library and the jobs
package.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Mapping, Sequence

from .base import Job, JobKind, JobStateError
from .output import CommandError, format_command_output
from .process_group import ProcessFactory, ProcessGroup, TreeTerminator

__all__ = ["SubprocessJob"]


class SubprocessJob(Job):
    """A single spawned command line driven to a terminal lifecycle state.

    Args:
        job_id: Stable identifier for the job.
        argv: Command line (argv style) to launch.
        env: Explicit environment for the child. Always supplied by the caller;
            never read from ``os.environ``.
        cwd: Working directory for the child.
        timeout_seconds: Deadline for the process to exit before the whole tree
            is terminated and the job is marked failed. ``None`` disables it.
        max_output_chars: Character budget for the truncated output snapshot.
        popen_factory, tree_terminator, is_windows, termination_timeout:
            Optional injectables forwarded to :class:`ProcessGroup`.
        error_formatter: ``ErrorFormatter`` for ``JobInfo.error``; defaults to
            :class:`~backend.jobs.ClassNameErrorFormatter`. Pass
            :class:`MessageErrorFormatter` to surface the WorkspaceCommand
            compatible result messages verbatim.
    """

    kind = JobKind.SUBPROCESS

    def __init__(
        self,
        job_id: str,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        timeout_seconds: float | None,
        *,
        max_output_chars: int = 20_000,
        popen_factory: ProcessFactory = subprocess.Popen,
        tree_terminator: TreeTerminator | None = None,
        is_windows: bool | None = None,
        termination_timeout: float = 5.0,
        error_formatter=None,
        clock=None,
        listener=None,
        sandbox_policy=None,
        sandbox_launcher=None,
        resource_monitor=None,
    ) -> None:
        super().__init__(
            job_id,
            self.kind,
            clock=clock,
            error_formatter=error_formatter,
            listener=listener,
        )
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars
        self._group = ProcessGroup(
            argv,
            env,
            cwd,
            is_windows=is_windows,
            popen_factory=popen_factory,
            tree_terminator=tree_terminator,
            termination_timeout=termination_timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._monitor_thread: threading.Thread | None = None
        self._output = ""
        self._stdout = ""
        self._stderr = ""
        self.sandbox_policy = sandbox_policy
        self.sandbox_launcher = sandbox_launcher
        self.resource_monitor = resource_monitor
        if sandbox_policy is not None:
            raw = sandbox_policy.to_dict()
            self._set_sandbox_info(
                {
                    "enforced": bool(raw.get("enforced", True)),
                    "file_mode": raw.get("file_mode", "read_only"),
                    "network_mode": raw.get("network_mode", "no_network"),
                    "limits": raw.get("limits", {}),
                    "failure_code": None,
                    "cleanup_pending": False,
                }
            )

    # -- public API ---------------------------------------------------------

    @property
    def output(self) -> str:
        """Truncated ``stdout``/``stderr`` snapshot rendered via
        :func:`format_command_output`; captured output never enters
        ``JobInfo.error``."""
        return self._output

    @property
    def stdout(self) -> str:
        """The decoded (untrucated) captured standard output."""
        return self._stdout

    @property
    def stderr(self) -> str:
        """The decoded (untrucated) captured standard error."""
        return self._stderr

    def start(self) -> None:
        """Launch the child and begin monitoring it.

        Calls ``super().start()`` first; a launch failure (the injected factory
        raising ``FileNotFoundError``/``OSError``) marks the job failed and is
        re-raised so the registry can propagate it.
        """
        super().start()
        try:
            pid = self._group.start()
        except OSError as exc:
            self._mark_sandbox_failure(getattr(exc, "code", "init_failed"))
            self._mark_failed(exc)
            raise
        except Exception as exc:
            self._mark_sandbox_failure(getattr(exc, "code", "init_failed"))
            self._mark_failed(exc)
            raise
        self._set_process_info((pid,))
        if self.resource_monitor is not None:
            self.resource_monitor.on_exceeded = self._resource_exceeded
            self.resource_monitor.start()
        self._monitor_thread = threading.Thread(
            target=self._monitor,
            name=f"job-{self._id}-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def close(self, timeout: float | None = None) -> None:
        """Cancel and wait, then join the monitor thread (idempotent)."""
        super().close(timeout)
        thread = self._monitor_thread
        if thread is not None:
            thread.join(timeout=5.0)

    def _request_cancel(self) -> None:
        """Stop the running subprocess by terminating the whole tree; the
        monitor thread observes the exit and seals the ``cancelled`` state."""
        self._group.terminate()

    def _mark_sandbox_failure(self, code: object) -> None:
        current = self.info().sandbox
        if current is None:
            return
        current.update({"failure_code": str(code)})
        self._set_sandbox_info(current)

    # -- monitor thread -----------------------------------------------------

    def _monitor(self) -> None:
        try:
            try:
                stdout, stderr = self._group.communicate(timeout=self._timeout_seconds)
            except subprocess.TimeoutExpired:
                self._group.terminate()
                stdout, stderr = self._group.communicate(timeout=30.0)
                exit_code = self._group.poll()
                self._capture(stdout, stderr)
                # If the user canceled around the timeout boundary, honor that
                # instead of reporting a timeout failure.
                if self.info().cancel_requested_at is not None:
                    self._mark_cancelled(exit_code=exit_code)
                else:
                    self._finish_timeout(exit_code)
                return

            exit_code = self._group.poll()
            self._capture(stdout, stderr)
            if self.info().cancel_requested_at is not None:
                self._mark_cancelled(exit_code=exit_code)
            elif exit_code == 0:
                self._mark_succeeded(exit_code=0)
            else:
                self._mark_failed(
                    CommandError(f"Command exited with code {exit_code}."),
                    exit_code=exit_code,
                )
        except OSError as exc:
            try:
                self._mark_failed(exc)
            except JobStateError:
                pass
        except JobStateError:
            # A concurrent cancel/close already sealed the terminal state.
            pass
        except Exception as exc:
            try:
                self._mark_sandbox_failure(getattr(exc, "code", "init_failed"))
                self._mark_failed(exc)
            except JobStateError:
                pass
        finally:
            if self.resource_monitor is not None:
                self.resource_monitor.stop()
            if self.sandbox_launcher is not None:
                process = getattr(self._group, "_process", None)
                if not self.sandbox_launcher.cleanup(process):
                    current = self.info().sandbox or {}
                    current.update({"cleanup_pending": True, "failure_code": "cleanup_pending"})
                    self._set_sandbox_info(current)

    def _resource_exceeded(self, error: Exception) -> None:
        self._mark_sandbox_failure("resource_exceeded")
        self._group.terminate()

    def _finish_timeout(self, exit_code: int | None) -> None:
        self._mark_failed(
            CommandError(f"Command timed out after {self._timeout_seconds} seconds."),
            exit_code=exit_code,
        )

    def _capture(self, stdout: bytes | None, stderr: bytes | None) -> None:
        self._stdout = _as_text(stdout) if stdout else ""
        self._stderr = _as_text(stderr) if stderr else ""
        self._output = format_command_output(stdout, stderr, max_chars=self._max_output_chars)


def _as_text(value: bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if value else ""
