import socket
from pathlib import Path
from typing import Any

import pytest

from backend.planning import RuleBasedPlanner
from backend.runtime import AgentRunner
from backend.tools import ConfirmationRequired, DdgrWebSearch, SafeWebFetcher, ToolError, ToolRegistry
from backend.tools.web.html import ReadableHtmlParser


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: bytes = b"",
        encoding: str | None = "utf-8",
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.encoding = encoding
        self.closed = False

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False):
        del decode_unicode
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index : index + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def public_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
    del host, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def test_ddgr_search_uses_html_endpoint_and_formats_results() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {"Content-Type": "text/html"},
                b'<a class="result__a" href="https://docs.python.org/">Python docs</a>'
                b'<div class="result__snippet">The official Python documentation.</div>',
            )
        ]
    )

    output = DdgrWebSearch(session=session).search("Python documentation", max_results=3)

    assert "Python docs" in output
    assert "https://docs.python.org/" in output
    assert session.calls[0][0] == "https://html.duckduckgo.com/html/"
    assert session.calls[0][1]["params"] == {"q": "Python documentation"}


def test_ddgr_search_rejects_invalid_input_and_bad_pages() -> None:
    search = DdgrWebSearch(session=FakeSession([FakeResponse(200, {"Content-Type": "text/html"}, b"not html")]))

    with pytest.raises(ToolError, match="query"):
        search.search("")
    with pytest.raises(ToolError, match="max_results"):
        search.search("python", max_results=0)
    with pytest.raises(ToolError, match="unrecognizable"):
        search.search("python")


def test_ddgr_search_reports_http_202_instead_of_empty_results() -> None:
    search = DdgrWebSearch(session=FakeSession([FakeResponse(202, {}, b"Accepted")]))

    with pytest.raises(ToolError, match="HTTP status 202"):
        search.search("python")


def test_web_search_requires_confirmation_when_registered(tmp_path: Path) -> None:
    session = FakeSession(
        [FakeResponse(200, {"Content-Type": "text/html"}, b'<div class="no-results">No results</div>')]
    )
    tools = ToolRegistry(tmp_path, web_search=DdgrWebSearch(session=session))

    with pytest.raises(ConfirmationRequired):
        tools.invoke("web_search", {"query": "Python"})
    assert tools.invoke("web_search", {"query": "Python"}, confirmed=True) == "No web search results found."
    assert {"web_search", "web_fetch"}.issubset(tools.names())

    with pytest.raises(ConfirmationRequired):
        tools.invoke("web_fetch", {"url": "https://example.com/"})


def test_web_fetch_extracts_static_html_with_safety_limits() -> None:
    response = FakeResponse(
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        b"<html><head><title>Example</title><script>ignore()</script></head>"
        b"<body><nav>Navigation</nav><main><h1>Visible heading</h1><p>Useful text.</p></main></body></html>",
    )
    session = FakeSession([response])
    output = SafeWebFetcher(session=session, resolver=public_resolver).fetch("https://example.com/docs")

    assert "Fetched URL: https://example.com/docs" in output
    assert "Title: Example" in output
    assert "Visible heading Useful text." in output
    assert "Navigation" not in output
    assert "ignore()" not in output
    assert response.closed is True
    assert session.calls == [
        (
            "https://example.com/docs",
            {
                "headers": {
                    "Accept": "text/html, text/plain, application/json",
                    "User-Agent": "Mini-Agent/0.1 (+https://example.invalid/mini-agent)",
                },
                "allow_redirects": False,
                "stream": True,
                "timeout": (5, 15),
            },
        )
    ]


def test_web_fetch_blocks_non_public_addresses_before_request() -> None:
    session = FakeSession([])

    def private_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    with pytest.raises(ToolError, match="refuses"):
        SafeWebFetcher(session=session, resolver=private_resolver).fetch("http://localhost/secret")
    assert session.calls == []


