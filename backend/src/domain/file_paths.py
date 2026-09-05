"""Shared session/project path names and confined local resolution."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

FILE_SOURCES = frozenset({"workspace", "project", "upload"})


def is_reference_path(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if Path(value).is_absolute():
        return True
    scope, separator, relative = value.partition(":")
    return bool(
        separator
        and scope in {"workspace", "project"}
        and relative
        and not relative.startswith(("/", "\\"))
        and ".." not in relative.replace("\\", "/").split("/")
        and ":" not in relative
        and "\x00" not in relative
    )


class ScopedPaths:
    def __init__(self, workspace: Path, project: Path | None = None) -> None:
        self.workspace = Path(os.path.abspath(workspace))
        self.project = Path(os.path.abspath(project)) if project is not None else None

    def root(self, scope: str) -> Path:
        if scope == "workspace":
            return self.workspace
        if scope == "project":
            if self.project is None:
                raise ValueError("当前会话没有项目目录。")
            return self.project
        raise ValueError("Unknown path prefix; use workspace: or project:.")

    @staticmethod
    def reject_links(path: Path, root: Path) -> None:
        current = root
        for part in (None, *path.relative_to(root).parts):
            if part is not None:
                current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError("Unable to inspect file path.") from exc
            if stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & 0x400:
                raise ValueError("Symbolic links and reparse points are not supported.")

    def resolve(self, value: str, *, allow_root: bool = False, read_roots: tuple[Path, ...] = ()) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("path must be a non-empty string.")
        normalized = value.strip().replace("\\", "/")
        scope, separator, relative = normalized.partition(":")
        if separator and scope in {"workspace", "project"}:
            root = self.root(scope)
            if relative.startswith("/") or ":" in relative:
                raise ValueError("The path after its prefix must be relative.")
            candidate = root / relative
            roots = (root,)
            parts = relative.split("/")
        else:
            candidate = Path(normalized)
            drive = re.match(r"^[A-Za-z]:", normalized)
            if drive and not candidate.is_absolute():
                raise ValueError("Drive-relative paths are not supported.")
            if ":" in normalized[2:] or (":" in normalized and not drive):
                raise ValueError("Unknown path prefix; use workspace: or project:.")
            if candidate.is_absolute():
                roots = (self.workspace, *((self.project,) if self.project is not None else ()), *read_roots)
            else:
                root = self.project if self.project is not None else self.workspace
                roots = (root,)
                candidate = root / candidate
            parts = normalized.split("/")
        if ".." in parts or "\x00" in normalized:
            raise ValueError("Path must stay inside an approved workspace.")
        lexical = Path(os.path.abspath(candidate))
        for root in roots:
            root = Path(os.path.abspath(root))
            if not lexical.is_relative_to(root):
                continue
            self.reject_links(lexical, root)
            try:
                resolved = lexical.resolve()
            except (OSError, RuntimeError) as exc:
                raise ValueError("Unable to resolve file path.") from exc
            if not resolved.is_relative_to(root.resolve()):
                continue
            if resolved == root.resolve() and not allow_root:
                raise ValueError("path must identify an entry inside an approved workspace.")
            return resolved
        raise ValueError("Path must stay inside an approved workspace.")

    def format(self, path: Path, *, scope: str | None = None) -> str:
        resolved = path.resolve()
        for name in (scope,) if scope else ("workspace", "project"):
            if name == "project" and self.project is None:
                continue
            root = self.root(name).resolve()
            if resolved.is_relative_to(root):
                relative = resolved.relative_to(root).as_posix()
                return f"{name}:{'' if relative == '.' else relative}"
        raise ValueError("Path must stay inside an approved workspace.")
