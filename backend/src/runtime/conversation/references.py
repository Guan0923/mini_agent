"""Task-input adapters used by application services before planning begins."""

from __future__ import annotations

import re

from backend.tools import ToolError, ToolExecutor, WorkspaceFiles

_FILE_REFERENCE_PATTERN = re.compile(r"(?<!\w)@(?P<path>[^\s@]+)")
_TRAILING_PUNCTUATION = ".,;:!?)]}，。！？；："


class FileReferenceExpander:
    """Replace ``@path`` tokens with bounded, workspace-confined file content."""

    def __init__(
        self,
        files: WorkspaceFiles | ToolExecutor,
        max_file_chars: int = 120_000,
        max_total_chars: int = 240_000,
    ) -> None:
        self._files = files if isinstance(files, WorkspaceFiles) else None
        self._tools = files if not isinstance(files, WorkspaceFiles) else None
        self._max_file_chars = max_file_chars
        self._max_total_chars = max_total_chars

    def expand(self, task: str, *, structured: bool = False) -> str:
        """Expand every file reference in a task, preserving surrounding punctuation.

        ``structured`` suppresses expansion entirely: when the caller already
        validated structured ``references`` metadata, the ``@path`` tokens in
        the prompt are presentation-only and must never be replaced with file
        contents (the agent reads files on demand through its tools).
        """

        if structured:
            return task

        matches = list(_FILE_REFERENCE_PATTERN.finditer(task))
        if not matches:
            return task

        pieces: list[str] = []
        cache: dict[str, str] = {}
        total_chars = 0
        cursor = 0
        for match in matches:
            path, suffix = self._split_path_and_suffix(match.group("path"))
            if not path:
                continue
            content = cache.get(path)
            if content is None:
                content = self._read(path)
                cache[path] = content
            total_chars += len(content)
            if total_chars > self._max_total_chars:
                raise ToolError(
                    f"Referenced file content is too large; keep the total under {self._max_total_chars} characters."
                )
            pieces.append(task[cursor : match.start()])
            is_pdf = path.lower().endswith(".pdf")
            reference = "Referenced PDF" if is_pdf else "Referenced file"
            pieces.append(f"\n\n[{reference}: {path}]\n{content}\n[End {reference}: {path}]\n")
            pieces.append(suffix)
            cursor = match.end()
        pieces.append(task[cursor:])
        return "".join(pieces)

    def _read(self, path: str) -> str:
        if path.lower().endswith(".pdf"):
            return (
                f"This is a PDF document; its binary content cannot be inlined. "
                f'Use the read_pdf tool with path="{path}" to extract its text and layout.'
            )
        if self._files is not None:
            content = self._files.read_text(path)
        else:
            assert self._tools is not None
            result = self._tools.invoke(
                "read_file",
                {"path": path, "start_line": 1, "max_lines": 1_000},
                confirmed=True,
            )
            header, separator, content = result.partition("\n")
            range_match = re.search(r"lines (\d+)-(\d+) of (\d+)$", header)
            if (
                not separator
                or range_match is None
                or int(range_match.group(2)) < int(range_match.group(3))
                or "output truncated" in content
            ):
                raise ToolError(f"Referenced file is too large to expand safely: {path}.")
        if len(content) > self._max_file_chars:
            raise ToolError(f"Referenced file is too large: {path} (limit {self._max_file_chars} characters).")
        return content

    @staticmethod
    def _split_path_and_suffix(raw_path: str) -> tuple[str, str]:
        path = raw_path.rstrip(_TRAILING_PUNCTUATION)
        return path, raw_path[len(path) :]