def test_restricted_web_fetch_rejects_private_resolution_before_request() -> None:
    session = FakeSession([])

    def private_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port))]

    fetcher = SafeWebFetcher(
        session=session,
        resolver=private_resolver,
        network_mode="restricted_network",
        network_allowlist=(("internal.example", 443),),
    )
    with pytest.raises(ToolError, match="Restricted web access"):
        fetcher.fetch("https://internal.example/secret")
    assert session.calls == []


def test_restricted_web_search_pins_public_resolution() -> None:
    calls: list[tuple[str, str]] = []

    def public_search_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    class CapturingTransport:
        def get(self, url: str, address: str, **kwargs: Any) -> FakeResponse:
            del kwargs
            calls.append((url, address))
            return FakeResponse(
                200,
                {"Content-Type": "text/html"},
                b'<a class="result__a" href="https://docs.python.org/">Python docs</a>',
            )

    search = DdgrWebSearch(
        network_mode="restricted_network",
        network_allowlist=(("html.duckduckgo.com", 443),),
        resolver=public_search_resolver,
    )
    search._transport = CapturingTransport()  # type: ignore[assignment]
    assert "Python docs" in search.search("Python")
    assert calls == [("https://html.duckduckgo.com/html/?q=Python", "93.184.216.34")]


def test_restricted_web_search_rejects_private_resolution_before_request() -> None:
    def private_search_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    search = DdgrWebSearch(
        session=FakeSession([]),
        network_mode="restricted_network",
        network_allowlist=(("html.duckduckgo.com", 443),),
        resolver=private_search_resolver,
    )
    with pytest.raises(ToolError, match="Restricted web search"):
        search.search("Python")


def test_web_fetch_pins_a_verified_public_address_after_synthetic_dns() -> None:
    calls: list[tuple[str, str]] = []

    def synthetic_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.222", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fdfe:dcba:9876::63", port, 0, 0)),
        ]

    class CapturingTransport:
        def get(self, url: str, address: str, **kwargs: Any) -> FakeResponse:
            del kwargs
            calls.append((url, address))
            return FakeResponse(200, {"Content-Type": "text/plain"}, b"verified content")

    fetcher = SafeWebFetcher(
        resolver=synthetic_resolver,
        doh_resolver=lambda host: ["93.184.216.34"],
    )
    fetcher._transport = CapturingTransport()  # type: ignore[assignment]

    assert "verified content" in fetcher.fetch("https://example.com/")
    assert calls == [("https://example.com/", "93.184.216.34")]


def test_web_fetch_rejects_private_address_in_any_fixed_doh_answer() -> None:
    def synthetic_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.222", port))]

    class DohTransport:
        def get(self, url: str, address: str, **kwargs: Any) -> FakeResponse:
            del kwargs
            assert address == "1.1.1.1"
            assert "cloudflare-dns.com/dns-query" in url
            if url.endswith("type=A"):
                body = b'{"Answer":[{"type":1,"data":"93.184.216.34"}]}'
            else:
                body = b'{"Answer":[{"type":28,"data":"fd00::1"}]}'
            return FakeResponse(200, {"Content-Type": "application/dns-json"}, body)

    fetcher = SafeWebFetcher(resolver=synthetic_resolver)
    fetcher._transport = DohTransport()  # type: ignore[assignment]

    with pytest.raises(ToolError, match="non-public"):
        fetcher._assert_public_target("https://example.com/")


def test_web_fetch_reports_fixed_doh_failures() -> None:
    def synthetic_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.222", port))]

    class FailedTransport:
        def get(self, url: str, address: str, **kwargs: Any) -> FakeResponse:
            del url, address, kwargs
            raise ToolError("DNS endpoint unavailable")

    fetcher = SafeWebFetcher(resolver=synthetic_resolver)
    fetcher._transport = FailedTransport()  # type: ignore[assignment]

    with pytest.raises(ToolError, match="Unable to resolve web host"):
        fetcher._assert_public_target("https://example.com/")


