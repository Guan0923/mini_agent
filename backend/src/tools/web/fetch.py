"""Public-only HTTP fetcher with bounded response handling."""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import requests
import urllib3

from ..base import ToolError
from .html import ReadableHtmlParser
from .protocols import HostResolver, HttpResponse, HttpSession
from .text import normalize_whitespace


class _PinnedResponse:
    """Adapt one urllib3 response to the project's injected HTTP protocol."""

    def __init__(self, response: urllib3.response.HTTPResponse) -> None:
        self._response = response
        self.raw = response
        self.status_code = int(response.status)
        self.headers = dict(response.headers)
        self.encoding = self._encoding_from_headers(self.headers)

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False):
        del decode_unicode
        yield from self._response.stream(amt=chunk_size)

    def close(self) -> None:
        self._response.release_conn()
        self._response.close()

    @staticmethod
    def _encoding_from_headers(headers: Mapping[str, str]) -> str | None:
        for key, value in headers.items():
            if key.lower() != "content-type":
                continue
            for part in value.split(";")[1:]:
                name, separator, encoding = part.strip().partition("=")
                if separator and name.lower() == "charset" and encoding.strip():
                    return encoding.strip().strip("\"'")
        return None


class _PinnedHttpTransport:
    """Make one request to an already-approved IP while preserving TLS SNI."""

    def get(self, url: str, address: str, *, headers: Mapping[str, str], timeout: tuple[int, int]) -> HttpResponse:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if hostname is None:
            raise ToolError("url must include a host.")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        request_headers = {**headers, "Host": parsed.netloc}
        timeout_value = urllib3.Timeout(connect=timeout[0], read=timeout[1])
        pool_kwargs: dict[str, Any] = {}
        pool_type: type[urllib3.HTTPConnectionPool] = urllib3.HTTPConnectionPool
        if parsed.scheme == "https":
            pool_type = urllib3.HTTPSConnectionPool
            pool_kwargs.update(
                {
                    "cert_reqs": "CERT_REQUIRED",
                    "ca_certs": requests.certs.where(),
                    "assert_hostname": hostname,
                    "server_hostname": hostname,
                }
            )
        pool = pool_type(address, port=port, **pool_kwargs)
        try:
            response = pool.urlopen(
                "GET",
                path,
                headers=request_headers,
                redirect=False,
                preload_content=False,
                retries=False,
                timeout=timeout_value,
                assert_same_host=False,
            )
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            raise ToolError(f"Unable to fetch URL: {exc}") from exc
        return _PinnedResponse(response)


