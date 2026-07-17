"""Controlled public-web search and fetch tools."""

from __future__ import annotations

import codecs
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import urllib3
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util import Timeout

from .base import ToolError
from .html_markdown import extract_html_document

DdgrRunner = Callable[..., subprocess.CompletedProcess[str]]
HostResolver = Callable[..., list[tuple[Any, ...]]]
Clock = Callable[[], float]


class HttpResponse(Protocol):
    """Streamed response contract used by ``SafeWebFetcher``."""

    status_code: int
    headers: Mapping[str, str]

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False) -> Iterable[bytes]: ...

    def close(self) -> None: ...


class HttpTransport(Protocol):
    """Transport that can connect only to caller-validated IP addresses."""

    def request(
        self,
        url: str,
        addresses: tuple[str, ...],
        *,
        connect_timeout: float,
        read_timeout: float,
        deadline: float,
    ) -> HttpResponse: ...


class PinnedHttpTransport:
    """Issue direct urllib3 requests to a validated DNS snapshot."""

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self._clock = clock

    def request(
        self,
        url: str,
        addresses: tuple[str, ...],
        *,
        connect_timeout: float,
        read_timeout: float,
        deadline: float,
    ) -> HttpResponse:
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += f"?{parsed.query}"
        headers = {
            "Accept": "text/html, text/plain, application/json",
            "Accept-Encoding": "gzip, deflate",
            "Host": self._host_header(parsed.hostname, port, parsed.scheme),
            "User-Agent": SafeWebFetcher._USER_AGENT,
        }

        last_error: Exception | None = None
        for address in addresses:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError("Web fetch exceeded its overall time budget.")
            timeout = Timeout(
                connect=min(connect_timeout, remaining),
                read=min(read_timeout, remaining),
            )
            pool = self._pool(parsed.scheme, address, port, parsed.hostname)
            try:
                response = pool.urlopen(
                    "GET",
                    request_target,
                    headers=headers,
                    redirect=False,
                    retries=False,
                    preload_content=False,
                    decode_content=True,
                    timeout=timeout,
                )
            except Exception as exc:
                pool.close()
                last_error = exc
                continue
            return _Urllib3Response(response, pool)

        if last_error is not None:
            raise last_error
        raise OSError("No validated IP addresses were available for the request.")

    @staticmethod
    def _pool(scheme: str, address: str, port: int, hostname: str):
        if scheme == "https":
            return HTTPSConnectionPool(
                address,
                port=port,
                retries=False,
                cert_reqs=ssl.CERT_REQUIRED,
                assert_hostname=hostname,
                server_hostname=hostname,
            )
        return HTTPConnectionPool(address, port=port, retries=False)

    @staticmethod
    def _host_header(hostname: str, port: int, scheme: str) -> str:
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if scheme == "https" else 80
        return host if port == default_port else f"{host}:{port}"


