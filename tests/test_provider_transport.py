import pytest
import requests

from backend.domain import AssistantMessage, ToolMessage, ToolSpec, UserMessage
from backend.planning import LLMPlanner, RuleBasedPlanner
from backend.providers import ChatCompletions, LLMClient, ModelConfig, ModelRequestError
from backend.providers.chat_completions.messages import _wire_messages_from
from backend.runtime import AgentRunner, PreparedResponse
from backend.tools import ToolRegistry


class FakeStreamResponse:
    def __init__(self, lines: list[str | bytes]) -> None:
        self.lines = lines
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = False) -> list[str | bytes]:
        assert decode_unicode is False
        return self.lines

    def close(self) -> None:
        self.closed = True


class FakeStreamSession:
    def __init__(self, lines: list[str | bytes]) -> None:
        self.response = FakeStreamResponse(lines)
        self.calls = 0

    def post(self, url: str, **kwargs: object) -> FakeStreamResponse:
        self.calls += 1
        return self.response


class UnexpectedSession:
    def post(self, url: str, **kwargs: object) -> None:
        raise AssertionError("Transport must not run when request preparation fails.")


class CustomAdapter:
    endpoint = "https://provider.test/messages"
    headers = {"x-api-key": "secret"}
    timeout_seconds = 12
    operation = "custom_messages"

    def prepare_request(self, runtime) -> dict[str, object]:
        return {"input": runtime.exchange.messages[-1].content}

    def prepare_response(self, runtime) -> PreparedResponse:
        return PreparedResponse(AssistantMessage(content=runtime.exchange.raw_response["answer"]))


class RecordingTransport:
    def __init__(self) -> None:
        self.call: tuple[object, ...] | None = None

    def post_json(self, endpoint, headers, payload, timeout_seconds):
        self.call = (endpoint, headers, payload, timeout_seconds)
        return {"answer": "custom response"}


class SequencedTransport:
    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.calls = 0

    def post_json(self, endpoint, headers, payload, timeout_seconds):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeJsonResponse:
    def __init__(self, status_code: int, body: object, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        error = requests.HTTPError(f"HTTP {self.status_code}")
        error.response = self
        raise error

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class SequencedSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def post(self, url: str, **kwargs: object):
        self.calls += 1
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class SequencedStreamSession:
    def __init__(self, responses: list[FakeStreamResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def post(self, url: str, **kwargs: object) -> FakeStreamResponse:
        self.calls += 1
        return self.responses.pop(0)


def runtime_for_custom(*, max_transport_retries: int = 2):
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry(), max_transport_retries=max_transport_retries).new_runtime(
        task="hello"
    )
    runtime.exchange.messages = [UserMessage(content="hello")]
    return runtime


def runtime_for_stream():
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="hello")
    runtime.exchange.messages = [UserMessage(content="hello")]
    runtime.exchange.stream = True
    return runtime


def chat_completions_for_test() -> ChatCompletions:
    return ChatCompletions(ModelConfig("secret", "https://example.test/v1", "demo"))


def test_llm_client_accepts_an_injected_provider_adapter() -> None:
    transport = RecordingTransport()
    client = LLMClient(
        ModelConfig("secret", "https://unused.test", "custom-model", provider="custom"),
        transport=transport,
        adapter=CustomAdapter(),
    )
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="hello")
    runtime.exchange.messages = [UserMessage(content="hello")]

    response = client.run(runtime)

    assert response.message.content == "custom response"
    assert transport.call == (
        "https://provider.test/messages",
        {"x-api-key": "secret"},
        {"input": "hello"},
        12,
    )
    assert client.consume_request_diagnostics()["operation"] == "custom_messages"


def test_model_events_include_wire_bodies_and_transport_metadata() -> None:
    session = SequencedSession([FakeJsonResponse(200, {"answer": "custom response"}, {"request-id": "req-1"})])
    client = LLMClient(
        ModelConfig("secret", "https://unused.test", "custom-model", provider="custom"),
        session=session,
        adapter=CustomAdapter(),
    )
    runtime = runtime_for_custom()
    events = []
    runtime.services.publish = events.append

    client.run(runtime)

    request = next(event for event in events if event.kind == "model_request")
    response = next(event for event in events if event.kind == "model_response")
    assert request.data["schema_version"] == 2
    assert request.data["wire_request"] == {"input": "hello"}
    assert request.data["transport"]["endpoint"] == "https://provider.test/messages"
    assert request.data["transport"]["attempt"] == 1
    assert request.data["transport"]["request_body_bytes"] > 0
    assert response.data["wire_response"] == {"answer": "custom response"}
    assert response.data["transport"]["http_status"] == 200
    assert response.data["transport"]["response_headers"] == {"request-id": "req-1"}
    assert response.data["transport"]["duration_ms"] >= 0


