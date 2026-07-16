"""Task-input adapters used by application services before planning begins."""

from __future__ import annotations

import re

from mini_agent.tools import ToolError, ToolExecutor

_FILE_REFERENCE_PATTERN = re.compile(r"(?<!\w)@(?P<path>[^\s@]+)")
_TRAILING_PUNCTUATION = ".,;:!?)]}，。！？；："


class FileReferenceExpander:
    """Replace ``@path`` tokens with bounded, workspace-confined file content."""

    def __init__(self, tools: ToolExecutor, max_file_chars: int = 120_000, max_total_chars: int = 240_000) -> None:
        self._tools = tools
        self._max_file_chars = max_file_chars
        self._max_total_chars = max_total_chars

    def expand(self, task: str) -> str:
        """Expand every file reference in a task, preserving surrounding punctuation."""

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
            pieces.append(f"\n\n[Referenced file: {path}]\n{content}\n[End referenced file: {path}]\n")
            pieces.append(suffix)
            cursor = match.end()
        pieces.append(task[cursor:])
        return "".join(pieces)

    def _read(self, path: str) -> str:
        content = self._tools.invoke("read_file", {"path": path}, confirmed=True)
        if len(content) > self._max_file_chars:
            raise ToolError(f"Referenced file is too large: {path} (limit {self._max_file_chars} characters).")
        return content

    @staticmethod
    def _split_path_and_suffix(raw_path: str) -> tuple[str, str]:
        path = raw_path.rstrip(_TRAILING_PUNCTUATION)
        return path, raw_path[len(path) :]
