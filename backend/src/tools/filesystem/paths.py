"""Reusable workspace path validation and traversal."""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from backend.domain.file_paths import ScopedPaths

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


def normalized_workspace_path(workspaces: Path | Iterable[Path], path: str) -> str:
    """Return one canonical lock key after enforcing multi-workspace confinement."""

    roots = (workspaces,) if isinstance(workspaces, Path) else tuple(workspaces)
    try:
        resolved = ScopedPaths(roots[0], roots[1] if len(roots) > 1 else None).resolve(path)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return os.path.normcase(str(resolved))


class WorkspacePathMixin:
    def _read_path(self, path: str, *, allow_root: bool = False) -> Path:
        return self._resolve_path(path, allow_root=allow_root)

    def _read_file_path(self, path: str) -> Path:
        """Resolve a scoped/bare path, or an approved absolute read-only path."""

        return self._resolve_path(path, read_roots=self.read_file_roots)

    def _resolve_path(self, path: str, *, allow_root: bool = False, read_roots: tuple[Path, ...] = ()) -> Path:
        try:
            return self.paths.resolve(path, allow_root=allow_root, read_roots=read_roots)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @staticmethod
    def _reject_reparse_points(path: Path, root: Path) -> None:
        try:
            ScopedPaths.reject_links(path, root)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def _write_path(self, path: str) -> Path:
        return self._resolve_path(path)

    def _iter_files(self, root: Path) -> Iterator[Path]:
        self._reject_reparse_points(root, root)
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            directories[:] = sorted(
                name
                for name in directories
                if name not in self._IGNORED_DIRECTORIES and not self._is_link(directory_path / name)
            )
            for name in sorted(filenames):
                file_path = directory_path / name
                if self._is_link(file_path) or not file_path.is_file():
                    continue
                resolved = file_path.resolve()
                if resolved == root or root not in resolved.parents:
                    continue
                yield resolved

    @staticmethod
    def _is_link(path: Path) -> bool:
        try:
            ScopedPaths.reject_links(path, path)
        except ValueError:
            return True
        return False

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
