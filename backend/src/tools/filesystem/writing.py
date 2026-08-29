"""Workspace-confined file mutation operations."""

from __future__ import annotations

from ..base import ToolError


class FileWriteMixin:
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
