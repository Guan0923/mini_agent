"""Workspace-confined text-file discovery, search, and mutation tools."""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

from .base import ToolError


class WorkspaceFiles:
    """Perform deterministic text-file operations inside one workspace."""

    _MAX_READ_LINES = 1_000
    _MAX_OUTPUT_CHARS = 20_000
    _MAX_RESULTS = 1_000
    _MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
    _MAX_MATCH_CHARS = 500
    _IGNORED_DIRECTORIES = {
        ".git",
        ".mini_agent",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def read_text(self, path: str) -> str:
        """Read one complete UTF-8 file for trusted internal consumers."""

        file_path = self._read_path(path)
        if not file_path.is_file():
            raise ToolError(f"Not a file: {path}")
        return self._normalise_newlines(self._read_raw(file_path, path))

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200) -> str:
        """Read a bounded line range with stable LF output."""

        self._validate_integer("start_line", start_line, minimum=1)
        self._validate_integer("max_lines", max_lines, minimum=1, maximum=self._MAX_READ_LINES)
        file_path = self._read_path(path)
        if not file_path.is_file():
            raise ToolError(f"Not a file: {path}")

        content = self._normalise_newlines(self._read_raw(file_path, path))
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        first_index = start_line - 1
        selected = lines[first_index : first_index + max_lines]
        body = "".join(selected)
        truncated = len(body) > self._MAX_OUTPUT_CHARS
        if truncated:
            body = body[: self._MAX_OUTPUT_CHARS]

        end_line = start_line + len(selected) - 1 if selected else 0
        display_path = self._display_path(file_path)
        header = f"{display_path}: lines {start_line if selected else 0}-{end_line} of {total_lines}"
        if truncated:
            body += "\n... output truncated at 20000 characters; request a smaller line range."
        return f"{header}\n{body}"

    def glob(self, pattern: str, path: str = ".", max_results: int = 200) -> str:
        """List workspace files matching a segment-aware glob pattern."""

        pattern_parts = self._pattern_parts(pattern)
        self._validate_integer("max_results", max_results, minimum=1, maximum=self._MAX_RESULTS)
        root = self._read_path(path, allow_root=True)
        if not root.is_dir():
            raise ToolError(f"Not a directory: {path}")

        matches: list[str] = []
        truncated = False
        for file_path in self._iter_files(root):
            relative_to_root = file_path.relative_to(root).parts
            if not self._glob_matches(relative_to_root, pattern_parts):
                continue
            if len(matches) == max_results:
                truncated = True
                break
            matches.append(self._display_path(file_path))

        if not matches:
            return "(no matches)"
        if truncated:
            matches.append(f"... results truncated at {max_results} files.")
        return "\n".join(matches)

    def grep(
        self,
        pattern: str,
        path: str = ".",
        glob: str = "**/*",
        regex: bool = False,
        case_sensitive: bool = True,
        max_results: int = 200,
    ) -> str:
        """Search bounded UTF-8 files and return grep-style line matches."""

        if not isinstance(pattern, str) or not pattern:
            raise ToolError("pattern must be a non-empty string.")
        if not isinstance(regex, bool):
            raise ToolError("regex must be a boolean.")
        if not isinstance(case_sensitive, bool):
            raise ToolError("case_sensitive must be a boolean.")
        pattern_parts = self._pattern_parts(glob)
        self._validate_integer("max_results", max_results, minimum=1, maximum=self._MAX_RESULTS)
        expression = pattern if regex else re.escape(pattern)
        try:
            matcher = re.compile(expression, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ToolError(f"Invalid regular expression: {exc}") from exc

        root = self._read_path(path, allow_root=True)
        if not root.exists():
            raise ToolError(f"Path does not exist: {path}")
        if not root.is_dir() and not root.is_file():
            raise ToolError(f"Not a file or directory: {path}")

        results: list[str] = []
        output_chars = 0
        skipped = 0
        truncated = False
        files = (root,) if root.is_file() else self._iter_files(root)
        for file_path in files:
            relative_to_root = (file_path.name,) if root.is_file() else file_path.relative_to(root).parts
            if not self._glob_matches(relative_to_root, pattern_parts):
                continue
            try:
                if file_path.stat().st_size > self._MAX_SEARCH_FILE_BYTES:
                    skipped += 1
                    continue
                raw = file_path.read_bytes()
            except OSError as exc:
                raise ToolError(f"Unable to read {self._display_path(file_path)}: {exc}") from exc
            if b"\x00" in raw:
                skipped += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped += 1
                continue

            for line_number, line in enumerate(text.splitlines(), start=1):
                if matcher.search(line) is None:
                    continue
                preview = line if len(line) <= self._MAX_MATCH_CHARS else f"{line[: self._MAX_MATCH_CHARS]}..."
                result = f"{self._display_path(file_path)}:{line_number}:{preview}"
                if len(results) == max_results or output_chars + len(result) + 1 > self._MAX_OUTPUT_CHARS:
                    truncated = True
                    break
                results.append(result)
                output_chars += len(result) + 1
            if truncated:
                break

        output = results or ["(no matches)"]
        if truncated:
            output.append("... search results truncated.")
        if skipped:
            output.append(f"Skipped {skipped} binary, non-UTF-8, or oversized files.")
        return "\n".join(output)

    def write_file(self, path: str, content: str, overwrite: bool = False) -> str:
        """Create a new UTF-8 file or explicitly replace an existing file."""

        if not isinstance(content, str):
            raise ToolError("content must be a string.")
        if not isinstance(overwrite, bool):
            raise ToolError("overwrite must be a boolean.")
        file_path = self._write_path(path)
        if not file_path.parent.is_dir():
            raise ToolError(f"Parent directory does not exist: {self._display_candidate(file_path.parent)}")
        if file_path.exists() and not file_path.is_file():
            raise ToolError(f"Not a file: {path}")

        if file_path.exists():
            if not overwrite:
                raise ToolError(f"File already exists: {path}. Pass overwrite=true to replace it.")
            original = self._read_raw(file_path, path)
            self._atomic_replace(file_path, content, expected_content=original)
            return f"Replaced {self._display_path(file_path)} with {len(content)} characters."

        self._exclusive_create(file_path, content)
        return f"Created {self._display_path(file_path)} with {len(content)} characters."

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """Replace exactly one text block in an existing UTF-8 file."""

        if not isinstance(old_text, str) or not old_text:
            raise ToolError("old_text must be a non-empty string.")
        if not isinstance(new_text, str):
            raise ToolError("new_text must be a string.")
        if old_text == new_text:
            raise ToolError("old_text and new_text must be different.")

        file_path = self._write_path(path)
        if not file_path.is_file():
            raise ToolError(f"Not a file: {path}")
        original = self._read_raw(file_path, path)
        newline = self._dominant_newline(original)
        old_candidate = self._with_newline(old_text, newline)
        new_candidate = self._with_newline(new_text, newline)
        occurrences = original.count(old_candidate)
        if occurrences == 0:
            raise ToolError("old_text was not found; file was not changed.")
        if occurrences > 1:
            raise ToolError(
                f"old_text matched {occurrences} locations; include more surrounding context. File was not changed."
            )

        updated = original.replace(old_candidate, new_candidate, 1)
        self._atomic_replace(file_path, updated, expected_content=original)
        return f"Edited {self._display_path(file_path)}: replaced 1 occurrence."

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
        if normalised.startswith("/") or re.match(r"^[A-Za-z]:", normalised):
            raise ToolError("glob pattern must be relative.")
        parts = tuple(part for part in normalised.split("/") if part not in {"", "."})
        if not parts or ".." in parts:
            raise ToolError("glob pattern must stay inside the search path.")
        return parts

    @staticmethod
    def _glob_matches(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
        @cache
        def matches(path_index: int, pattern_index: int) -> bool:
            if pattern_index == len(pattern_parts):
                return path_index == len(path_parts)
            pattern = pattern_parts[pattern_index]
            if pattern == "**":
                return matches(path_index, pattern_index + 1) or (
                    path_index < len(path_parts) and matches(path_index + 1, pattern_index)
                )
            return (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], pattern)
                and matches(path_index + 1, pattern_index + 1)
            )

        return matches(0, 0)

    def _exclusive_create(self, file_path: Path, content: str) -> None:
        opened = False
        completed = False
        try:
            with file_path.open("x", encoding="utf-8", newline="") as handle:
                opened = True
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            completed = True
        except FileExistsError as exc:
            raise ToolError(f"File already exists: {self._display_path(file_path)}") from exc
        except OSError as exc:
            raise ToolError(f"Unable to create {self._display_path(file_path)}: {exc}") from exc
        finally:
            if opened and not completed:
                file_path.unlink(missing_ok=True)

    def _atomic_replace(self, file_path: Path, content: str, *, expected_content: str) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=file_path.parent,
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            current = self._read_raw(file_path, self._display_path(file_path))
            if current != expected_content:
                raise ToolError(f"File changed during the operation: {self._display_path(file_path)}")
            os.replace(temporary_path, file_path)
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(f"Unable to replace {self._display_path(file_path)}: {exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _read_raw(file_path: Path, display_path: str) -> str:
        try:
            with file_path.open("r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_path}") from exc
        except OSError as exc:
            raise ToolError(f"Unable to read {display_path}: {exc}") from exc

    @staticmethod
    def _normalise_newlines(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @classmethod
    def _with_newline(cls, value: str, newline: str) -> str:
        return cls._normalise_newlines(value).replace("\n", newline)

    @staticmethod
    def _dominant_newline(value: str) -> str:
        crlf = value.count("\r\n")
        lf = value.count("\n") - crlf
        cr = value.count("\r") - crlf
        counts = [(crlf, "\r\n"), (lf, "\n"), (cr, "\r")]
        count, newline = max(counts, key=lambda item: item[0])
        return newline if count else "\n"

    def _display_path(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def _display_candidate(self, path: Path) -> str:
        try:
            return self._display_path(path)
        except ValueError:
            return str(path)

    @staticmethod
    def _validate_integer(name: str, value: Any, *, minimum: int, maximum: int | None = None) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{name} must be an integer.")
        if value < minimum or maximum is not None and value > maximum:
            if maximum is None:
                raise ToolError(f"{name} must be at least {minimum}.")
            raise ToolError(f"{name} must be between {minimum} and {maximum}.")