def test_chat_completions_json_wire_request_forces_thinking_disabled() -> None:
    session = SequencedSession(
        [
            FakeJsonResponse(
                200,
                {
                    "id": "response-json",
                    "model": "demo",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "{}"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        ]
    )
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), session=session)
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="return json")
    runtime.exchange.messages = [UserMessage(content="return json")]
    runtime.exchange.output_mode = "json"
    runtime.state.request_parameters = {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    events = []
    runtime.services.publish = events.append

    client.run(runtime)

    request = next(event for event in events if event.kind == "model_request")
    assert request.data["wire_request"]["response_format"] == {"type": "json_object"}
    assert request.data["wire_request"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in request.data["wire_request"]


def test_stream_model_response_keeps_all_wire_events() -> None:
    session = FakeStreamSession(
        [
            'data: {"id":"stream-1","choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), session=session)
    runtime = runtime_for_stream()
    events = []
    runtime.services.publish = events.append

    client.run(runtime)

    response = next(event for event in events if event.kind == "model_response")
    assert response.data["wire_response"] == [
        {
            "id": "stream-1",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }
    ]
    assert response.data["transport"]["stream_completed"] is True


def test_stream_model_response_decodes_utf8_bytes_independent_of_response_charset() -> None:
    session = FakeStreamSession(
        [
            (
                'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"你好"},"finish_reason":"stop"}]}'
            ).encode(),
            b"data: [DONE]",
        ]
    )
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), session=session)
    runtime = runtime_for_stream()

    response = client.run(runtime)

    assert response.message.content == "你好"


def test_request_preparation_failure_replaces_stale_diagnostics() -> None:
    client = LLMClient(
        ModelConfig("secret", "https://example.test/v1", "demo"),
        session=UnexpectedSession(),
    )
    client._last_request_diagnostics = {"request_outcome": "stale"}
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="hello")
    runtime.state.messages = []

    with pytest.raises(ModelRequestError, match="at least one message") as exc_info:
        client.run(runtime)

    assert exc_info.value.diagnostics["provider"] == "chat_completions"
    assert exc_info.value.diagnostics["request_outcome"] == "failed"
    assert "stale" not in exc_info.value.diagnostics.values()
    assert client.consume_request_diagnostics() == exc_info.value.diagnostics


def test_stream_schema_failure_closes_response_and_discards_raw_iterator() -> None:
    session = FakeStreamSession(['data: {"choices":{}}', "data: [DONE]"])
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), session=session)
    runtime = runtime_for_stream()

    with pytest.raises(ModelRequestError, match="choices must be an array"):
        client.run(runtime)

    assert session.response.closed is True
    assert runtime.exchange.raw_response is None


def test_stream_eof_before_done_is_rejected_and_closed() -> None:
    session = FakeStreamSession(
        ['data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"partial"},"finish_reason":"stop"}]}']
    )
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), session=session)
    runtime = runtime_for_stream()

    with pytest.raises(ModelRequestError, match=r"before \[DONE\]"):
        client.run(runtime)

    assert session.response.closed is True
    assert session.calls == 1
    assert runtime.exchange.raw_response is None


def test_stream_done_without_finish_reason_is_rejected() -> None:
    session = FakeStreamSession(
        [
            'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"partial"},"finish_reason":null}]}',
            "data: [DONE]",
        ]
    )
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), session=session)
    runtime = runtime_for_stream()

    with pytest.raises(ModelRequestError, match="without a finish reason"):
        client.run(runtime)

    assert session.response.closed is True
    assert runtime.exchange.raw_response is None


def test_non_stream_tool_calls_rejects_falsey_non_array() -> None:
    runtime = runtime_for_stream()
    runtime.exchange.stream = False
    runtime.exchange.raw_response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "hello", "tool_calls": {}},
                "finish_reason": "stop",
            }
        ]
    }

    with pytest.raises(ModelRequestError, match="tool_calls must be an array"):
        chat_completions_for_test().prepare_response(runtime)


@pytest.mark.parametrize(
    ("delta", "error"),
    [
        ([], "choice.delta must be an object"),
        ({"tool_calls": {}}, "tool_calls must be an array"),
        (
            {"tool_calls": [{"index": 0, "function": []}]},
            "tool call function must be an object",
        ),
    ],
)
def test_stream_fields_reject_falsey_invalid_types(delta: object, error: str) -> None:
    runtime = runtime_for_stream()
    runtime.exchange.raw_response = iter([{"choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}]}])

    with pytest.raises(ModelRequestError, match=error):
        chat_completions_for_test().prepare_response(runtime)


def test_response_rejects_duplicate_tool_call_ids() -> None:
    runtime = runtime_for_stream()
    runtime.exchange.stream = False
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "run_command", "arguments": "{}"},
    }
    runtime.exchange.raw_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call, tool_call],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    with pytest.raises(ModelRequestError, match="Duplicate Chat Completions tool call id"):
        chat_completions_for_test().prepare_response(runtime)


