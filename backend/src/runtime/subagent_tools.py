"""Workspace mutation locking for concurrent child agents."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Condition
from typing import Any

from backend.tools import ToolError, ToolInvocationContext
from backend.tools.filesystem import normalized_workspace_path


class WorkspaceWriteLock:
    """Allow unrelated file writes while excluding commands and same-path writes."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._paths: set[str] = set()
        self._command_active = False

    @contextmanager
    def file(self, path: str) -> Iterator[None]:
        with self._condition:
            while self._command_active or path in self._paths:
                self._condition.wait()
            self._paths.add(path)
        try:
            yield
        finally:
            with self._condition:
                self._paths.remove(path)
                self._condition.notify_all()

    @contextmanager
    def command(self) -> Iterator[None]:
        with self._condition:
            while self._command_active or self._paths:
                self._condition.wait()
            self._command_active = True
        try:
            yield
        finally:
            with self._condition:
                self._command_active = False
                self._condition.notify_all()


class LockedToolExecutor:
    """Delegate the tool port while locking only workspace mutation operations."""

    def __init__(self, tools: object, locks: WorkspaceWriteLock, workspace: Path | None = None) -> None:
        self._tools = tools
        self._locks = locks
        self._workspace = (workspace or Path(".")).resolve()

    def names(self) -> list[str]:
        return self._tools.names()

    def read_only_names(self) -> list[str]:
        return self._tools.read_only_names()

    def specs(self):
        return self._tools.specs()

    def read_only_specs(self):
        return self._tools.read_only_specs()

    def is_read_only(self, name: str) -> bool:
        return self._tools.is_read_only(name)

    def requires_confirmation(self, name: str) -> bool:
        return self._tools.requires_confirmation(name)

    def is_workspace_confined(self, name: str) -> bool:
        return self._tools.is_workspace_confined(name)

    def is_retryable(self, name: str) -> bool:
        return self._tools.is_retryable(name)

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        self._tools.validate_arguments(name, arguments)

    def invoke(self, name: str, arguments: dict[str, Any], confirmed: bool = False) -> str:
        return self._invoke(name, arguments, confirmed=confirmed)

    def invoke_with_context(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolInvocationContext,
        confirmed: bool = False,
    ) -> str:
        return self._invoke(name, arguments, confirmed=confirmed, context=context)

    def _invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        confirmed: bool,
        context: ToolInvocationContext | None = None,
    ) -> str:
        def call() -> str:
            if context is not None and hasattr(self._tools, "invoke_with_context"):
                return self._tools.invoke_with_context(name, arguments, context, confirmed=confirmed)
            return self._tools.invoke(name, arguments, confirmed=confirmed)

        if name in {"write_file", "edit_file"}:
            path = arguments.get("path")
            if not isinstance(path, str):
                raise ToolError("Workspace mutation requires a path.")
            with self._locks.file(normalized_workspace_path(self._workspace, path)):
                return call()
        if name in {"create_directory", "run_command"}:
            with self._locks.command():
                return call()
        return call()
