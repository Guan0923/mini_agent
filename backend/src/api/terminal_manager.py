"""Process-owned Windows PTY sessions with Redis-backed output replay."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from threading import Condition, RLock, Thread, Timer, current_thread
from typing import Any
from uuid import uuid4

from backend.domain import MessageQueueUnavailable
from backend.domain.terminal import TERMINAL_LABELS, TerminalType, normalize_terminal_type
from backend.storage.terminal_stream import RedisTerminalOutputStream, TerminalOutputChunk
from backend.tools.terminal import terminal_executable, windows_workspace_to_wsl

TERMINAL_DISCONNECT_SECONDS = 30 * 60


def _terminal_argv(terminal_type: TerminalType, executable: str, cwd: Path) -> list[str]:
    if terminal_type == "cmd":
        return [executable]
    if terminal_type in {"powershell", "pwsh"}:
        return [executable, "-NoLogo"]
    if terminal_type == "git_bash":
        return [executable, "--login", "-i"]
    return [executable, "--cd", windows_workspace_to_wsl(cwd)]


def _terminate_process_tree(process: Any) -> None:
    if os.name != "nt" or not bool(process.isalive()):
        return
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0:
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.close(force=True)
    except (OSError, RuntimeError):
        pass


@dataclass(slots=True)
class TerminalSession:
    id: str
    terminal_type: TerminalType
    cwd: str
    process: Any
    output: RedisTerminalOutputStream
    reader_thread: Thread | None = None
    current_sequence: int = 0
    exit_code: int | None = None
    clients: int = 0
    closed: bool = False
    condition: Condition = field(default_factory=Condition)
    lock: RLock = field(default_factory=RLock)
    disconnect_timer: Timer | None = None

    @property
    def alive(self) -> bool:
        return not self.closed and self.exit_code is None and bool(self.process.isalive())

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "terminal_type": self.terminal_type,
            "terminal_label": TERMINAL_LABELS[self.terminal_type],
            "cwd": self.cwd,
            "last_sequence": self.current_sequence,
            "exit_code": self.exit_code,
            "alive": self.alive,
        }


class TerminalManager:
    def __init__(self, message_queue: object) -> None:
        client = getattr(message_queue, "client", None)
        key_prefix = getattr(message_queue, "key_prefix", "mini-agent:v1")
        self.output = RedisTerminalOutputStream(client, key_prefix=key_prefix) if client is not None else None
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = RLock()

    def create(self, terminal_type: object, cwd: str) -> TerminalSession:
        if os.name != "nt":
            raise RuntimeError("Interactive terminals are supported only on Windows.")
        if self.output is None:
            raise MessageQueueUnavailable("message_queue_unavailable")
        self.output.ping()
        selected = normalize_terminal_type(terminal_type)
        executable = terminal_executable(selected)
        if executable is None:
            raise RuntimeError(f"{TERMINAL_LABELS[selected]} is unavailable.")
        workspace = Path(cwd).resolve(strict=True)
        if not workspace.is_dir():
            raise RuntimeError("Terminal cwd is unavailable.")
        try:
            from winpty import PtyProcess

            process = PtyProcess.spawn(_terminal_argv(selected, executable, workspace), cwd=str(workspace))
        except (ImportError, OSError) as exc:
            raise RuntimeError("Unable to start the interactive terminal.") from exc
        terminal_id = f"terminal_{uuid4().hex}"
        session = TerminalSession(terminal_id, selected, str(workspace), process, self.output)
        with self._lock:
            self._sessions[terminal_id] = session
        reader = Thread(target=self._read_loop, args=(session,), name=f"terminal-reader-{terminal_id}", daemon=True)
        session.reader_thread = reader
        reader.start()
        return session

    def _read_loop(self, session: TerminalSession) -> None:
        try:
            while not session.closed and session.process.isalive():
                try:
                    data = session.process.read(16 * 1024)
                except (EOFError, OSError):
                    break
                if not data:
                    continue
                chunks = session.output.append(session.id, data)
                with session.condition:
                    if chunks:
                        session.current_sequence = chunks[-1].sequence
                    session.condition.notify_all()
        except MessageQueueUnavailable:
            _terminate_process_tree(session.process)
        finally:
            try:
                session.exit_code = session.process.wait()
            except (OSError, RuntimeError):
                session.exit_code = session.process.exitstatus
            try:
                session.process.close(force=True)
            except (OSError, RuntimeError):
                pass
            try:
                session.output.expire(session.id)
            except MessageQueueUnavailable:
                pass
            with session.condition:
                session.condition.notify_all()

    def get(self, terminal_id: str) -> TerminalSession | None:
        with self._lock:
            return self._sessions.get(terminal_id)

    def connect(self, terminal_id: str) -> TerminalSession:
        session = self.get(terminal_id)
        if session is None or session.closed:
            raise KeyError(terminal_id)
        with session.lock:
            if session.disconnect_timer is not None:
                session.disconnect_timer.cancel()
                session.disconnect_timer = None
            session.clients += 1
        return session

    def disconnect(self, terminal_id: str) -> None:
        session = self.get(terminal_id)
        if session is None:
            return
        with session.lock:
            session.clients = max(0, session.clients - 1)
            if session.clients == 0 and not session.closed:
                try:
                    session.output.expire(session.id)
                except MessageQueueUnavailable:
                    pass
                session.disconnect_timer = Timer(TERMINAL_DISCONNECT_SECONDS, self.close, args=(terminal_id,))
                session.disconnect_timer.daemon = True
                session.disconnect_timer.start()

    def write(self, terminal_id: str, data: str) -> None:
        session = self.get(terminal_id)
        if session is None or not session.alive:
            raise KeyError(terminal_id)
        if len(data.encode("utf-8")) > 16 * 1024:
            raise ValueError("Terminal input exceeds 16 KiB.")
        with session.lock:
            session.process.write(data)

    def resize(self, terminal_id: str, cols: int, rows: int) -> None:
        if not 2 <= cols <= 500 or not 2 <= rows <= 300:
            raise ValueError("Terminal dimensions are out of range.")
        session = self.get(terminal_id)
        if session is None or session.closed:
            raise KeyError(terminal_id)
        with session.lock:
            session.process.setwinsize(rows, cols)

    def wait_after(self, terminal_id: str, sequence: int, timeout: float = 1.0) -> list[TerminalOutputChunk]:
        session = self.get(terminal_id)
        if session is None or session.closed:
            raise KeyError(terminal_id)
        chunks = session.output.after(terminal_id, sequence)
        if chunks or session.exit_code is not None:
            return chunks
        with session.condition:
            session.condition.wait(timeout)
        return session.output.after(terminal_id, sequence)

    def close(self, terminal_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(terminal_id, None)
        if session is None:
            return
        with session.lock:
            session.closed = True
            if session.disconnect_timer is not None:
                session.disconnect_timer.cancel()
            _terminate_process_tree(session.process)
        if session.reader_thread is not None and session.reader_thread is not current_thread():
            session.reader_thread.join(timeout=5)
        try:
            session.output.delete(terminal_id)
        except MessageQueueUnavailable:
            pass
        with session.condition:
            session.condition.notify_all()

    def close_all(self) -> None:
        with self._lock:
            terminal_ids = list(self._sessions)
        for terminal_id in terminal_ids:
            self.close(terminal_id)


__all__ = ["TERMINAL_DISCONNECT_SECONDS", "TerminalManager", "TerminalSession"]
