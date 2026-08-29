"""Workspace-confined file mutation operations."""

from __future__ import annotations

from ..base import ToolError


class FileWriteMixin:
    def create_directory(self, path: str) -> str:
        """Recursively create one approved-workspace directory."""

        directory_path = self._write_path(path)
        current = directory_path
        while not current.exists() and current not in self.workspaces:
            current = current.parent
        if current.exists() and not current.is_dir():
            raise ToolError(f"Not a directory: {self._display_candidate(current)}")
        if directory_path.is_dir():
            return f"Directory already exists: {self._display_path(directory_path)}."
        try:
            directory_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"Could not create directory {path}: {exc}") from exc
        return f"Created directory {self._display_path(directory_path)}."

    def write_file(self, path: str, content: str, overwrite: bool = False) -> str:
        """Create a new UTF-8 file or explicitly replace an existing file."""

        if not isinstance(content, str):
            raise ToolError("content must be a string.")
        if not isinstance(overwrite, bool):
            raise ToolError("overwrite must be a boolean.")
        file_path = self._write_path(path)
        if not file_path.parent.is_dir():
            self.create_directory(str(file_path.parent))
            file_path = self._write_path(path)
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

    def edit_file(
        self,
        path: str,
        start_line: int,
        end_line: int,
        expected_lines: list[str],
        replacement_lines: list[str],
    ) -> str:
        """Replace one inclusive line range after checking its current contents."""

        self._validate_integer("start_line", start_line, minimum=1)
        self._validate_integer("end_line", end_line, minimum=1)
        if end_line < start_line:
            raise ToolError("end_line must be greater than or equal to start_line.")
        for name, lines in (("expected_lines", expected_lines), ("replacement_lines", replacement_lines)):
            if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
                raise ToolError(f"{name} must be an array of strings.")
            if any("\n" in line or "\r" in line for line in lines):
                raise ToolError(f"{name} entries must not contain line-break characters.")
        if len(expected_lines) != end_line - start_line + 1:
            raise ToolError("expected_lines length must match the inclusive line range.")

        file_path = self._write_path(path)
        if not file_path.is_file():
            raise ToolError(f"Not a file: {path}")
        original = self._read_raw(file_path, path)
        records = self._line_records(original)
        if end_line > len(records):
            raise ToolError(f"Line range {start_line}-{end_line} exceeds the file's {len(records)} lines.")
        selected = [content for content, _ending in records[start_line - 1 : end_line]]
        if selected != expected_lines:
            raise ToolError("The selected lines no longer match expected_lines; file was not changed.")
        newline = self._dominant_newline(original)
        prefix = "".join(content + ending for content, ending in records[: start_line - 1])
        suffix = "".join(content + ending for content, ending in records[end_line:])
        selected_ending = records[end_line - 1][1]
        replacement = ""
        for index, line in enumerate(replacement_lines):
            is_last = index == len(replacement_lines) - 1
            ending = selected_ending if is_last else newline
            if is_last and not ending and not line:
                ending = newline
            replacement += line + ending
        updated = prefix + replacement + suffix
        self._atomic_replace(file_path, updated, expected_content=original)
        return f"Edited {self._display_path(file_path)}: replaced lines {start_line}-{end_line}."

    @staticmethod
    def _line_records(value: str) -> list[tuple[str, str]]:
        records: list[tuple[str, str]] = []
        for raw in value.splitlines(keepends=True):
            if raw.endswith("\r\n"):
                records.append((raw[:-2], "\r\n"))
            elif raw.endswith(("\n", "\r")):
                records.append((raw[:-1], raw[-1]))
            else:
                records.append((raw, ""))
        return records
