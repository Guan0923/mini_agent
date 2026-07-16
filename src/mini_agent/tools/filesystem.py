"""Workspace-confined file operations."""

from __future__ import annotations

import shutil
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

    def delete_file(self, path: str) -> str:
        """Delete one workspace-confined regular file."""

        file_path = self._file_path(path)
        file_path.unlink()
        return f"Deleted file {self._relative_path(file_path)}."

    def delete_folder(self, path: str, recursive: bool = False) -> str:
        """Delete an empty directory or, when explicit, its complete tree."""

        if not isinstance(recursive, bool):
            raise ToolError("recursive must be a boolean.")
        folder_path = self._folder_path(path)
        if recursive:
            self._reject_tree_symbolic_links(folder_path)
            shutil.rmtree(folder_path)
            return f"Deleted folder {self._relative_path(folder_path)} and its contents."
        if any(folder_path.iterdir()):
            raise ToolError(f"Directory is not empty: {path}. Pass recursive=true to delete its contents.")
        folder_path.rmdir()
        return f"Deleted empty folder {self._relative_path(folder_path)}."

    def move_file(self, source: str, destination: str) -> str:
        """Move one regular file to a new, non-existent workspace path."""

        source_path = self._file_path(source)
        destination_path = self._destination_path(destination)
        if source_path == destination_path:
            raise ToolError("Source and destination must be different.")
        source_path.rename(destination_path)
        return f"Moved file {self._relative_path(source_path)} to {self._relative_path(destination_path)}."

    def move_folder(self, source: str, destination: str) -> str:
        """Move one directory tree to a new, non-existent workspace path."""

        source_path = self._folder_path(source)
        destination_path = self._destination_path(destination)
        if source_path == destination_path:
            raise ToolError("Source and destination must be different.")
        if source_path in destination_path.parents:
            raise ToolError("Cannot move a folder into itself or one of its descendants.")
        self._reject_tree_symbolic_links(source_path)
        source_path.rename(destination_path)
        return f"Moved folder {self._relative_path(source_path)} to {self._relative_path(destination_path)}."

    def _path(self, relative_path: str) -> Path:
        return self._resolve_path(self.workspace / relative_path)

    def _file_path(self, path: str) -> Path:
        file_path = self._destructive_path(path)
        if not file_path.is_file():
            raise ToolError(f"Not a file: {path}")
        return file_path

    def _folder_path(self, path: str) -> Path:
        folder_path = self._destructive_path(path)
        if folder_path == self.workspace:
            raise ToolError("Cannot operate on the workspace root.")
        if not folder_path.is_dir():
            raise ToolError(f"Not a directory: {path}")
        return folder_path

    def _destination_path(self, path: str) -> Path:
        destination_path = self._destructive_path(path)
        if destination_path == self.workspace:
            raise ToolError("Cannot use the workspace root as a destination.")
        if destination_path.exists():
            raise ToolError(f"Destination already exists: {path}")
        if not destination_path.parent.is_dir():
            raise ToolError(f"Destination parent directory does not exist: {destination_path.parent}")
        return destination_path

    def _destructive_path(self, path: str) -> Path:
        candidate = self.workspace / path
        self._reject_symbolic_links(candidate)
        return self._resolve_path(candidate)

    def _resolve_path(self, candidate: Path) -> Path:
        resolved = candidate.resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise ToolError("Path must stay inside the workspace.")
        return resolved

    def _reject_symbolic_links(self, candidate: Path) -> None:
        """Reject any symbolic link component before a destructive operation."""

        try:
            relative_path = candidate.relative_to(self.workspace)
        except ValueError:
            return
        current = self.workspace
        for part in relative_path.parts:
            current /= part
            if current.is_symlink():
                raise ToolError("Symbolic links are not supported for destructive file operations.")

    @staticmethod
    def _reject_tree_symbolic_links(folder_path: Path) -> None:
        for item in folder_path.rglob("*"):
            if item.is_symlink():
                raise ToolError("Recursive folder operations do not support symbolic links.")

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()
