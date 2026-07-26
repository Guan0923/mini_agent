"""DuckDuckGo search through the local ddgr executable."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from ..base import ToolError
from .protocols import DdgrRunner
from .text import normalize_whitespace


class DdgrWebSearch:
    """Search DuckDuckGo through the locally installed ``ddgr`` executable."""

    _MAX_QUERY_CHARS = 500
    _MAX_RESULTS = 10
    _MAX_SNIPPET_CHARS = 2_000

    def __init__(self, executable: str = "ddgr", *, runner: DdgrRunner = subprocess.run) -> None:
        self._executable = executable
        self._runner = runner

    def search(self, query: str, max_results: int = 5) -> str:
        """Run a non-interactive JSON search and return compact result text."""
        self._validate(query, max_results)
        command = [self._executable, "--json", "--np", "-n", str(max_results), query.strip()]
        try:
            result = self._runner(
                command,
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                text=True,
                timeout=15,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                "ddgr is not installed or is not on PATH. Install the project's web dependency first."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolError("ddgr search timed out after 15 seconds.") from exc
        except OSError as exc:
            raise ToolError(f"Unable to start ddgr: {exc}") from exc

        if result.returncode != 0:
            details = self._format_process_output(result.stdout, result.stderr)
            suffix = f"\n{details}" if details else ""
            raise ToolError(f"ddgr exited with code {result.returncode}.{suffix}")
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ToolError("ddgr returned invalid JSON search results.") from exc
        if not isinstance(payload, list):
            raise ToolError("ddgr returned an unexpected JSON search result format.")

        formatted: list[str] = []
        for index, item in enumerate(payload[:max_results], start=1):
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            url = item.get("url")
            abstract = item.get("abstract", "")
            if not isinstance(title, str) or not isinstance(url, str) or not title.strip() or not url.strip():
                continue
            snippet = self._truncate(self._normalise_whitespace(abstract) if isinstance(abstract, str) else "")
            result_text = f"{index}. {title.strip()}\nURL: {url.strip()}"
            if snippet:
                result_text += f"\nSnippet: {snippet}"
            formatted.append(result_text)

        if not formatted:
            return "No web search results found."
        return "Web search results (untrusted external content):\n\n" + "\n\n".join(formatted)

    def _validate(self, query: Any, max_results: Any) -> None:
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query must be a non-empty string.")
        if len(query.strip()) > self._MAX_QUERY_CHARS:
            raise ToolError(f"query must not exceed {self._MAX_QUERY_CHARS} characters.")
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise ToolError("max_results must be an integer.")
        if not 1 <= max_results <= self._MAX_RESULTS:
            raise ToolError(f"max_results must be between 1 and {self._MAX_RESULTS}.")

    @classmethod
    def _format_process_output(cls, stdout: str | bytes | None, stderr: str | bytes | None) -> str:
        parts: list[str] = []
        if stdout:
            parts.append(f"stdout:\n{cls._truncate(cls._as_text(stdout))}")
        if stderr:
            parts.append(f"stderr:\n{cls._truncate(cls._as_text(stderr))}")
        return "\n".join(parts)

    @staticmethod
    def _as_text(value: str | bytes) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

    @classmethod
    def _truncate(cls, value: str) -> str:
        if len(value) <= cls._MAX_SNIPPET_CHARS:
            return value
        omitted = len(value) - cls._MAX_SNIPPET_CHARS
        return f"{value[: cls._MAX_SNIPPET_CHARS]}… ({omitted} characters omitted)"

    @staticmethod
    def _normalise_whitespace(value: str) -> str:
        return normalize_whitespace(value)
