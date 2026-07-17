import json
import socket
import ssl
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mini_agent.planning import RuleBasedPlanner
from mini_agent.runtime import AgentRunner
from mini_agent.runtime.contracts import InterruptDecision
from mini_agent.tools import ConfirmationRequired, DdgrWebSearch, SafeWebFetcher, ToolError, ToolRegistry
from mini_agent.tools.web import PinnedHttpTransport


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: bytes = b"",
        *,
        chunks: list[bytes | Exception | Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False):
        del decode_unicode
        if self.chunks is not None:
            for chunk in self.chunks:
                if isinstance(chunk, Exception):
                    raise chunk
                if callable(chunk):
                    chunk = chunk()
                yield chunk
            return
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index : index + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, results: list[FakeResponse | Exception | Any]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    def request(self, url: str, addresses: tuple[str, ...], **kwargs: Any) -> FakeResponse:
        call = {"url": url, "addresses": addresses, **kwargs}
        self.calls.append(call)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(call)
        return result


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def public_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
    del host, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def body_segment(output: str) -> str:
    return output.split("Untrusted external content:\n", 1)[1]


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


def test_web_tools_require_confirmation_and_fetch_is_not_retryable(tmp_path: Path) -> None:
    tools = ToolRegistry(
        tmp_path,
        web_search=DdgrWebSearch(
            runner=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
        ),
    )

    with pytest.raises(ConfirmationRequired):
        tools.invoke("web_search", {"query": "Python"})
    with pytest.raises(ConfirmationRequired):
        tools.invoke("web_fetch", {"url": "https://example.com/"})
    assert tools.is_retryable("web_search") is True
    assert tools.is_retryable("web_fetch") is False


def test_web_fetch_returns_structured_markdown_and_uses_final_url_for_links() -> None:
    redirect = FakeResponse(302, {"Location": "/guide/page"})
    response = FakeResponse(
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        b"<html><head><title>Example</title></head><body><nav>Navigation</nav>"
        b"<h1>Visible heading</h1><p><a href='../next'>Next</a></p></body></html>",
    )
    transport = FakeTransport([redirect, response])

    output = SafeWebFetcher(transport=transport, resolver=public_resolver).fetch("https://example.com/start")

    assert "Fetched URL: https://example.com/guide/page" in output
    assert "Title: Example" in output
    assert "# Visible heading" in output
    assert "[Next](https://example.com/next)" in output
    assert "Navigation" in output
    assert redirect.closed is True
    assert response.closed is True


def test_web_fetch_handles_html_meta_and_invalid_http_charset() -> None:
    response = FakeResponse(
        200,
        {"Content-Type": "text/html; charset=not-a-codec"},
        b'<meta charset="windows-1252"><body><p>Caf\xe9</p></body>',
    )

    output = SafeWebFetcher(transport=FakeTransport([response]), resolver=public_resolver).fetch("https://example.com/")

    assert "Caf\u00e9" in output


@pytest.mark.parametrize("charset", ["invalid", "\x00"])
def test_web_fetch_plain_text_invalid_charset_falls_back_to_utf8_with_replacement(charset: str) -> None:
    response = FakeResponse(200, {"Content-Type": f"text/plain; charset={charset}"}, b"Good \xff text")

    output = SafeWebFetcher(transport=FakeTransport([response]), resolver=public_resolver).fetch("https://example.com/")

    assert "Good \ufffd text" in output


def test_web_fetch_limits_streamed_decompressed_body_size() -> None:
    response = FakeResponse(200, {"Content-Type": "text/plain"}, b"x" * 2_000_001)

    with pytest.raises(ToolError, match="2000000-byte limit"):
        SafeWebFetcher(transport=FakeTransport([response]), resolver=public_resolver).fetch("https://example.com/")
    assert response.closed is True


@pytest.mark.parametrize("body", [b"not json", b"[" * 1_100 + b"0" + b"]" * 1_100])
def test_web_fetch_rejects_malformed_or_too_deep_json(body: bytes) -> None:
    response = FakeResponse(200, {"Content-Type": "application/json"}, body)

    with pytest.raises(ToolError, match="invalid JSON"):
        SafeWebFetcher(transport=FakeTransport([response]), resolver=public_resolver).fetch("https://example.com/data")
    assert response.closed is True


def test_web_fetch_preserves_empty_body_semantics() -> None:
    response = FakeResponse(200, {"Content-Type": "text/html"}, b"<body><script>ignored()</script></body>")

    output = SafeWebFetcher(transport=FakeTransport([response]), resolver=public_resolver).fetch("https://example.com/")

    assert body_segment(output) == "(The page had no readable text.)"


