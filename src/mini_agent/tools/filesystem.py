"""Workspace-confined text-file discovery, search, and mutation tools."""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import regex as regex_engine

from .base import ToolError


class WorkspaceFiles:
    """Perform deterministic text-file operations inside one workspace."""

    _MAX_READ_LINES = 1_000
    _MAX_OUTPUT_CHARS = 20_000
    _MAX_RESULTS = 1_000
    _MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
    _MAX_MATCH_CHARS = 500
    _MAX_GLOB_PATTERN_CHARS = 4_096
    _MAX_GLOB_PATTERN_PARTS = 256
    _REGEX_TIMEOUT_SECONDS = 0.1
    _TEXT_CHUNK_CHARS = 8_192
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

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = 200,
        start_column: int = 1,
    ) -> str:
        """Read a bounded line range with stable LF output and resumable columns."""

        self._validate_integer("start_line", start_line, minimum=1)
        self._validate_integer("max_lines", max_lines, minimum=1, maximum=self._MAX_READ_LINES)
        self._validate_integer("start_column", start_column, minimum=1)
        file_path = self._read_path(path)
        if not file_path.is_file():
            raise ToolError(f"Not a file: {path}")

        last_selected_line = start_line + max_lines - 1
        line_number = 1
        column = 1
        newline_count = 0
        any_content = False
        ends_with_newline = False
        start_line_length = 0
        body_chars: list[str] = []
        last_returned_line: int | None = None
        continuation: tuple[int, int] | None = None

        for chunk in self._iter_text_chunks(file_path, path):
            for character in chunk:
                any_content = True
                if line_number == start_line and character != "\n":
                    start_line_length += 1

                selected_line = start_line <= line_number <= last_selected_line
                selected_column = line_number != start_line or column >= start_column
                if selected_line and selected_column:
                    if len(body_chars) < self._MAX_OUTPUT_CHARS:
                        body_chars.append(character)
                        last_returned_line = line_number
                    elif continuation is None:
                        continuation = (line_number, column)

                if character == "\n":
                    newline_count += 1
                    line_number += 1
                    column = 1
                    ends_with_newline = True
                else:
                    column += 1
                    ends_with_newline = False

        total_lines = newline_count + (1 if any_content and not ends_with_newline else 0)
        if start_line <= total_lines and start_column > start_line_length + 1:
            raise ToolError(
                f"start_column must be between 1 and {start_line_length + 1} for line {start_line}."
            )

        if start_line > total_lines:
            first_display_line = 0
            end_display_line = 0
        else:
            first_display_line = start_line
            end_display_line = min(last_selected_line, total_lines)
            if continuation is not None and last_returned_line is not None:
                end_display_line = last_returned_line

        display_path = self._display_path(file_path)
        header = f"{display_path}: lines {first_display_line}-{end_display_line} of {total_lines}"
        if start_column != 1 and first_display_line:
            header += f", starting at column {start_column}"

        body = "".join(body_chars)
        if continuation is not None:
            next_line, next_column = continuation
            separator = "" if not body or body.endswith("\n") else "\n"
            body += (
                f"{separator}... output truncated at {self._MAX_OUTPUT_CHARS} characters; "
                f"continue with start_line={next_line} and start_column={next_column}."
            )
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
        expression = pattern if regex else regex_engine.escape(pattern)
        try:
            matcher = regex_engine.compile(expression, 0 if case_sensitive else regex_engine.IGNORECASE)
        except regex_engine.error as exc:
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
                try:
                    match = matcher.search(line, timeout=self._REGEX_TIMEOUT_SECONDS)
                except TimeoutError as exc:
                    raise ToolError(
                        f"Regular expression search timed out after {self._REGEX_TIMEOUT_SECONDS:g} seconds."
                    ) from exc
                if match is None:
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
            original_mode = stat.S_IMODE(file_path.stat().st_mode)
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
            os.chmod(temporary_path, original_mode)
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

    def _iter_text_chunks(self, file_path: Path, display_path: str) -> Iterator[str]:
        try:
            with file_path.open("r", encoding="utf-8", newline=None) as handle:
                while chunk := handle.read(self._TEXT_CHUNK_CHARS):
                    yield chunk
        except UnicodeDecodeError as exc:
            raise ToolError(f"File is not valid UTF-8: {display_path}") from exc
        except OSError as exc:
            raise ToolError(f"Unable to read {display_path}: {exc}") from exc

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