def test_wire_messages_merges_duplicate_canonical_tool_calls() -> None:
    messages = [
        AssistantMessage(
            content=None,
            tool_messages=[
                ToolMessage(name="glob", call_id="call_a", arguments={}, content="glob", status="succeeded"),
                ToolMessage(name="run_command", call_id="call_b", arguments={}, status="pending"),
            ],
        ),
        AssistantMessage(
            content=None,
            tool_messages=[
                ToolMessage(
                    name="run_command",
                    call_id="call_b",
                    arguments={},
                    content="command",
                    status="succeeded",
                )
            ],
        ),
    ]

    wire = _wire_messages_from(messages)
    assistant = [item for item in wire if item.get("role") == "assistant"]
    tools = [item for item in wire if item.get("role") == "tool"]
    assert len(assistant) == 1
    assert [call["id"] for call in assistant[0]["tool_calls"]] == ["call_a", "call_b"]
    assert [item["tool_call_id"] for item in tools] == ["call_a", "call_b"]


@pytest.mark.parametrize("status_code", [429, 503])
def test_transient_http_status_retries_with_retry_after(monkeypatch, status_code: int) -> None:
    session = SequencedSession(
        [
            FakeJsonResponse(status_code, {}, {"Retry-After": "0"}),
            FakeJsonResponse(200, {"answer": "recovered"}),
        ]
    )
    client = LLMClient(
        ModelConfig("secret", "https://unused.test", "custom-model", provider="custom"),
        session=session,
        adapter=CustomAdapter(),
    )
    runtime = runtime_for_custom()
    events = []
    runtime.services.publish = events.append
    delays = []
    monkeypatch.setattr("backend.providers.client.time.sleep", delays.append)

    response = client.run(runtime)

    assert response.message.content == "recovered"
    assert session.calls == 2
    assert delays == [0.0]
    assert [event.kind for event in events] == [
        "model_request",
        "model_retry",
        "model_request",
        "model_response",
    ]
    assert events[0].data["transport"]["attempt"] == 1
    assert events[2].data["transport"]["attempt"] == 2
    assert events[1].data["status_code"] == status_code


def test_non_retryable_http_status_fails_immediately(monkeypatch) -> None:
    session = SequencedSession(
        [
            FakeJsonResponse(401, {}),
            FakeJsonResponse(200, {"answer": "must not run"}),
        ]
    )
    client = LLMClient(
        ModelConfig("secret", "https://unused.test", "custom-model", provider="custom"),
        session=session,
        adapter=CustomAdapter(),
    )
    runtime = runtime_for_custom()
    monkeypatch.setattr("backend.providers.client.time.sleep", lambda _delay: None)

    with pytest.raises(ModelRequestError, match="HTTPError"):
        client.run(runtime)

    assert session.calls == 1


def test_invalid_json_http_body_retries(monkeypatch) -> None:
    session = SequencedSession(
        [
            FakeJsonResponse(200, ValueError("invalid JSON")),
            FakeJsonResponse(200, {"answer": "recovered"}),
        ]
    )
    client = LLMClient(
        ModelConfig("secret", "https://unused.test", "custom-model", provider="custom"),
        session=session,
        adapter=CustomAdapter(),
    )
    runtime = runtime_for_custom()
    monkeypatch.setattr("backend.providers.client.time.sleep", lambda _delay: None)

    response = client.run(runtime)

    assert response.message.content == "recovered"
    assert session.calls == 2


def test_stream_retries_only_before_the_first_event(monkeypatch) -> None:
    first = FakeStreamResponse([])
    second = FakeStreamResponse(
        [
            'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"done"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )
    session = SequencedStreamSession([first, second])
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), session=session)
    runtime = runtime_for_stream()
    monkeypatch.setattr("backend.providers.client.time.sleep", lambda _delay: None)

    response = client.run(runtime)

    assert response.message.content == "done"
    assert session.calls == 2
    assert first.closed is True
    assert second.closed is True


def test_chat_completions_invalid_tool_arguments_are_regenerated_before_execution() -> None:
    invalid = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": "not-json"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    valid = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": '{"command":"echo ok"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    transport = SequencedTransport([invalid, valid])
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), transport=transport)
    spec = ToolSpec("run_command", "Run a command")
    planner = LLMPlanner(client, [spec], [spec])
    runtime = AgentRunner(
        planner,
        ToolRegistry(),
        max_transport_retries=0,
    ).new_runtime(task="run it")

    message = planner.decide(runtime)

    assert transport.calls == 2
    assert message.tool_messages[0].arguments == {"command": "echo ok"}
    assert planner.consume_output_repairs()[0]["outcome"] == "repaired"


def test_connection_timeout_retries(monkeypatch) -> None:
    session = SequencedSession(
        [
            requests.Timeout("timed out"),
            FakeJsonResponse(200, {"answer": "recovered"}),
        ]
    )
    client = LLMClient(
        ModelConfig("secret", "https://unused.test", "custom-model", provider="custom"),
        session=session,
        adapter=CustomAdapter(),
    )
    runtime = runtime_for_custom()
    delays = []
    monkeypatch.setattr("backend.providers.client.time.sleep", delays.append)

    response = client.run(runtime)

    assert response.message.content == "recovered"
    assert session.calls == 2
    assert delays == [0.5]
