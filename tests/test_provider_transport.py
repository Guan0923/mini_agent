import pytest

from mini_agent.domain import AssistantMessage, UserMessage
from mini_agent.planning import RuleBasedPlanner
from mini_agent.providers import DeepSeek, LLMClient, ModelConfig, ModelRequestError
from mini_agent.runtime import AgentRunner, PreparedResponse
from mini_agent.tools import ToolRegistry


class FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode: bool = True) -> list[str]:
        assert decode_unicode is True
        return self.lines

    def close(self) -> None:
        self.closed = True


class FakeStreamSession:
    def __init__(self, lines: list[str]) -> None:
        self.response = FakeStreamResponse(lines)

    def post(self, url: str, **kwargs: object) -> FakeStreamResponse:
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


def runtime_for_stream():
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="hello")
    runtime.exchange.messages = [UserMessage(content="hello")]
    runtime.exchange.stream = True
    return runtime


def deepseek_for_test() -> DeepSeek:
    return DeepSeek(ModelConfig("secret", "https://example.test/v1", "demo"))


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

    assert exc_info.value.diagnostics["provider"] == "deepseek"
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
        deepseek_for_test().prepare_response(runtime)


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
        deepseek_for_test().prepare_response(runtime)


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

    with pytest.raises(ModelRequestError, match="Duplicate DeepSeek tool call id"):
        deepseek_for_test().prepare_response(runtime)
