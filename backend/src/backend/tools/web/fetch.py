"""Public-only HTTP fetcher with bounded response handling."""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from ..base import ToolError
from .html import ReadableHtmlParser
from .protocols import HostResolver, HttpResponse, HttpSession
from .text import normalize_whitespace


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
            self._verify_connected_peer(response)
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

    def _verify_connected_peer(self, response: HttpResponse) -> None:
        """Reject a response whose real connection reached a non-public address.

        The pre-request hostname check narrows but cannot fully close the DNS
        rebinding window: the HTTP client resolves the host a second time when
        it actually connects. Verifying the connected peer catches a rebound
        connection and refuses to return its content. The check is duck-typed
        so injected test sessions (which expose no socket) are skipped, and a
        missing socket is treated as unverifiable rather than a failure.
        """
        raw = getattr(response, "raw", None)
        connection = getattr(raw, "_connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None:
            return
        try:
            peer = sock.getpeername()
            address = str(peer[0]).split("%", 1)[0]
            ip = ipaddress.ip_address(address)
        except (IndexError, OSError, ValueError):
            return
        if not ip.is_global:
            raise ToolError(
                "Web fetch connection reached a non-public address (possible DNS rebinding); response rejected."
            )

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
            parser = ReadableHtmlParser()
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
        return normalize_whitespace(value)

    @staticmethod
    def _truncate_output(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        omitted = len(value) - max_chars
        return f"{value[:max_chars]}\n\n… output truncated ({omitted} characters omitted)"