@pytest.mark.parametrize("max_chars", [1, 2, 10, 39, 40, 50, 100])
def test_web_fetch_body_segment_never_exceeds_max_chars(max_chars: int) -> None:
    response = FakeResponse(200, {"Content-Type": "text/plain"}, b"x" * 500)

    output = SafeWebFetcher(transport=FakeTransport([response]), resolver=public_resolver).fetch(
        "https://example.com/", max_chars=max_chars
    )

    assert len(body_segment(output)) <= max_chars
    if max_chars < len("\u2026 output truncated (500 characters omitted)"):
        assert body_segment(output) == "\u2026"


def test_web_fetch_rejects_unsupported_content_and_invalid_url() -> None:
    response = FakeResponse(200, {"Content-Type": "application/pdf"})
    fetcher = SafeWebFetcher(transport=FakeTransport([response]), resolver=public_resolver)

    with pytest.raises(ToolError, match="Unsupported content type"):
        fetcher.fetch("https://example.com/report.pdf")
    with pytest.raises(ToolError, match="http or https"):
        fetcher.fetch("file:///secret.txt")
    assert response.closed is True


def test_web_fetch_blocks_private_and_mixed_dns_results_before_transport() -> None:
    transport = FakeTransport([])

    def mixed_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    with pytest.raises(ToolError, match="refuses"):
        SafeWebFetcher(transport=transport, resolver=mixed_resolver).fetch("https://example.com/")
    assert transport.calls == []


def test_web_fetch_deduplicates_public_ipv4_and_ipv6_dns_results() -> None:
    response = FakeResponse(200, {"Content-Type": "text/plain"}, b"ok")
    transport = FakeTransport([response])

    def resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, kwargs
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", port, 0, 0)),
        ]

    SafeWebFetcher(transport=transport, resolver=resolver).fetch("https://example.com/")

    assert transport.calls[0]["addresses"] == ("93.184.216.34", "2606:4700:4700::1111")


def test_web_fetch_revalidates_and_blocks_a_private_redirect() -> None:
    redirect = FakeResponse(302, {"Location": "http://localhost/internal"})
    transport = FakeTransport([redirect])

    def resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        del kwargs
        address = "127.0.0.1" if host == "localhost" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    with pytest.raises(ToolError, match="refuses"):
        SafeWebFetcher(transport=transport, resolver=resolver).fetch("https://example.com/start")
    assert len(transport.calls) == 1
    assert redirect.closed is True


def test_pinned_http_transport_connects_to_validated_ip_and_preserves_host(monkeypatch: pytest.MonkeyPatch) -> None:
    pools = []

    class RawResponse:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def stream(self, chunk_size: int, decode_content: bool = True):
            del chunk_size, decode_content
            yield b"ok"

        def close(self) -> None:
            self.closed = True

    class Pool:
        def __init__(self, host: str, **kwargs: Any) -> None:
            self.host = host
            self.kwargs = kwargs
            self.calls = []
            self.closed = False
            pools.append(self)

        def urlopen(self, method: str, target: str, **kwargs: Any):
            self.calls.append((method, target, kwargs))
            return RawResponse()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("mini_agent.tools.web.HTTPConnectionPool", Pool)
    response = PinnedHttpTransport(clock=lambda: 0.0).request(
        "http://example.com:443/path?q=1",
        ("93.184.216.34",),
        connect_timeout=5,
        read_timeout=15,
        deadline=30,
    )

    pool = pools[0]
    method, target, kwargs = pool.calls[0]
    assert pool.host == "93.184.216.34"
    assert pool.kwargs == {"port": 443, "retries": False}
    assert (method, target) == ("GET", "/path?q=1")
    assert kwargs["headers"]["Host"] == "example.com:443"
    assert kwargs["redirect"] is False and kwargs["retries"] is False
    response.close()
    assert pool.closed is True


def test_pinned_https_transport_uses_original_host_for_sni_and_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = {}

    class Pool:
        def __init__(self, host: str, **kwargs: Any) -> None:
            observed["host"] = host
            observed["pool_kwargs"] = kwargs

        def urlopen(self, method: str, target: str, **kwargs: Any):
            observed["request"] = (method, target, kwargs)
            return type(
                "RawResponse",
                (),
                {
                    "status": 200,
                    "headers": {},
                    "stream": lambda self, *args, **kwargs: iter(()),
                    "close": lambda self: None,
                },
            )()

        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setattr("mini_agent.tools.web.HTTPSConnectionPool", Pool)
    response = PinnedHttpTransport(clock=lambda: 0.0).request(
        "https://example.com/path",
        ("2606:4700:4700::1111",),
        connect_timeout=5,
        read_timeout=15,
        deadline=30,
    )

    assert observed["host"] == "2606:4700:4700::1111"
    assert observed["pool_kwargs"]["server_hostname"] == "example.com"
    assert observed["pool_kwargs"]["assert_hostname"] == "example.com"
    assert observed["pool_kwargs"]["cert_reqs"] == ssl.CERT_REQUIRED
    assert observed["request"][2]["headers"]["Host"] == "example.com"
    response.close()


