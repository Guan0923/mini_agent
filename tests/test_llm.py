import json

import pytest
import requests

from backend.domain import (
    AssistantMessage,
    ModelOutputError,
    SystemMessage,
    ToolMessage,
    ToolSpec,
    UserMessage,
)
from backend.planning import LLMPlanner, RuleBasedPlanner
from backend.providers import (
    ChatCompletions,
    LLMClient,
    ModelConfig,
    ModelRequestError,
)
from backend.runtime import AgentRunner, PreparedResponse
from backend.tools import Tool, ToolRegistry


def runtime_for(*, messages=None, tools=None):
    registry = ToolRegistry(tools or [])
    runtime = AgentRunner(RuleBasedPlanner(), registry).new_runtime(task="hello", messages=messages or [])
    runtime.state.model = "demo"
    runtime.state.request_parameters = {"max_tokens": 512, "temperature": 0}
    return runtime


def chat_completions_for_test() -> ChatCompletions:
    return ChatCompletions(ModelConfig("secret", "https://example.test/v1", "demo"))


def test_prepare_request_expands_nested_tool_messages() -> None:
    tool = ToolMessage(
        name="run_command",
        call_id="call_1",
        arguments={"expression": "2 + 2"},
        content="4",
        status="succeeded",
    )
    runtime = runtime_for(
        messages=[
            UserMessage(content="calculate"),
            AssistantMessage(reasoning="Use arithmetic.", tool_messages=[tool]),
        ]
    )
    runtime.exchange.messages = runtime.state.messages[:2]
    runtime.exchange.output_mode = "tools"
    runtime.exchange.allowed_tools = [
        ToolSpec(
            "run_command",
            "Calculate.",
            {"type": "object", "properties": {"expression": {"type": "string"}}},
        )
    ]

    payload = chat_completions_for_test().prepare_request(runtime)

    assert "response_format" not in payload
    assert payload["tools"][0]["function"]["name"] == "run_command"
    assistant, result = payload["messages"][-2:]
    assert assistant["reasoning_content"] == "Use arithmetic."
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"expression": "2 + 2"}
    assert result == {"role": "tool", "tool_call_id": "call_1", "content": "4"}


def test_prepare_request_omits_empty_assistant_history() -> None:
    messages = [
        UserMessage(content="Start"),
        AssistantMessage(),
        AssistantMessage(content=" \n", reasoning="Internal reasoning"),
        UserMessage(content="Continue"),
    ]
    runtime = runtime_for(messages=messages)
    runtime.exchange.messages = messages

    payload = chat_completions_for_test().prepare_request(runtime)

    assert payload["messages"] == [
        {"role": "user", "content": "Start"},
        {"role": "user", "content": "Continue"},
    ]


def test_prepare_request_supports_documented_chat_completions_parameters() -> None:
    messages = [UserMessage(name="alice", content="Use a tool.")]
    runtime = runtime_for(messages=messages)
    runtime.exchange.messages = messages
    runtime.state.request_parameters = {
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "max_tokens": 1024,
        "temperature": 0.2,
        "top_p": 0.9,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
        "response_format": {"type": "text"},
        "stop": ["DONE"],
        "logprobs": True,
        "top_logprobs": 5,
        "user_id": "user_123",
        "tool_choice": {"type": "function", "function": {"name": "run_command"}},
        "extra_body": {"future_parameter": "supported"},
    }
    runtime.exchange.stream = True
    runtime.exchange.output_mode = "tools"
    runtime.exchange.allowed_tools = [
        ToolSpec(
            "run_command",
            "Calculate.",
            {"type": "object"},
            provider_options={"chat_completions": {"strict": True}},
        )
    ]

    payload = chat_completions_for_test().prepare_request(runtime)

    assert payload["messages"] == [{"role": "user", "content": "Use a tool.", "name": "alice"}]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"
    assert payload["max_tokens"] == 1024
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.9
    assert payload["frequency_penalty"] == 0
    assert payload["presence_penalty"] == 0
    assert payload["response_format"] == {"type": "text"}
    assert payload["stop"] == ["DONE"]
    assert payload["logprobs"] is True
    assert payload["top_logprobs"] == 5
    assert payload["user_id"] == "user_123"
    assert payload["tools"][0]["function"]["strict"] is True
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "run_command"}}
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["future_parameter"] == "supported"


