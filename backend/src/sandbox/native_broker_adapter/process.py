"""Restricted Windows child-process wrapper and pipe I/O."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Mapping
from typing import Any

from ..errors import SandboxInitializationError
from ..native_windows import WindowsJobObject
from ..native_windows.api import _modules


class _NativeWindowsProcess:
    def __init__(
        self,
        *,
        process_handle: Any,
        thread_handle: Any,
        pid: int,
        stdin_handle: Any,
        stdout_handle: Any,
        stderr_handle: Any,
        job: WindowsJobObject,
    ) -> None:
        self.process_handle = process_handle
        self.thread_handle = thread_handle
        self.pid = pid
        self.stdin_handle = stdin_handle
        self.stdout_handle = stdout_handle
        self.stderr_handle = stderr_handle
        self.job = job
        self.returncode: int | None = None
        self._stdin_closed = False
        self._lock = threading.RLock()
        self._modules = _modules()
        self.output_bytes = 0
        self._reader_threads: tuple[threading.Thread, ...] = ()
        self._output_chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}

    @classmethod
    def launch(
        cls,
        token: Any,
        argv: list[str],
        cwd: str,
        environment: Mapping[str, str],
        job: WindowsJobObject,
    ) -> _NativeWindowsProcess:
        modules = _modules()
        pipe = modules["pipe"]
        api = modules["api"]
        process = modules["process"]
        child_stdin, parent_stdin = pipe.CreatePipe(None, 0)
        parent_stdout, child_stdout = pipe.CreatePipe(None, 0)
        parent_stderr, child_stderr = pipe.CreatePipe(None, 0)
        for handle in (parent_stdin, parent_stdout, parent_stderr):
            api.SetHandleInformation(handle, modules["con"].HANDLE_FLAG_INHERIT, 0)
        startup = process.STARTUPINFO()
        startup.dwFlags |= modules["con"].STARTF_USESTDHANDLES
        startup.hStdInput = child_stdin
        startup.hStdOutput = child_stdout
        startup.hStdError = child_stderr
        flags = (
            modules["con"].CREATE_SUSPENDED
            | modules["con"].CREATE_NO_WINDOW
            | modules["con"].CREATE_UNICODE_ENVIRONMENT
        )
        try:
            process_handle, thread_handle, pid, _ = process.CreateProcessAsUser(
                token,
                None,
                subprocess.list2cmdline(argv),
                None,
                None,
                True,
                flags,
                dict(environment),
                cwd,
                startup,
            )
            job.assign(process_handle)
            process.ResumeThread(thread_handle)
        except Exception as exc:  # pragma: no cover - requires UAC
            job.terminate()
            job.close()
            raise SandboxInitializationError("Broker could not launch the restricted process") from exc
        finally:
            for handle in (child_stdin, child_stdout, child_stderr):
                try:
                    api.CloseHandle(handle)
                except Exception:
                    pass
        return cls(
            process_handle=process_handle,
            thread_handle=thread_handle,
            pid=int(pid),
            stdin_handle=parent_stdin,
            stdout_handle=parent_stdout,
            stderr_handle=parent_stderr,
            job=job,
        )

    def poll(self) -> int | None:
        with self._lock:
            if self.returncode is not None:
                return self.returncode
            result = self._modules["event"].WaitForSingleObject(self.process_handle, 0)
            if result == self._modules["con"].WAIT_TIMEOUT:
                return None
            self.returncode = int(self._modules["process"].GetExitCodeProcess(self.process_handle))
            return self.returncode

    def wait(self, timeout: float | None) -> int | None:
        milliseconds = self._modules["event"].INFINITE if timeout is None else max(0, int(timeout * 1000))
        result = self._modules["event"].WaitForSingleObject(self.process_handle, milliseconds)
        if result == self._modules["con"].WAIT_TIMEOUT:
            return None
        return self.poll()

    def read(self, stream: str, size: int) -> bytes:
        handle = self.stdout_handle if stream == "stdout" else self.stderr_handle
        try:
            _, value = self._modules["file"].ReadFile(handle, max(1, min(size, 1024 * 1024)))
            result = bytes(value)
            with self._lock:
                self.output_bytes += len(result)
            return result
        except Exception as exc:
            if getattr(exc, "winerror", None) in {109, 232}:
                return b""
            raise OSError("Broker process stream read failed") from exc

    def write(self, value: bytes) -> int:
        if self._stdin_closed:
            raise OSError("Broker process stdin is closed")
        try:
            _, written = self._modules["file"].WriteFile(self.stdin_handle, value)
            return int(written) if isinstance(written, int) else len(value)
        except Exception as exc:
            raise OSError("Broker process stream write failed") from exc

    def close_stdin(self) -> None:
        with self._lock:
            if self._stdin_closed:
                return
            self._stdin_closed = True
            self._modules["api"].CloseHandle(self.stdin_handle)

    def communicate(self, input_value: bytes | None, timeout: float | None) -> tuple[int | None, bytes, bytes]:
        if input_value:
            self.write(input_value)
        self.close_stdin()
        self._ensure_readers()
        code = self.wait(timeout)
        if code is not None:
            for thread in self._reader_threads:
                thread.join(timeout=5.0)
        with self._lock:
            return (
                code,
                b"".join(self._output_chunks["stdout"]),
                b"".join(self._output_chunks["stderr"]),
            )

    def _ensure_readers(self) -> None:
        with self._lock:
            if self._reader_threads:
                return

            def drain(stream: str) -> None:
                while True:
                    value = self.read(stream, 65536)
                    if not value:
                        return
                    with self._lock:
                        self._output_chunks[stream].append(value)

            self._reader_threads = tuple(
                threading.Thread(
                    target=drain,
                    args=(name,),
                    name=f"sandbox-{self.pid}-{name}",
                    daemon=True,
                )
                for name in ("stdout", "stderr")
            )
            for thread in self._reader_threads:
                thread.start()

    def terminate(self) -> int | None:
        self.job.terminate()
        return self.wait(5.0)

    def close(self) -> None:
        self.terminate()
        for thread in self._reader_threads:
            thread.join(timeout=5.0)
        for handle in (self.stdout_handle, self.stderr_handle, self.thread_handle, self.process_handle):
            try:
                self._modules["api"].CloseHandle(handle)
            except Exception:
                pass
        self.job.close()
