"""Workspace-confined file operations."""

from __future__ import annotations

from pathlib import Path

from .base import ToolError


class WorkspaceFiles:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def list_files(self, path: str = ".") -> str:
        base = self._path(path)
        if not base.is_dir():
            raise ToolError(f"Not a directory: {path}")
        files = [item.relative_to(self.workspace).as_posix() for item in base.rglob("*") if item.is_file()]
        return "\n".join(sorted(files)) or "(no files)"

    def read_file(self, path: str) -> str:
        file_path = self._path(path)
        if not file_path.is_file():
            raise ToolError(f"Not a file: {path}")
        return file_path.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        file_path = self._path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {file_path.relative_to(self.workspace)}."

    def _path(self, relative_path: str) -> Path:
        candidate = (self.workspace / relative_path).resolve()
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise ToolError("Path must stay inside the workspace.")
        return candidate