def test_web_fetch_revalidates_redirect_targets() -> None:
    session = FakeSession(
        [
            FakeResponse(302, {"Location": "/guide"}),
            FakeResponse(200, {"Content-Type": "text/plain"}, b"Guide content"),
        ]
    )
    output = SafeWebFetcher(session=session, resolver=public_resolver).fetch("https://example.com/start")

    assert "Fetched URL: https://example.com/guide" in output
    assert [url for url, _kwargs in session.calls] == ["https://example.com/start", "https://example.com/guide"]


def test_web_fetch_blocks_private_redirect_before_a_second_request() -> None:
    session = FakeSession([FakeResponse(302, {"Location": "http://localhost/internal"})])
    addresses = iter(["93.184.216.34", "127.0.0.1"])

    def changing_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(addresses), port))]

    with pytest.raises(ToolError, match="refuses"):
        SafeWebFetcher(session=session, resolver=changing_resolver).fetch("https://example.com/start")
    assert [url for url, _kwargs in session.calls] == ["https://example.com/start"]


def test_web_fetch_rejects_unsupported_content_and_invalid_url() -> None:
    session = FakeSession([FakeResponse(200, {"Content-Type": "application/pdf"})])
    fetcher = SafeWebFetcher(session=session, resolver=public_resolver)

    with pytest.raises(ToolError, match="Unsupported content type"):
        fetcher.fetch("https://example.com/report.pdf")
    with pytest.raises(ToolError, match="http or https"):
        fetcher.fetch("file:///secret.txt")


def test_rule_planner_generates_web_tool_calls() -> None:
    planner = RuleBasedPlanner()
    runner = AgentRunner(planner, ToolRegistry())

    search = planner.decide(runner.new_runtime(task="search Python docs")).tool_messages[0]
    fetch = planner.decide(runner.new_runtime(task="抓取网页 https://example.com/docs")).tool_messages[0]

    assert (search.name, search.arguments) == ("web_search", {"query": "Python docs"})
    assert (fetch.name, fetch.arguments) == ("web_fetch", {"url": "https://example.com/docs"})


class _FakeSocket:
    def __init__(self, peer: tuple[str, int]) -> None:
        self._peer = peer

    def getpeername(self) -> tuple[str, int]:
        return self._peer


class _FakeConnection:
    def __init__(self, sock: _FakeSocket) -> None:
        self.sock = sock


class _FakeRaw:
    def __init__(self, sock: _FakeSocket) -> None:
        self._connection = _FakeConnection(sock)


def test_html_parser_recovers_after_unclosed_ignored_tag() -> None:
    parser = ReadableHtmlParser()
    parser.feed("<p>before</p><nav>hidden<p>lost</p><p>after</p>")
    parser.close()

    assert "before" in parser.text
    assert "after" in parser.text
    assert "hidden" not in parser.text
    assert "lost" not in parser.text


def test_html_parser_unclosed_script_swallows_to_end_of_input() -> None:
    parser = ReadableHtmlParser()
    parser.feed("<p>kept</p><script>js code</p><p>dropped</p>")
    parser.close()

    assert "kept" in parser.text
    assert "dropped" not in parser.text
    assert "js code" not in parser.text


def test_web_fetch_rejects_response_connected_to_non_public_peer() -> None:
    response = FakeResponse(200, {"Content-Type": "text/plain"}, b"internal secret")
    response.raw = _FakeRaw(_FakeSocket(("10.0.0.5", 443)))
    session = FakeSession([response])

    with pytest.raises(ToolError, match="non-public address"):
        SafeWebFetcher(session=session, resolver=public_resolver).fetch("https://example.com/")


def test_web_fetch_accepts_response_connected_to_public_peer() -> None:
    response = FakeResponse(200, {"Content-Type": "text/plain"}, b"public content")
    response.raw = _FakeRaw(_FakeSocket(("93.184.216.34", 443)))
    session = FakeSession([response])

    output = SafeWebFetcher(session=session, resolver=public_resolver).fetch("https://example.com/")

    assert "public content" in output
