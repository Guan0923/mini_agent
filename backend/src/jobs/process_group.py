"""Cross-platform process group abstraction for neutral job carriers.

A :class:`ProcessGroup` owns a single launch, giving its caller a uniform way to
start, poll, wait for, terminate, and inspect a process *and* its descendant
tree without duplicating Windows/POSIX differences.

Everything ``subprocess.Popen`` needs — ``argv``, environment, working
directory — is passed explicitly to the constructor. This module never reads
``os.environ``, never stores or echoes command lines or environment values in
state, logs, or errors, and depends only on the standard library.

It is intentionally neutral: it must not be imported by the runtime, tools,
MCP, or storage layers. Later modules (``SubprocessJob`` and friends) start,
poll, wait, and terminate process trees through this abstraction instead of
touching ``subprocess.Popen`` directly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

#: Callable that launches a process given argv plus kwargs. Kept broad so an
#: injected factory can accept the same kwargs ``subprocess.Popen`` does.
ProcessFactory = Callable[..., subprocess.Popen[str]]
#: Callable that force-terminates a process and its entire descendant tree.
TreeTerminator = Callable[[subprocess.Popen[str]], None]

__all__ = ["ProcessGroup", "ProcessFactory", "TreeTerminator"]


class ProcessGroup:
    """One process plus its descendant tree, with neutral lifecycle control.

    Args:
        argv: Command line (argv style) to launch.
        env: Explicit environment for the child process. The caller supplies
            this; it is never read from ``os.environ`` and its contents are
            never stored, logged, or echoed.
        cwd: Working directory for the child process.
        is_windows: Force the platform branch (``None`` auto-detects via
            ``os.name == "nt"``).
        popen_factory: Factory used to launch the child; defaults to
            :func:`subprocess.Popen`.
        tree_terminator: Callable that force-terminates the whole tree. When
            ``None``, a built-in platform terminator is used (``taskkill`` on
            Windows, :func:`os.killpg` on POSIX).
        termination_timeout: Deadline in seconds for asserting the root process
            exits after a terminate (used for the Windows ``taskkill`` wait).
        stdout: Stream the child's standard output is wired to; defaults to
            :data:`subprocess.DEVNULL`. Pass :data:`subprocess.PIPE` to capture
            it and drain it via :meth:`communicate`.
        stderr: Stream the child's standard error is wired to; defaults to
            :data:`subprocess.DEVNULL`.

    Thread safety: an instance may be used concurrently by a cancel/close
    thread and a monitor thread; all public methods synchronize on an internal
    lock so terminate/poll/wait never corrupt each other.
    """

    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        cwd: str | os.PathLike[str],
        *,
        is_windows: bool | None = None,
        popen_factory: ProcessFactory = subprocess.Popen,
        tree_terminator: TreeTerminator | None = None,
        termination_timeout: float = 5.0,
        stdout: int | None = subprocess.DEVNULL,
        stderr: int | None = subprocess.DEVNULL,
    ) -> None:
        self._argv = list(argv)
        self._env = dict(env)
        self._cwd = os.fspath(cwd)
        self._is_windows = os.name == "nt" if is_windows is None else is_windows
        self._popen_factory = popen_factory
        self._tree_terminator = tree_terminator
        self._termination_timeout = termination_timeout
        self._stdout = stdout
        self._stderr = stderr

        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        # Set by terminate() when the root process could not be confirmed to
        # have exited within the termination timeout.
        self.termination_uncertain = False

    # -- public lifecycle ---------------------------------------------------

    def start(self) -> int:
        """Launch the child and return the root PID.

        The factory's :class:`FileNotFoundError` / :class:`OSError` propagates
        to the caller. Starting an already-running group returns its existing
        PID without spawning a second process, so a retry after a failed start
        leaves the instance in a consistent state.
        """
        process_options: dict[str, Any] = {
            "cwd": self._cwd,
            "env": self._env,
            "stdin": subprocess.DEVNULL,
            "stdout": self._stdout,
            "stderr": self._stderr,
        }
        if self._is_windows:
            process_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        else:
            process_options["start_new_session"] = True

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._process.pid
            process = self._popen_factory(self._argv, **process_options)
            self._process = process
            return process.pid

    def poll(self) -> int | None:
        """Return the root exit code, or ``None`` while it is still running."""
        with self._lock:
            process = self._process
        if process is None:
            return None
        return process.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for the root process and return its exit code.

        Returns ``None`` if ``timeout`` elapses before the process exits.
        """
        with self._lock:
            process = self._process
        if process is None:
            return None
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def communicate(
        self,
        timeout: float | None = None,
    ) -> tuple[bytes | None, bytes | None]:
        """Interact with the child and collect its captured ``stdout``/``stderr``.

        Drains the piped streams (reading them concurrently when both are
        pipes) and waits for the root process. When ``timeout`` elapses before
        the process exits, :class:`subprocess.TimeoutExpired` propagates from
        this method. Returns ``(stdout_bytes, stderr_bytes)``; bytes are
        ``None`` for streams that were not piped.
        """
        with self._lock:
            process = self._process
        if process is None:
            return None, None
        return process.communicate(timeout=timeout)

    def terminate(self) -> None:
        """Terminate the whole tree, then ensure the root process exits.

        Idempotent: calling it on an already-exited group (``poll()`` is not
        ``None``) is a no-op with no exception. After terminating, if the root
        could not be confirmed to exit within ``termination_timeout``, the
        ``termination_uncertain`` flag is set to True so callers know the kill
        was not acknowledged.
        """
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return  # nothing running → already-exited idempotent no-op

            self._terminate_tree(process)

            # Ensure the root process actually exits.
            if process.poll() is None:
                self._wait_for_exit(process)
            if process.poll() is None:
                process.kill()
            if process.poll() is None:
                self._wait_for_exit(process)
            if process.poll() is None:
                self.termination_uncertain = True

    def kill(self) -> None:
        """Immediately force-kill the root process.

        This mirrors ``Popen.kill``; it does not proactively walk the tree (the
        platform terminator used by :meth:`terminate` is the path that handles
        descendants). Missing/OS-level errors are swallowed so a concurrent
        termination or an already-exited process is harmless.
        """
        with self._lock:
            process = self._process
            if process is None:
                return
        try:
            process.kill()
        except OSError:
            pass

    @property
    def pids(self) -> tuple[int, ...]:
        """PIDs this group knows about — the root PID while it is running.

        Descendant PIDs are intentionally not tracked: the platform terminator
        walks the tree, so only the root PID is required for control.
        """
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return ()
        return (process.pid,)

    # -- internals ----------------------------------------------------------

    def _terminate_tree(self, process: subprocess.Popen[str]) -> None:
        """Best-effort tree kill; the caller always follows with a root fallback."""
        if self._tree_terminator is not None:
            try:
                self._tree_terminator(process)
            except Exception:  # noqa: BLE001 - terminator failures must not block root kill
                pass
            return
        self._builtin_terminate_tree(process)

    def _builtin_terminate_tree(self, process: subprocess.Popen[str]) -> None:
        if self._is_windows:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=self._termination_timeout,
                    env=self._env,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                killpg = getattr(os, "killpg")
                killpg(process.pid, signal.SIGKILL)
            except (AttributeError, OSError, ProcessLookupError):
                pass

    def _wait_for_exit(self, process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=self._termination_timeout)
        except subprocess.TimeoutExpired:
            pass
