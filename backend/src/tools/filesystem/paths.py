"""Reusable workspace path validation and traversal."""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from collections.abc import Iterable, Iterator
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


def normalized_workspace_path(workspaces: Path | Iterable[Path], path: str) -> str:
    """Return one canonical lock key after enforcing multi-workspace confinement."""

    if not isinstance(path, str) or not path.strip():
        raise ToolError("path must be a non-empty string.")
    normalised = path.strip().replace("\\", "/")
    candidate = Path(normalised)
    if not candidate.is_absolute() and not re.match(r"^[A-Za-z]:", normalised):
        raise ToolError("path must be absolute.")
    resolved = Path(os.path.abspath(candidate)).resolve()
    roots = (workspaces,) if isinstance(workspaces, Path) else tuple(workspaces)
    if not any(resolved != root.resolve() and resolved.is_relative_to(root.resolve()) for root in roots):
        raise ToolError("Path must stay inside an approved workspace.")
    return os.path.normcase(str(resolved))


class WorkspacePathMixin:
    def _read_path(self, path: str, *, allow_root: bool = False) -> Path:
        return self._resolve_absolute(path, self.workspaces, allow_root=allow_root)

    def _read_file_path(self, path: str) -> Path:
        """Resolve an absolute file path inside a workspace or read-only root."""

        return self._resolve_absolute(path, (*self.workspaces, *self.read_file_roots), allow_root=False)

    def _resolve_absolute(self, path: str, roots: Iterable[Path], *, allow_root: bool) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path must be a non-empty string.")
        normalised = path.strip().replace("\\", "/")
        candidate = Path(normalised)
        if not candidate.is_absolute() and not re.match(r"^[A-Za-z]:", normalised):
            raise ToolError("path must be absolute.")
        if ".." in candidate.parts:
            raise ToolError("Path must stay inside an approved workspace.")
        lexical = Path(os.path.abspath(candidate))
        resolved = lexical.resolve()
        for root in roots:
            lexical_root = Path(os.path.abspath(root))
            try:
                lexical.relative_to(lexical_root)
                resolved.relative_to(root)
            except ValueError:
                continue
            self._reject_reparse_points(lexical, lexical_root)
            if resolved == root and not allow_root:
                raise ToolError("path must identify an entry inside an approved workspace.")
            return resolved
        raise ToolError("Absolute path must stay inside an approved workspace.")

    @staticmethod
    def _reject_reparse_points(path: Path, root: Path) -> None:
        current = root
        candidates = [root]
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ToolError("Path must stay inside an allowed read root.") from exc
        for part in relative.parts:
            current /= part
            candidates.append(current)
        for candidate in candidates:
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ToolError(f"Unable to inspect whitelisted path: {candidate}") from exc
            attributes = int(getattr(info, "st_file_attributes", 0))
            if stat.S_ISLNK(info.st_mode) or attributes & 0x400:
                raise ToolError(f"Symbolic links and reparse points are not supported: {candidate}")

    def _write_path(self, path: str) -> Path:
        return self._resolve_absolute(path, self.workspaces, allow_root=False)

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
                if resolved == root or root not in resolved.parents:
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
