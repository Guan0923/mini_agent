"""Bounded text reading, globbing, and grep operations."""

from __future__ import annotations

import regex as regex_engine

from ..base import ToolError


class FileReadMixin:
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
            raise ToolError(f"start_column must be between 1 and {start_line_length + 1} for line {start_line}.")

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