def test_prepare_json_request_forces_thinking_off_and_drops_effort() -> None:
    messages = [UserMessage(content="Return JSON.")]
    runtime = runtime_for(messages=messages)
    runtime.exchange.messages = messages
    runtime.state.request_parameters = {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    runtime.exchange.output_mode = "json"

    payload = chat_completions_for_test().prepare_request(runtime)

    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize(
    ("parameters", "stream", "error"),
    [
        ({"top_logprobs": 3}, False, "requires logprobs=true"),
        ({"stream_options": {"include_usage": True}}, False, "only valid when stream=true"),
        ({"unknown": True}, False, "use extra_body"),
        ({"extra_body": {"model": "override"}}, False, "cannot override: model"),
    ],
)
def test_prepare_request_rejects_invalid_parameter_combinations(parameters, stream, error) -> None:
    runtime = runtime_for()
    runtime.state.request_parameters = parameters
    runtime.exchange.stream = stream

    with pytest.raises(ModelRequestError, match=error):
        chat_completions_for_test().prepare_request(runtime)


def test_prepare_response_preserves_usage_logprobs_and_tool_calls() -> None:
    runtime = runtime_for()
    runtime.exchange.raw_response = {
        "id": "chatcmpl-test",
        "model": "chat_completions-test",
        "created": 1718345013,
        "object": "chat.completion",
        "system_fingerprint": "fp_test",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "Need a calculation.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": '{"expression":"2 + 2"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
                "logprobs": {"content": [{"token": "x", "logprob": -0.1}]},
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "prompt_cache_hit_tokens": 4,
            "prompt_cache_miss_tokens": 6,
            "completion_tokens": 3,
            "completion_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 13,
        },
    }

    response = chat_completions_for_test().prepare_response(runtime)

    assert response.message.name == "assistant"
    assert response.message.role == "assistant"
    assert response.message.reasoning == "Need a calculation."
    assert response.message.logprobs == {"content": [{"token": "x", "logprob": -0.1}]}
    assert response.message.tool_messages == [
        ToolMessage(name="run_command", call_id="call_1", arguments={"expression": "2 + 2"})
    ]
    assert response.usage == {
        "prompt_tokens": 10,
        "prompt_cache_hit_tokens": 4,
        "prompt_cache_miss_tokens": 6,
        "completion_tokens": 3,
        "completion_tokens_details": {"reasoning_tokens": 2},
        "total_tokens": 13,
    }
    assert response.provider_metadata["created"] == 1718345013
    assert response.provider_metadata["system_fingerprint"] == "fp_test"
    assert response.message.provider_options["chat_completions"]["response"]["choice_index"] == 0
    assert runtime.state.turn_usage == response.usage


def test_prepare_response_uses_lowest_choice_and_preserves_alternatives() -> None:
    runtime = runtime_for()
    runtime.exchange.raw_response = {
        "id": "multi",
        "model": "chat_completions-test",
        "choices": [
            {
                "index": 1,
                "message": {"role": "assistant", "content": "Alternative"},
                "finish_reason": "length",
                "logprobs": None,
            },
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Primary"},
                "finish_reason": "content_filter",
                "logprobs": None,
            },
        ],
    }

    response = chat_completions_for_test().prepare_response(runtime)

    assert response.message.content == "Primary"
    assert response.finish_reason == "content_filter"
    alternatives = response.message.provider_options["chat_completions"]["response"]["alternative_choices"]
    assert alternatives[0]["index"] == 1


