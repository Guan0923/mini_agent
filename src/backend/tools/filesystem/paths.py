"""Reusable workspace path validation and traversal."""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path

from ..base import ToolError


def workspace_relative_parts(path: str, *, allow_root: bool = False) -> tuple[str, ...]:
    """Validate and normalize one model-supplied workspace-relative path."""

    if not isinstance(path, str) or not path.strip():
        raise ToolError("path must be a non-empty string.")
    normalised = path.replace("\\", "/")
    candidate = Path(normalised)
    if candidate.is_absolute() or re.match(r"^[A-Za-z]:", normalised):
        raise ToolError("path must be relative to the workspace.")
    parts = tuple(part for part in normalised.split("/") if part not in {"", "."})
    if ".." in parts:
        raise ToolError("Path must stay inside the workspace.")
    if not parts and not allow_root:
        raise ToolError("path must identify a file inside the workspace.")
    return parts


def normalized_workspace_path(workspace: Path, path: str) -> str:
    """Return one canonical lock key after enforcing workspace confinement."""

    if not isinstance(path, str) or not path.strip():
        raise ToolError("path must be a non-empty string.")
    normalised = path.replace("\\", "/")
    if Path(normalised).is_absolute() or re.match(r"^[A-Za-z]:", normalised):
        raise ToolError("path must be relative to the workspace.")
    root = workspace.resolve()
    resolved = root.joinpath(normalised).resolve()
    if resolved == root or root not in resolved.parents:
        raise ToolError("Path must stay inside the workspace.")
    return os.path.normcase(str(resolved))


class WorkspacePathMixin:
    def _read_path(self, path: str, *, allow_root: bool = False) -> Path:
        return self._resolve_relative(path, allow_root=allow_root)

    def _write_path(self, path: str) -> Path:
        relative = self._relative_parts(path, allow_root=False)
        current = self.workspace
        for part in relative:
            current /= part
            if current.is_symlink():
                raise ToolError("Symbolic links are not supported for writes.")
        return self._resolve_inside(current, allow_root=False)

    def _resolve_relative(self, path: str, *, allow_root: bool) -> Path:
        parts = self._relative_parts(path, allow_root=allow_root)
        return self._resolve_inside(self.workspace.joinpath(*parts), allow_root=allow_root)

    def _relative_parts(self, path: str, *, allow_root: bool) -> tuple[str, ...]:
        return workspace_relative_parts(path, allow_root=allow_root)

    def _resolve_inside(self, candidate: Path, *, allow_root: bool) -> Path:
        resolved = candidate.resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise ToolError("Path must stay inside the workspace.")
        if resolved == self.workspace and not allow_root:
            raise ToolError("path must identify a file inside the workspace.")
        return resolved

    def _iter_files(self, root: Path) -> Iterator[Path]:
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directories[:] = sorted(
                name
                for name in directories
                if name not in self._IGNORED_DIRECTORIES and not (directory_path / name).is_symlink()
            )
            for name in sorted(filenames):
                file_path = directory_path / name
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                resolved = file_path.resolve()
                if resolved == self.workspace or self.workspace not in resolved.parents:
                    continue
                yield resolved

    def _pattern_parts(self, pattern: str) -> tuple[str, ...]:
        if not isinstance(pattern, str) or not pattern.strip():
            raise ToolError("glob pattern must be a non-empty string.")
        normalised = pattern.replace("\\", "/")
        if len(normalised) > self._MAX_GLOB_PATTERN_CHARS:
            raise ToolError(f"glob pattern must not exceed {self._MAX_GLOB_PATTERN_CHARS} characters.")
        if normalised.startswith("/") or re.match(r"^[A-Za-z]:", normalised):
            raise ToolError("glob pattern must be relative.")
        parts = tuple(part for part in normalised.split("/") if part not in {"", "."})
        if not parts or ".." in parts:
            raise ToolError("glob pattern must stay inside the search path.")
        if len(parts) > self._MAX_GLOB_PATTERN_PARTS:
            raise ToolError(f"glob pattern must not contain more than {self._MAX_GLOB_PATTERN_PARTS} path segments.")
        return parts

    @staticmethod
    def _glob_matches(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
        def close_globstars(states: set[int]) -> set[int]:
            closed = set(states)
            for index, pattern in enumerate(pattern_parts):
                if index in closed and pattern == "**":
                    closed.add(index + 1)
            return closed

        states = close_globstars({0})
        for part in path_parts:
            next_states: set[int] = set()
            for index in states:
                if index == len(pattern_parts):
                    continue
                pattern = pattern_parts[index]
                if pattern == "**":
                    next_states.add(index)
                elif fnmatch.fnmatchcase(part, pattern):
                    next_states.add(index + 1)
            states = close_globstars(next_states)
            if not states:
                return False
        return len(pattern_parts) in close_globstars(states)
