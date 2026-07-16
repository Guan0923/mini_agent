"""Controlled public-web search and fetch tools."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
from collections.abc import Callable, Iterable, Mapping
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from .base import ToolError

DdgrRunner = Callable[..., subprocess.CompletedProcess[str]]
HostResolver = Callable[..., list[tuple[Any, ...]]]


class HttpResponse(Protocol):
    """Subset of a streamed requests response used by ``SafeWebFetcher``."""

    status_code: int
    headers: Mapping[str, str]
    encoding: str | None

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False) -> Iterable[bytes]: ...

    def close(self) -> None: ...


class HttpSession(Protocol):
    """Subset of ``requests.Session`` needed for dependency-free tests."""

    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...


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
        return re.sub(r"\s+", " ", value).strip()


class SafeWebFetcher:
    """Fetch limited public HTTP content while applying basic SSRF protections."""

    _MAX_REDIRECTS = 3
    _MAX_RESPONSE_BYTES = 2_000_000
    _MAX_OUTPUT_CHARS = 100_000
    _DEFAULT_OUTPUT_CHARS = 50_000
    _ALLOWED_CONTENT_TYPES = {"text/html", "text/plain", "application/json"}
    _REDIRECT_STATUSES = {301, 302, 303, 307, 308}
    _USER_AGENT = "Mini-Agent/0.1 (+https://example.invalid/mini-agent)"

    def __init__(
        self,
        *,
        session: HttpSession | None = None,
        resolver: HostResolver = socket.getaddrinfo,
    ) -> None:
        if session is None:
            requests_session = requests.Session()
            requests_session.trust_env = False
            self._session: HttpSession = requests_session
        else:
            self._session = session
        self._resolver = resolver

    def fetch(self, url: str, max_chars: int = _DEFAULT_OUTPUT_CHARS) -> str:
        """Fetch a public HTML, text, or JSON resource as limited readable text."""
        self._validate_max_chars(max_chars)
        current_url = self._normalise_url(url)
        for redirect_count in range(self._MAX_REDIRECTS + 1):
            self._assert_public_target(current_url)
            response = self._request(current_url)
            try:
                if response.status_code in self._REDIRECT_STATUSES:
                    current_url = self._redirect_target(response, current_url, redirect_count)
                    continue
                if not 200 <= response.status_code < 300:
                    raise ToolError(f"Web fetch failed with HTTP status {response.status_code}.")

                content_type = self._content_type(response.headers)
                if content_type not in self._ALLOWED_CONTENT_TYPES:
                    allowed = ", ".join(sorted(self._ALLOWED_CONTENT_TYPES))
                    raise ToolError(f"Unsupported content type {content_type or 'missing'}; allowed types: {allowed}.")
                body = self._read_limited_body(response)
                text = body.decode(response.encoding or "utf-8", errors="replace")
                title, readable = self._extract_content(text, content_type)
                readable = self._truncate_output(readable, max_chars)
                header = f"Fetched URL: {current_url}\nContent type: {content_type}"
                if title:
                    header += f"\nTitle: {title}"
                return f"{header}\n\nUntrusted external content:\n{readable or '(The page had no readable text.)'}"
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        raise ToolError(f"Web fetch exceeded the redirect limit of {self._MAX_REDIRECTS}.")

    def _request(self, url: str) -> HttpResponse:
        try:
            return self._session.get(
                url,
                headers={"Accept": "text/html, text/plain, application/json", "User-Agent": self._USER_AGENT},
                allow_redirects=False,
                stream=True,
                timeout=(5, 15),
            )
        except requests.RequestException as exc:
            raise ToolError(f"Unable to fetch URL: {exc}") from exc
        except OSError as exc:
            raise ToolError(f"Unable to fetch URL: {exc}") from exc

    def _redirect_target(self, response: HttpResponse, current_url: str, redirect_count: int) -> str:
        if redirect_count >= self._MAX_REDIRECTS:
            raise ToolError(f"Web fetch exceeded the redirect limit of {self._MAX_REDIRECTS}.")
        location = self._header(response.headers, "location")
        if not location:
            raise ToolError("Redirect response did not include a Location header.")
        return self._normalise_url(urljoin(current_url, location))

    def _assert_public_target(self, url: str) -> None:
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = self._resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ToolError(f"Unable to resolve web host: {parsed.hostname}.") from exc
        except OSError as exc:
            raise ToolError(f"Unable to resolve web host: {parsed.hostname}.") from exc
        if not addresses:
            raise ToolError(f"Web host resolved without addresses: {parsed.hostname}.")
        for address_info in addresses:
            try:
                address = str(address_info[4][0]).split("%", 1)[0]
                ip = ipaddress.ip_address(address)
            except (IndexError, ValueError) as exc:
                raise ToolError(f"Web host resolved to an invalid address: {parsed.hostname}.") from exc
            if not ip.is_global:
                raise ToolError("Web fetch refuses loopback, private, link-local, or reserved network addresses.")

    def _normalise_url(self, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ToolError("url must be a non-empty string.")
        try:
            parsed = urlsplit(value.strip())
            port = parsed.port
        except ValueError as exc:
            raise ToolError("url has an invalid port.") from exc
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ToolError("url must use http or https.")
        if parsed.username is not None or parsed.password is not None:
            raise ToolError("url must not include credentials.")
        if not parsed.hostname:
            raise ToolError("url must include a host.")
        if port is not None and port not in {80, 443}:
            raise ToolError("url ports must be 80 or 443.")
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))

    def _read_limited_body(self, response: HttpResponse) -> bytes:
        length = self._header(response.headers, "content-length")
        if length:
            try:
                if int(length) > self._MAX_RESPONSE_BYTES:
                    raise ToolError(f"Web response exceeds the {self._MAX_RESPONSE_BYTES}-byte limit.")
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
                raise ToolError(f"Web response exceeds the {self._MAX_RESPONSE_BYTES}-byte limit.")
            chunks.append(chunk_bytes)
        return b"".join(chunks)

    def _extract_content(self, text: str, content_type: str) -> tuple[str | None, str]:
        if content_type == "text/html":
            parser = _ReadableHtmlParser()
            parser.feed(text)
            parser.close()
            return parser.title, parser.text
        if content_type == "application/json":
            try:
                return None, json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
        return None, self._normalise_whitespace(text)

    def _validate_max_chars(self, max_chars: Any) -> None:
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise ToolError("max_chars must be an integer.")
        if not 1 <= max_chars <= self._MAX_OUTPUT_CHARS:
            raise ToolError(f"max_chars must be between 1 and {self._MAX_OUTPUT_CHARS}.")

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        for key, value in headers.items():
            if key.lower() == name:
                return value
        return None

    @classmethod
    def _content_type(cls, headers: Mapping[str, str]) -> str:
        raw = cls._header(headers, "content-type")
        return raw.split(";", 1)[0].strip().lower() if raw else ""

    @staticmethod
    def _normalise_whitespace(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _truncate_output(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        omitted = len(value) - max_chars
        return f"{value[:max_chars]}\n\n… output truncated ({omitted} characters omitted)"


class _ReadableHtmlParser(HTMLParser):
    """Small dependency-free extractor for static HTML content."""

    _IGNORED_TAGS = {"canvas", "footer", "form", "iframe", "nav", "noscript", "script", "style", "svg"}
    _BLOCK_TAGS = {"article", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "main", "p", "section", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    @property
    def title(self) -> str | None:
        title = SafeWebFetcher._normalise_whitespace(" ".join(self._title_parts))
        return title or None

    @property
    def text(self) -> str:
        return SafeWebFetcher._normalise_whitespace(" ".join(self._text_parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._title_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._title_depth = max(0, self._title_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._title_depth:
            self._title_parts.append(data)
        else:
            self._text_parts.append(data)