def test_prepare_response_rejects_invalid_tool_arguments_before_execution() -> None:
    runtime = runtime_for()
    runtime.exchange.raw_response = {
        "choices": [
            {
                "index": 0,
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

    with pytest.raises(ModelRequestError, match="Expecting value"):
        chat_completions_for_test().prepare_response(runtime)


def test_prepare_response_aggregates_streamed_reasoning_and_tool_arguments() -> None:
    runtime = runtime_for()
    reasoning = []
    runtime.exchange.on_reasoning = reasoning.append
    runtime.exchange.raw_response = iter(
        [
            {
                "id": "stream-1",
                "model": "chat_completions-test",
                "created": 1718345013,
                "object": "chat.completion.chunk",
                "system_fingerprint": "fp_stream",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "reasoning_content": "Think ",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "run_command", "arguments": '{"expression":'},
                                }
                            ],
                        },
                        "finish_reason": None,
                        "logprobs": {
                            "content": None,
                            "reasoning_content": [{"token": "Think", "logprob": -0.1}],
                        },
                    }
                ],
                "usage": None,
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "reasoning_content": "now.",
                            "tool_calls": [{"index": 0, "function": {"arguments": '"2 + 2"}'}}],
                        },
                        "finish_reason": "tool_calls",
                        "logprobs": {
                            "content": [{"token": "", "logprob": -0.2}],
                            "reasoning_content": [{"token": "now", "logprob": -0.3}],
                        },
                    }
                ],
                "usage": None,
            },
            {"choices": [], "usage": {"total_tokens": 12, "completion_tokens_details": {"reasoning_tokens": 2}}},
        ]
    )

    response = chat_completions_for_test().prepare_response(runtime)

    assert response.message.reasoning == "Think now."
    assert reasoning == ["Think ", "now."]
    assert response.message.tool_messages[0].arguments == {"expression": "2 + 2"}
    assert response.message.logprobs == {
        "content": [{"token": "", "logprob": -0.2}],
        "reasoning_content": [
            {"token": "Think", "logprob": -0.1},
            {"token": "now", "logprob": -0.3},
        ],
    }
    assert response.usage == {"total_tokens": 12, "completion_tokens_details": {"reasoning_tokens": 2}}
    assert response.provider_metadata["object"] == "chat.completion.chunk"
    assert response.provider_metadata["system_fingerprint"] == "fp_stream"


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "id": "chatcmpl-test",
            "model": "chat_completions-test",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello", "reasoning_content": "Greet."},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }


class FakeSession:
    def __init__(self) -> None:
        self.request = None

    def post(self, url, **kwargs):
        self.request = {"url": url, **kwargs}
        return FakeResponse()


class FakeStreamResponse:
    def __init__(self) -> None:
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode=False):
        assert decode_unicode is False
        return [
            'data: {"id":"stream","model":"demo","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null,"logprobs":null}],"usage":null}',
            'data: {"choices":[{"index":0,"delta":{"content":"!"},"finish_reason":"stop","logprobs":null}],"usage":null}',
            'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}',
            "data: [DONE]",
        ]

    def close(self) -> None:
        self.closed = True


class FakeStreamSession:
    def __init__(self) -> None:
        self.request = None
        self.response = FakeStreamResponse()

    def post(self, url, **kwargs):
        self.request = {"url": url, **kwargs}
        return self.response


class FailingSession:
    def post(self, url, **kwargs):
        raise requests.Timeout("timeout")


def test_llm_client_delegates_to_chat_completions_runtime() -> None:
    session = FakeSession()
    client = LLMClient(
        ModelConfig("secret", "https://example.test/v1", "demo"),
        session=session,
    )
    runtime = runtime_for()
    runtime.exchange.messages = [UserMessage(content="hello")]

    response = client.run(runtime)

    assert isinstance(client.llm, ChatCompletions)
    assert session.request["url"] == "https://example.test/v1/chat/completions"
    assert session.request["json"]["messages"] == [{"role": "user", "content": "hello"}]
    assert response.message.content == "Hello"
    assert response.message.reasoning == "Greet."
    assert runtime.state.turn_usage["total_tokens"] == 4


