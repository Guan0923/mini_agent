"""DuckDuckGo HTML search with bounded, explicit HTTP handling."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import requests

from ..base import ToolError
from .text import normalize_whitespace


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class _DuckDuckGoParser(HTMLParser):
    """Extract only result anchors and snippets from DuckDuckGo's HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._title_href: str | None = None
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self.close_result()
            self._title_href = attributes.get("href")
            self._title_parts = []
            self._in_title = True
            return
        if "result__snippet" in classes:
            self._snippet_parts = []
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
            return
        if self._in_snippet and tag in {"div", "a", "p"}:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def close_result(self) -> None:
        if self._title_href is None:
            return
        self.results.append(
            SearchResult(
                normalize_whitespace("".join(self._title_parts)),
                self._title_href,
                normalize_whitespace("".join(self._snippet_parts)),
            )
        )
        self._title_href = None
        self._title_parts = []
        self._snippet_parts = []


class DuckDuckGoWebSearch:
    """Search DuckDuckGo's fixed public HTML endpoint without a subprocess.

    The former ``ddgr`` executable reports DuckDuckGo HTTP 202 responses as a
    successful empty JSON list in some network environments.  Keeping the
    HTTP request in-process lets the tool distinguish a valid empty result page
    from a transport or anti-bot failure and keeps the dependency injectable.
    """

    _ENDPOINT = "https://html.duckduckgo.com/html/"
    _MAX_QUERY_CHARS = 500
    _MAX_RESULTS = 10
    _MAX_RESPONSE_BYTES = 1_000_000
    _MAX_SNIPPET_CHARS = 2_000
    _USER_AGENT = "Mini-Agent/0.1 (+https://example.invalid/mini-agent)"

    def __init__(
        self,
        *,
        session: Any | None = None,
    ) -> None:
        if session is None:
            requests_session = requests.Session()
            # The endpoint is fixed and not user-controlled.  Avoid inheriting
            # arbitrary process proxies for this security-sensitive client.
            requests_session.trust_env = False
            self._session = requests_session
        else:
            self._session = session

    def search(self, query: str, max_results: int = 5) -> str:
        self._validate(query, max_results)
        try:
            headers = {"Accept": "text/html", "User-Agent": self._USER_AGENT}
            response = self._session.get(
                self._ENDPOINT,
                params={"q": query.strip()},
                headers=headers,
                allow_redirects=False,
                stream=True,
                timeout=(5, 15),
            )
        except requests.RequestException as exc:
            raise ToolError(f"Unable to start web search: {exc}") from exc
        except OSError as exc:
            raise ToolError(f"Unable to start web search: {exc}") from exc

        try:
            if response.status_code != 200:
                raise ToolError(f"DuckDuckGo search failed with HTTP status {response.status_code}.")
            try:
                body = self._read_limited_body(response)
            except (requests.RequestException, OSError) as exc:
                raise ToolError(f"Unable to read web search response: {exc}") from exc
            parser = _DuckDuckGoParser()
            parser.feed(body.decode(getattr(response, "encoding", None) or "utf-8", errors="replace"))
            parser.close()
            parser.close_result()
            if not parser.results:
                lowered = body.lower()
                if b"no-results" in lowered:
                    return "No web search results found."
                raise ToolError("DuckDuckGo returned an unrecognizable search result page.")

            formatted: list[str] = []
            seen: set[str] = set()
            for result in parser.results:
                url = self._result_url(result.url)
                if not result.title or not url or url in seen:
                    continue
                seen.add(url)
                snippet = self._truncate(result.snippet)
                line = f"{len(formatted) + 1}. {result.title}\nURL: {url}"
                if snippet:
                    line += f"\nSnippet: {snippet}"
                formatted.append(line)
                if len(formatted) >= max_results:
                    break
            if not formatted:
                raise ToolError("DuckDuckGo returned search entries without usable URLs.")
            return "Web search results (untrusted external content):\n\n" + "\n\n".join(formatted)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def _read_limited_body(self, response: Any) -> bytes:
        content_length = None
        headers = getattr(response, "headers", {})
        for key, value in headers.items():
            if key.lower() == "content-length":
                content_length = value
                break
        if content_length:
            try:
                if int(content_length) > self._MAX_RESPONSE_BYTES:
                    raise ToolError("Web search response exceeds the 1000000-byte limit.")
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            chunk_bytes = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
            total += len(chunk_bytes)
            if total > self._MAX_RESPONSE_BYTES:
                raise ToolError("Web search response exceeds the 1000000-byte limit.")
            chunks.append(chunk_bytes)
        return b"".join(chunks)

    @staticmethod
    def _result_url(value: str) -> str | None:
        candidate = urljoin(DuckDuckGoWebSearch._ENDPOINT, value.strip())
        parsed = urlsplit(candidate)
        if parsed.path == "/l/":
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            candidate = unquote(target)
            parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        return candidate

    @classmethod
    def _truncate(cls, value: str) -> str:
        if len(value) <= cls._MAX_SNIPPET_CHARS:
            return value
        omitted = len(value) - cls._MAX_SNIPPET_CHARS
        return f"{value[: cls._MAX_SNIPPET_CHARS]}… ({omitted} characters omitted)"

    def _validate(self, query: Any, max_results: Any) -> None:
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query must be a non-empty string.")
        if len(query.strip()) > self._MAX_QUERY_CHARS:
            raise ToolError(f"query must not exceed {self._MAX_QUERY_CHARS} characters.")
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise ToolError("max_results must be an integer.")
        if not 1 <= max_results <= self._MAX_RESULTS:
            raise ToolError(f"max_results must be between 1 and {self._MAX_RESULTS}.")


# Preserve the existing import path for callers that still use the old class
# name while making the new implementation explicit in public APIs.
DdgrWebSearch = DuckDuckGoWebSearch

__all__ = ["DdgrWebSearch", "DuckDuckGoWebSearch", "SearchResult"]