class _Urllib3Response:
    """Adapt urllib3's streamed response and own its connection pool."""

    def __init__(self, response: urllib3.response.BaseHTTPResponse, pool: Any) -> None:
        self._response = response
        self._pool = pool
        self.status_code = response.status
        self.headers = response.headers

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False) -> Iterable[bytes]:
        del decode_unicode
        return self._response.stream(chunk_size, decode_content=True)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._pool.close()


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
    """Fetch bounded public HTTP content with DNS-pinned SSRF protection."""

    _MAX_REDIRECTS = 3
    _MAX_RESPONSE_BYTES = 2_000_000
    _MAX_OUTPUT_CHARS = 100_000
    _DEFAULT_OUTPUT_CHARS = 50_000
    _TOTAL_TIMEOUT_SECONDS = 30.0
    _CONNECT_TIMEOUT_SECONDS = 5.0
    _READ_TIMEOUT_SECONDS = 15.0
    _ALLOWED_CONTENT_TYPES = {"text/html", "text/plain", "application/json"}
    _REDIRECT_STATUSES = {301, 302, 303, 307, 308}
    _USER_AGENT = "Mini-Agent/0.1 (+https://example.invalid/mini-agent)"
    _EMPTY_CONTENT = "(The page had no readable text.)"

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        resolver: HostResolver = socket.getaddrinfo,
        clock: Clock = time.monotonic,
    ) -> None:
        self._transport = transport or PinnedHttpTransport(clock=clock)
        self._resolver = resolver
        self._clock = clock

    def fetch(self, url: str, max_chars: int = _DEFAULT_OUTPUT_CHARS) -> str:
        """Fetch a public HTML, text, or JSON resource as bounded readable content."""
        self._validate_max_chars(max_chars)
        deadline = self._clock() + self._TOTAL_TIMEOUT_SECONDS
        current_url = self._normalise_url(url)
        for redirect_count in range(self._MAX_REDIRECTS + 1):
            self._check_budget(deadline)
            addresses = self._resolve_public_addresses(current_url, deadline)
            response = self._request(current_url, addresses, deadline)
            try:
                self._check_budget(deadline)
                if response.status_code in self._REDIRECT_STATUSES:
                    current_url = self._redirect_target(response, current_url, redirect_count)
                    continue
                if not 200 <= response.status_code < 300:
                    raise ToolError(f"Web fetch failed with HTTP status {response.status_code}.")

                content_type, declared_encoding = self._content_metadata(response.headers)
                if content_type not in self._ALLOWED_CONTENT_TYPES:
                    allowed = ", ".join(sorted(self._ALLOWED_CONTENT_TYPES))
                    raise ToolError(f"Unsupported content type {content_type or 'missing'}; allowed types: {allowed}.")
                body = self._read_limited_body(response, current_url, deadline)
                title, readable = self._extract_content(
                    body,
                    content_type,
                    final_url=current_url,
                    declared_encoding=declared_encoding,
                )
                self._check_budget(deadline)
                readable = self._truncate_output(readable or self._EMPTY_CONTENT, max_chars)
                header = f"Fetched URL: {current_url}\nContent type: {content_type}"
                if title:
                    header += f"\nTitle: {title}"
                return f"{header}\n\nUntrusted external content:\n{readable}"
            finally:
                self._close_response(response)
        raise ToolError(f"Web fetch exceeded the redirect limit of {self._MAX_REDIRECTS}.")

    def _request(self, url: str, addresses: tuple[str, ...], deadline: float) -> HttpResponse:
        remaining = self._remaining_budget(deadline)
        try:
            return self._transport.request(
                url,
                addresses,
                connect_timeout=min(self._CONNECT_TIMEOUT_SECONDS, remaining),
                read_timeout=min(self._READ_TIMEOUT_SECONDS, remaining),
                deadline=deadline,
            )
        except Exception as exc:
            raise ToolError(f"Unable to fetch URL {url}: {exc}") from exc

    def _redirect_target(self, response: HttpResponse, current_url: str, redirect_count: int) -> str:
        if redirect_count >= self._MAX_REDIRECTS:
            raise ToolError(f"Web fetch exceeded the redirect limit of {self._MAX_REDIRECTS}.")
        location = self._header(response.headers, "location")
        if not location:
            raise ToolError("Redirect response did not include a Location header.")
        return self._normalise_url(urljoin(current_url, location))

    def _resolve_public_addresses(self, url: str, deadline: float) -> tuple[str, ...]:
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = self._resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ToolError(f"Unable to resolve web host: {parsed.hostname}.") from exc
        except OSError as exc:
            raise ToolError(f"Unable to resolve web host: {parsed.hostname}.") from exc
        self._check_budget(deadline)
        if not addresses:
            raise ToolError(f"Web host resolved without addresses: {parsed.hostname}.")
        validated: list[str] = []
        for address_info in addresses:
            try:
                address = str(address_info[4][0]).split("%", 1)[0]
                ip = ipaddress.ip_address(address)
            except (IndexError, ValueError) as exc:
                raise ToolError(f"Web host resolved to an invalid address: {parsed.hostname}.") from exc
            if not ip.is_global:
                raise ToolError("Web fetch refuses loopback, private, link-local, or reserved network addresses.")
            normalised = str(ip)
            if normalised not in validated:
                validated.append(normalised)
        return tuple(validated)

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
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ToolError("url includes an invalid host.") from exc
        if any(character.isspace() or ord(character) < 32 for character in hostname):
            raise ToolError("url includes an invalid host.")
        host = f"[{hostname}]" if ":" in hostname else hostname
        netloc = f"{host}:{port}" if port is not None else host
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))

    def _read_limited_body(self, response: HttpResponse, url: str, deadline: float) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            iterator = iter(response.iter_content(chunk_size=8192))
            while True:
                self._check_budget(deadline)
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                self._check_budget(deadline)
                if not chunk:
                    continue
                chunk_bytes = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                total += len(chunk_bytes)
                if total > self._MAX_RESPONSE_BYTES:
                    raise ToolError(f"Web response exceeds the {self._MAX_RESPONSE_BYTES}-byte limit.")
                chunks.append(chunk_bytes)
                self._check_budget(deadline)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Unable to read web response from {url}: {exc}") from exc
        return b"".join(chunks)

    def _extract_content(
        self,
        body: bytes,
        content_type: str,
        *,
        final_url: str,
        declared_encoding: str | None,
    ) -> tuple[str | None, str]:
        if content_type == "text/html":
            document = extract_html_document(
                body,
                base_url=final_url,
                declared_encoding=self._valid_encoding(declared_encoding),
            )
            return document.title, document.markdown
        if content_type == "application/json":
            try:
                text = body.decode("utf-8-sig")
                return None, json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError, OverflowError) as exc:
                raise ToolError("Web response contained invalid JSON.") from exc
        encoding = self._valid_encoding(declared_encoding) or "utf-8"
        try:
            return None, self._normalise_whitespace(body.decode(encoding, errors="replace"))
        except (LookupError, UnicodeError) as exc:
            raise ToolError("Unable to decode plain-text web response.") from exc

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
    def _content_metadata(cls, headers: Mapping[str, str]) -> tuple[str, str | None]:
        raw = cls._header(headers, "content-type")
        if not raw:
            return "", None
        content_type = raw.split(";", 1)[0].strip().lower()
        match = re.search(r"(?:^|;)\s*charset\s*=\s*(?:\"([^\"]*)\"|([^;\s]*))", raw, flags=re.IGNORECASE)
        declared_encoding = next((group.strip() for group in match.groups() if group), None) if match else None
        return content_type, declared_encoding

    @staticmethod
    def _valid_encoding(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return codecs.lookup(value).name
        except (LookupError, ValueError):
            return None

    @staticmethod
    def _normalise_whitespace(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _truncate_output(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        for prefix_length in range(min(len(value) - 1, max_chars), -1, -1):
            omitted = len(value) - prefix_length
            marker = f"… output truncated ({omitted} characters omitted)"
            separator = "\n\n" if prefix_length else ""
            candidate = f"{value[:prefix_length]}{separator}{marker}"
            if len(candidate) <= max_chars:
                return candidate
        return "…"[:max_chars]

    def _remaining_budget(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise ToolError(f"Web fetch exceeded the {int(self._TOTAL_TIMEOUT_SECONDS)}-second time budget.")
        return remaining

    def _check_budget(self, deadline: float) -> None:
        self._remaining_budget(deadline)

    @staticmethod
    def _close_response(response: HttpResponse) -> None:
        try:
            response.close()
        except Exception:
            pass