def test_llm_client_runs_chat_completions_sse_lifecycle() -> None:
    session = FakeStreamSession()
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), session=session)
    runtime = runtime_for()
    runtime.exchange.messages = [UserMessage(content="hello")]
    runtime.exchange.stream = True

    response = client.run(runtime)

    assert session.request["stream"] is True
    assert session.request["json"]["stream_options"] == {"include_usage": True}
    assert response.message.content == "Hi!"
    assert response.usage == {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
    assert session.response.closed is True


def test_llm_client_attaches_provider_diagnostics_to_http_errors() -> None:
    client = LLMClient(
        ModelConfig("secret", "https://example.test/v1", "demo"),
        session=FailingSession(),
    )
    runtime = runtime_for()

    with pytest.raises(ModelRequestError, match="timeout") as exc_info:
        client.run(runtime)

    assert exc_info.value.diagnostics["provider"] == "chat_completions"
    assert exc_info.value.diagnostics["request_outcome"] == "failed"


def test_llm_client_accepts_arbitrary_provider_name() -> None:
    config = ModelConfig(
        "secret",
        "https://example.test/v1",
        "demo",
        provider="unknown",
        provider_name="my-custom-api",
    )

    client = LLMClient(config)
    assert client.config.provider_name == "my-custom-api"
    assert client.config.provider == "chat_completions"


def test_model_config_loads_provider_from_env_file(tmp_path, monkeypatch) -> None:
    for name in ("API_KEY", "BASE_URL", "MODEL", "MAX_TOKENS", "PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "API_KEY=secret\nBASE_URL=https://example.test/v1\nMODEL=demo\nPROVIDER=DEEPSEEK\n",
        encoding="utf-8",
    )

    config = ModelConfig.from_env(env_path)

    assert config.provider == "chat_completions"


def test_model_config_migrates_legacy_vendor_defaults() -> None:
    config = ModelConfig.from_mapping(
        {
            "api_key": "secret",
            "base_url": "https://example.test/v1",
            "model": "demo",
            "provider": "deepseek",
            "provider_name": "deepseek",
            "protocol": "chat_completions",
            "tokenizer_model": "deepseek-ai/DeepSeek-V3",
        }
    )

    assert config.provider == "chat_completions"
    assert config.provider_name == "default"
    assert config.tokenizer_model == ""


def test_runtime_state_migrates_legacy_provider_and_options() -> None:
    state = runtime_for().state
    payload = state.to_dict()
    payload.update(
        {
            "provider": "deepseek",
            "provider_name": "deepseek",
            "messages": [
                {
                    "role": "user",
                    "name": "user",
                    "content": "hello",
                    "provider_options": {"deepseek": {"extra_body": {"feature": True}}},
                }
            ],
            "tool_specs": [
                {
                    "name": "lookup",
                    "description": "Lookup",
                    "parameters": {"type": "object"},
                    "provider_options": {"deepseek": {"strict": True}},
                }
            ],
        }
    )

    restored = type(state).from_dict(payload)

    assert restored.provider == "chat_completions"
    assert restored.provider_name == "default"
    assert restored.messages[0].provider_options == {"chat_completions": {"extra_body": {"feature": True}}}
    assert restored.tool_specs[0].provider_options == {"chat_completions": {"strict": True}}


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.message_requests = []
        self.exchange_ids = []

    def run(self, runtime):
        self.requests.append(runtime.exchange)
        self.message_requests.append(list(runtime.exchange.messages))
        self.exchange_ids.append(runtime.exchange.exchange_id)
        response = self.responses.pop(0)
        runtime.state.turn_usage = response.usage
        return response


def test_llm_planner_uses_native_tool_response_with_runtime_only() -> None:
    client = ScriptedClient(
        [
            PreparedResponse(
                AssistantMessage(
                    reasoning="Calculate.",
                    tool_messages=[
                        ToolMessage(
                            name="run_command",
                            call_id="call_1",
                            arguments={"expression": "2 + 2"},
                        )
                    ],
                ),
                usage={"total_tokens": 8},
            )
        ]
    )
    spec = ToolSpec("run_command", "Calculate", {"type": "object"})
    planner = LLMPlanner(client, [spec], [spec])
    tools = [Tool("run_command", "Calculate", lambda expression: expression, parameters=spec.parameters)]
    runtime = AgentRunner(planner, ToolRegistry(tools)).new_runtime(task="2 + 2")

    message = planner.decide(runtime)

    assert message.tool_messages[0].name == "run_command"
    assert runtime.exchange.output_mode == "tools"
    assert runtime.exchange.allowed_tools == [spec]


def test_plan_decision_exposes_request_user_input_without_registering_it() -> None:
    client = ScriptedClient([PreparedResponse(AssistantMessage(content="1. Inspect the project."))])
    planner = LLMPlanner(client, [], [])
    registry = ToolRegistry()
    runtime = AgentRunner(planner, registry).new_runtime(task="Plan the change", mode="plan")

    planner.decide(runtime)

    assert registry.names() == []
    assert [spec.name for spec in runtime.exchange.allowed_tools] == [
        "request_user_input",
        "request_plan_review",
    ]
    system = runtime.exchange.messages[0]
    assert isinstance(system, SystemMessage)
    assert "request_user_input" in (system.content or "")
    assert "request_plan_review" in (system.content or "")
    assert "does not require every response" in (system.content or "")


def test_llm_planner_rejects_unknown_native_tool() -> None:
    client = ScriptedClient(
        [PreparedResponse(AssistantMessage(tool_messages=[ToolMessage(name="shell", call_id="call_1", arguments={})]))]
    )
    spec = ToolSpec("run_command", "Calculate")
    planner = LLMPlanner(client, [spec], [spec])
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task="run it")
    runtime.services.cancel_requested = lambda: True

    with pytest.raises(ModelOutputError, match="cancelled"):
        planner.decide(runtime)


