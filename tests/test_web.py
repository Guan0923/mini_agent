import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from backend.planning import RuleBasedPlanner
from backend.runtime import AgentRunner
from backend.tools import ConfirmationRequired, DdgrWebSearch, SafeWebFetcher, ToolError, ToolRegistry


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


def test_ddgr_search_uses_noninteractive_json_and_formats_results() -> None:
    calls = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                [
                    {
                        "title": "Python docs",
                        "url": "https://docs.python.org/",
                        "abstract": "The official Python documentation.",
                    }
                ]
            ),
            stderr="",
        )

    output = DdgrWebSearch(runner=runner).search("Python documentation", max_results=3)

    assert "Python docs" in output
    assert "https://docs.python.org/" in output
    assert calls == [
        (
            ["ddgr", "--json", "--np", "-n", "3", "Python documentation"],
            {
                "capture_output": True,
                "check": False,
                "encoding": "utf-8",
                "errors": "replace",
                "text": True,
                "timeout": 15,
                "shell": False,
            },
        )
    ]


def test_ddgr_search_rejects_invalid_input_and_bad_json() -> None:
    search = DdgrWebSearch(
        runner=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="not json", stderr="")
    )

    with pytest.raises(ToolError, match="query"):
        search.search("")
    with pytest.raises(ToolError, match="max_results"):
        search.search("python", max_results=0)
    with pytest.raises(ToolError, match="invalid JSON"):
        search.search("python")


def test_web_search_requires_confirmation_when_registered(tmp_path: Path) -> None:
    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    tools = ToolRegistry(tmp_path, web_search=DdgrWebSearch(runner=runner))

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


from backend.tools.web.html import ReadableHtmlParser


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