def test_pinned_transport_closes_each_failed_ip_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pools = []

    class Pool:
        def __init__(self, host: str, **kwargs: Any) -> None:
            del kwargs
            self.host = host
            self.closed = False
            pools.append(self)

        def urlopen(self, *args: Any, **kwargs: Any):
            del args, kwargs
            raise OSError(f"cannot connect to {self.host}")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("mini_agent.tools.web.HTTPConnectionPool", Pool)

    with pytest.raises(OSError, match="cannot connect"):
        PinnedHttpTransport(clock=lambda: 0.0).request(
            "http://example.com/",
            ("93.184.216.34", "93.184.216.35"),
            connect_timeout=5,
            read_timeout=15,
            deadline=30,
        )
    assert [pool.host for pool in pools] == ["93.184.216.34", "93.184.216.35"]
    assert all(pool.closed for pool in pools)


def test_web_fetch_wraps_connection_and_read_failures_and_closes_responses() -> None:
    connection_transport = FakeTransport([TimeoutError("connect timed out")])
    with pytest.raises(ToolError, match="Unable to fetch URL"):
        SafeWebFetcher(transport=connection_transport, resolver=public_resolver).fetch("https://example.com/")
    assert connection_transport.calls[0]["connect_timeout"] <= 5

    response = FakeResponse(200, {"Content-Type": "text/plain"}, chunks=[b"first", OSError("read failed")])
    with pytest.raises(ToolError, match="Unable to read web response"):
        SafeWebFetcher(transport=FakeTransport([response]), resolver=public_resolver).fetch("https://example.com/")
    assert response.closed is True


def test_web_fetch_stops_slow_chunks_at_overall_deadline() -> None:
    clock = FakeClock()
    response = FakeResponse(
        200,
        {"Content-Type": "text/plain"},
        chunks=[lambda: clock.advance(31) or b"late"],
    )

    with pytest.raises(ToolError, match="30-second time budget"):
        SafeWebFetcher(transport=FakeTransport([response]), resolver=public_resolver, clock=clock).fetch(
            "https://example.com/"
        )
    assert response.closed is True


def test_web_fetch_budget_spans_redirects_and_closes_the_last_response() -> None:
    clock = FakeClock()
    first = FakeResponse(302, {"Location": "/next"})
    second = FakeResponse(200, {"Content-Type": "text/plain"}, b"late")

    def first_request(call: dict[str, Any]) -> FakeResponse:
        del call
        clock.advance(20)
        return first

    def second_request(call: dict[str, Any]) -> FakeResponse:
        assert call["read_timeout"] <= 10
        clock.advance(11)
        return second

    with pytest.raises(ToolError, match="30-second time budget"):
        SafeWebFetcher(
            transport=FakeTransport([first_request, second_request]), resolver=public_resolver, clock=clock
        ).fetch("https://example.com/start")
    assert first.closed is True
    assert second.closed is True


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("fetch https://example.com/docs，", "https://example.com/docs"),
        ("fetch https://example.com/docs;", "https://example.com/docs"),
        ("fetch https://example.com/docs)", "https://example.com/docs"),
        ("fetch https://example.com/a(b)", "https://example.com/a(b)"),
        ("fetch https://example.com/archive.", "https://example.com/archive."),
    ],
)
def test_rule_planner_cleans_only_url_sentence_delimiters(task: str, expected: str) -> None:
    planner = RuleBasedPlanner()
    runner = AgentRunner(planner, ToolRegistry())

    fetch = planner.decide(runner.new_runtime(task=task)).tool_messages[0]

    assert (fetch.name, fetch.arguments) == ("web_fetch", {"url": expected})


@pytest.mark.parametrize("message", ["Unsupported content type application/pdf", "temporary connection failure"])
def test_web_fetch_failures_are_not_automatically_retried(tmp_path: Path, message: str) -> None:
    class FailingFetcher:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, url: str, max_chars: int = 50_000) -> str:
            del url, max_chars
            self.calls += 1
            raise ToolError(message)

    fetcher = FailingFetcher()
    tools = ToolRegistry(tmp_path, web_fetch=fetcher)
    runner = AgentRunner(RuleBasedPlanner(), tools, max_retries=3, max_tool_recoveries=0)
    runtime = runner.new_runtime(
        task="fetch https://example.com/report.pdf",
        interrupt=lambda _request: InterruptDecision("continue"),
    )

    runner.run(runtime)

    assert fetcher.calls == 1