class SafeWebFetcher:
    """Fetch limited public HTTP content while applying SSRF protections.

    A normal DNS answer is validated and then pinned into the connection.  In
    networks that use Clash/TUN fake DNS, only the explicit RFC 2544 benchmark
    range is eligible for a fixed-IP DNS-over-HTTPS fallback.  Private,
    loopback, link-local and reserved answers remain rejected.
    """

    _MAX_REDIRECTS = 3
    _MAX_RESPONSE_BYTES = 2_000_000
    _MAX_OUTPUT_CHARS = 100_000
    _DEFAULT_OUTPUT_CHARS = 50_000
    _ALLOWED_CONTENT_TYPES = {"text/html", "text/plain", "application/json"}
    _REDIRECT_STATUSES = {301, 302, 303, 307, 308}
    _USER_AGENT = "Mini-Agent/0.1 (+https://example.invalid/mini-agent)"
    _DOH_ADDRESS = "1.1.1.1"
    _DOH_HOST = "cloudflare-dns.com"
    _SYNTHETIC_NETWORK = ipaddress.ip_network("198.18.0.0/15")

    def __init__(
        self,
        *,
        session: HttpSession | None = None,
        resolver: HostResolver = socket.getaddrinfo,
        doh_resolver: Callable[[str], list[str]] | None = None,
        allow_private_network: bool = False,
    ) -> None:
        self._resolver = resolver
        self._doh_resolver = doh_resolver or self._resolve_with_doh
        self._allow_private_network = allow_private_network
        self._transport = _PinnedHttpTransport()
        if session is None:
            self._session: HttpSession | None = None
        else:
            self._session = session

    def fetch(self, url: str, max_chars: int = _DEFAULT_OUTPUT_CHARS) -> str:
        """Fetch a public HTML, text, or JSON resource as limited readable text."""
        self._validate_max_chars(max_chars)
        current_url = self._normalise_url(url)
        for redirect_count in range(self._MAX_REDIRECTS + 1):
            approved_addresses = self._assert_public_target(current_url)
            response = self._request(current_url, approved_addresses)
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
                try:
                    body = self._read_limited_body(response)
                except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
                    raise ToolError(f"Unable to read web response: {exc}") from exc
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

    def _request(self, url: str, addresses: list[str]) -> HttpResponse:
        headers = {"Accept": "text/html, text/plain, application/json", "User-Agent": self._USER_AGENT}
        if self._session is not None:
            try:
                return self._session.get(
                    url,
                    headers=headers,
                    allow_redirects=False,
                    stream=True,
                    timeout=(5, 15),
                )
            except requests.RequestException as exc:
                raise ToolError(f"Unable to fetch URL: {exc}") from exc
            except OSError as exc:
                raise ToolError(f"Unable to fetch URL: {exc}") from exc
        return self._transport.get(url, self._preferred_address(addresses), headers=headers, timeout=(5, 15))

    def _resolve_with_doh(self, host: str) -> list[str]:
        """Resolve through a fixed public DoH address with an explicit SNI."""

        addresses: list[str] = []
        for record_type in ("A", "AAAA"):
            response = None
            try:
                query = urlencode({"name": host, "type": record_type})
                response = self._transport.get(
                    f"https://{self._DOH_HOST}/dns-query?{query}",
                    self._DOH_ADDRESS,
                    headers={"Accept": "application/dns-json", "Host": self._DOH_HOST},
                    timeout=(5, 15),
                )
                self._verify_connected_peer(response)
                if not 200 <= response.status_code < 300:
                    raise ToolError(f"Fixed DNS resolver returned HTTP status {response.status_code}.")
                raw = self._read_limited_body(response)
                payload = json.loads(raw.decode(response.encoding or "utf-8", errors="replace"))
            except (ToolError, ValueError, OSError) as exc:
                raise ToolError(f"Unable to resolve web host through the fixed DNS resolver: {exc}") from exc
            finally:
                if response is not None and callable(getattr(response, "close", None)):
                    response.close()

            answers = payload.get("Answer", []) if isinstance(payload, dict) else []
            for answer in answers if isinstance(answers, list) else []:
                if not isinstance(answer, dict) or answer.get("type") not in {1, 28}:
                    continue
                value = answer.get("data")
                try:
                    address = ipaddress.ip_address(str(value))
                except ValueError:
                    continue
                if not address.is_global:
                    raise ToolError("Web host resolved through the fixed DNS resolver to a non-public address.")
                value = str(address)
                if value not in addresses:
                    addresses.append(value)
        if addresses:
            return addresses
        raise ToolError(f"Web host resolved without public addresses: {host}.")

    def _assert_public_target(self, url: str) -> list[str]:
        parsed = urlsplit(url)
        assert parsed.hostname is not None
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if self._allow_private_network:
            return self._resolve_all_addresses(host, port, allow_non_public=True)
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise ToolError("Web fetch refuses loopback, private, link-local, or reserved network addresses.")
        try:
            addresses_info = self._resolver(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ToolError(f"Unable to resolve web host: {host}.") from exc
        except OSError as exc:
            raise ToolError(f"Unable to resolve web host: {host}.") from exc
        if not addresses_info:
            raise ToolError(f"Web host resolved without addresses: {host}.")

        addresses: list[str] = []
        for address_info in addresses_info:
            try:
                address = str(address_info[4][0]).split("%", 1)[0]
                parsed_address = ipaddress.ip_address(address)
            except (IndexError, ValueError) as exc:
                raise ToolError(f"Web host resolved to an invalid address: {host}.") from exc
            addresses.append(str(parsed_address))

        if all(ipaddress.ip_address(address).is_global for address in addresses):
            return addresses
        if all(not ipaddress.ip_address(address).is_global for address in addresses) and any(
            ipaddress.ip_address(address) in self._SYNTHETIC_NETWORK for address in addresses
        ):
            resolved = self._doh_resolver(host)
            if not resolved or not all(ipaddress.ip_address(address).is_global for address in resolved):
                raise ToolError("Web host did not resolve to a verified public address.")
            return resolved
        raise ToolError("Web fetch refuses loopback, private, link-local, or reserved network addresses.")

    def _resolve_all_addresses(self, host: str, port: int, *, allow_non_public: bool = False) -> list[str]:
        try:
            addresses_info = self._resolver(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError) as exc:
            raise ToolError(f"Unable to resolve web host: {host}.") from exc
        addresses: list[str] = []
        for address_info in addresses_info or []:
            try:
                address = str(address_info[4][0]).split("%", 1)[0]
                parsed_address = ipaddress.ip_address(address)
            except (IndexError, ValueError) as exc:
                raise ToolError(f"Web host resolved to an invalid address: {host}.") from exc
            if not parsed_address.is_global and not allow_non_public:
                raise ToolError("Restricted web access refuses loopback, private, link-local, or reserved addresses.")
            value = str(parsed_address)
            if value not in addresses:
                addresses.append(value)
        if not addresses:
            raise ToolError(f"Web host resolved without addresses: {host}.")
        return addresses

    @staticmethod
    def _preferred_address(addresses: list[str]) -> str:
        if not addresses:
            raise ToolError("Web host has no approved public address.")
        for address in addresses:
            if ipaddress.ip_address(address).version == 4:
                return address
        return addresses[0]

    def _verify_connected_peer(self, response: HttpResponse) -> None:
        """Reject a response whose real connection reached a non-public peer."""
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
        if not ip.is_global and not self._allow_private_network:
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
    def _normalise_url(value: Any) -> str:
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
    def _truncate_output(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        omitted = len(value) - max_chars
        return f"{value[:max_chars]}\n\n… output truncated ({omitted} characters omitted)"

    @staticmethod
    def _normalise_whitespace(value: str) -> str:
        return normalize_whitespace(value)