def test_llm_client_reconciles_estimated_and_provider_token_usage() -> None:
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), adapter=object())
    runtime = runtime_for()
    runtime.exchange.exchange_id = "exchange_usage"
    runtime.exchange.context["estimated_input_tokens"] = 11

    client._begin_token_usage(runtime)
    client._complete_token_usage(
        runtime,
        {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        AssistantMessage(content="done"),
    )

    entry = runtime.state.token_usage["requests"]["exchange_usage"]
    assert entry["estimated_input_tokens"] == 11
    assert entry["input_tokens"] == 8
    assert entry["output_tokens"] == 3
    assert entry["total_tokens"] == 11
    assert entry["input_source"] == "provider"
    assert entry["output_source"] == "provider"
    assert entry["total_source"] == "provider"
    assert runtime.state.token_usage["totals"] == {
        "input_tokens": 8,
        "output_tokens": 3,
        "total_tokens": 11,
    }
    assert runtime.state.token_usage == type(runtime.state).from_dict(runtime.state.to_dict()).token_usage


def test_llm_client_accumulates_context_input_and_replaces_estimates_with_provider_usage() -> None:
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), adapter=object())
    runtime = runtime_for()
    events = []
    runtime.services.publish = events.append
    runtime.services.planner = type("Planner", (), {"client": type("Model", (), {"context_size": 100})()})()

    runtime.exchange.exchange_id = "first"
    runtime.exchange.context["estimated_input_tokens"] = 10
    client._begin_token_usage(runtime)
    client._complete_token_usage(runtime, {"prompt_tokens": 8}, AssistantMessage(content="first"))

    runtime.exchange.exchange_id = "second"
    runtime.exchange.context["estimated_input_tokens"] = 15
    client._begin_token_usage(runtime)
    estimated = events[-1]
    assert estimated.data["cumulative_input_tokens"] == 23
    assert estimated.data["current_input_tokens"] == 15
    assert estimated.data["input_source"] == "estimated"

    client._complete_token_usage(runtime, {"prompt_tokens": 12}, AssistantMessage(content="second"))
    provider = events[-1]
    assert provider.data["cumulative_input_tokens"] == 20
    assert provider.data["current_input_tokens"] == 12
    assert provider.data["input_source"] == "provider"
    assert runtime.state.token_usage["totals"]["input_tokens"] == 20
    assert runtime.state.token_usage == type(runtime.state).from_dict(runtime.state.to_dict()).token_usage


def test_llm_client_discards_unconfirmed_context_estimate() -> None:
    client = LLMClient(ModelConfig("secret", "https://example.test/v1", "demo"), adapter=object())
    runtime = runtime_for()
    events = []
    runtime.services.publish = events.append
    runtime.services.planner = type("Planner", (), {"client": type("Model", (), {"context_size": 100})()})()
    runtime.exchange.exchange_id = "failed"
    runtime.exchange.context["estimated_input_tokens"] = 10

    client._begin_token_usage(runtime)
    client._usage_tracker().discard_unconfirmed(runtime)

    assert runtime.state.token_usage["requests"] == {}
    assert runtime.state.token_usage["totals"]["input_tokens"] == 0
    assert events[-1].data["phase"] == "discarded"
    assert events[-1].data["cumulative_input_tokens"] == 0
