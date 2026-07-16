import json

import pytest
import requests

from mini_agent.domain import AssistantMessage, PlanningError, SystemMessage, ToolMessage, ToolSpec, UserMessage
from mini_agent.planning import LLMPlanner, RuleBasedPlanner
from mini_agent.providers import (
    DeepSeek,
    LLMClient,
    ModelConfig,
    ModelConfigurationError,
    ModelRequestError,
)
from mini_agent.runtime import AgentRunner, PreparedResponse
from mini_agent.tools import Tool, ToolRegistry


def runtime_for(*, messages=None, tools=None):
    registry = ToolRegistry(tools or [])
    runtime = AgentRunner(RuleBasedPlanner(), registry).new_runtime(task="hello", messages=messages or [])
    runtime.state.model = "demo"
    runtime.state.request_parameters = {"max_tokens": 512, "temperature": 0}
    return runtime


def deepseek_for_test() -> DeepSeek:
    return DeepSeek(ModelConfig("secret", "https://example.test/v1", "demo"))


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

    payload = deepseek_for_test().prepare_request(runtime)

    assert "response_format" not in payload
    assert payload["tools"][0]["function"]["name"] == "run_command"
    assistant, result = payload["messages"][-2:]
    assert assistant["reasoning_content"] == "Use arithmetic."
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"expression": "2 + 2"}
    assert result == {"role": "tool", "tool_call_id": "call_1", "content": "4"}


def test_prepare_request_replays_plan_question_without_exposing_control_tool_in_agent_mode() -> None:
    question = ToolMessage(
        name="request_user_input",
        call_id="question_1",
        arguments={"questions": []},
        content='{"answers":{"scope":{"answers":["Focused"]}}}',
        status="succeeded",
    )
    runtime = runtime_for(messages=[AssistantMessage(tool_messages=[question]), UserMessage(content="Implement the plan")])
    runtime.exchange.messages = runtime.state.messages
    runtime.exchange.output_mode = "tools"
    runtime.exchange.allowed_tools = [ToolSpec("run_command", "Execute commands.", {"type": "object"})]

    payload = deepseek_for_test().prepare_request(runtime)

    assert [tool["function"]["name"] for tool in payload["tools"]] == ["run_command"]
    assert payload["messages"][0]["tool_calls"][0]["function"]["name"] == "request_user_input"
    assert payload["messages"][1] == {
        "role": "tool",
        "tool_call_id": "question_1",
        "content": question.content,
    }


def test_prepare_request_rejects_pending_tool_history() -> None:
    runtime = runtime_for(messages=[AssistantMessage(tool_messages=[ToolMessage(name="run_command", call_id="call_1")])])

    with pytest.raises(ModelRequestError, match="no result"):
        deepseek_for_test().prepare_request(runtime)


def test_prepare_request_supports_documented_deepseek_parameters() -> None:
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
            provider_options={"deepseek": {"strict": True}},
        )
    ]

    payload = deepseek_for_test().prepare_request(runtime)

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


def test_prepare_request_supports_assistant_prefix_and_explicit_usage_opt_out() -> None:
    messages = [
        SystemMessage(name="guide", content="Answer directly."),
        UserMessage(content="Complete this."),
        AssistantMessage(
            content="Prefix:",
            reasoning="Continue carefully.",
            provider_options={"deepseek": {"prefix": True}},
        ),
    ]
    runtime = runtime_for(messages=messages)
    runtime.exchange.messages = messages
    runtime.state.request_parameters["stream_options"] = {"include_usage": False}
    runtime.exchange.stream = True

    payload = deepseek_for_test().prepare_request(runtime)

    assert payload["messages"][0] == {"role": "system", "content": "Answer directly.", "name": "guide"}
    assert payload["messages"][-1] == {
        "role": "assistant",
        "content": "Prefix:",
        "prefix": True,
        "reasoning_content": "Continue carefully.",
    }
    assert payload["stream_options"] == {"include_usage": False}


@pytest.mark.parametrize("tool_choice", ["none", "auto", "required"])
def test_prepare_request_supports_string_tool_choices(tool_choice) -> None:
    runtime = runtime_for()
    runtime.state.request_parameters["tool_choice"] = tool_choice
    runtime.exchange.allowed_tools = [ToolSpec("run_command", "Calculate.")]

    payload = deepseek_for_test().prepare_request(runtime)

    assert payload["tool_choice"] == tool_choice


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
        deepseek_for_test().prepare_request(runtime)


def test_prepare_response_preserves_usage_logprobs_and_tool_calls() -> None:
    runtime = runtime_for()
    runtime.exchange.raw_response = {
        "id": "chatcmpl-test",
        "model": "deepseek-test",
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

    response = deepseek_for_test().prepare_response(runtime)

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
    assert response.message.provider_options["deepseek"]["response"]["choice_index"] == 0
    assert runtime.state.turn_usage == response.usage


def test_prepare_response_uses_lowest_choice_and_preserves_alternatives() -> None:
    runtime = runtime_for()
    runtime.exchange.raw_response = {
        "id": "multi",
        "model": "deepseek-test",
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

    response = deepseek_for_test().prepare_response(runtime)

    assert response.message.content == "Primary"
    assert response.finish_reason == "content_filter"
    alternatives = response.message.provider_options["deepseek"]["response"]["alternative_choices"]
    assert alternatives[0]["index"] == 1


@pytest.mark.parametrize(
    "finish_reason",
    ["stop", "length", "content_filter", "tool_calls", "insufficient_system_resource"],
)
def test_prepare_response_preserves_documented_finish_reasons(finish_reason) -> None:
    runtime = runtime_for()
    runtime.exchange.raw_response = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Partial"},
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ]
    }

    response = deepseek_for_test().prepare_response(runtime)

    assert response.finish_reason == finish_reason


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

    with pytest.raises(ModelRequestError, match="not valid JSON"):
        deepseek_for_test().prepare_response(runtime)


def test_prepare_response_aggregates_streamed_reasoning_and_tool_arguments() -> None:
    runtime = runtime_for()
    reasoning = []
    runtime.exchange.on_reasoning = reasoning.append
    runtime.exchange.raw_response = iter(
        [
            {
                "id": "stream-1",
                "model": "deepseek-test",
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

    response = deepseek_for_test().prepare_response(runtime)

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
            "model": "deepseek-test",
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

    def iter_lines(self, decode_unicode=True):
        assert decode_unicode is True
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


def test_llm_client_delegates_to_deepseek_runtime() -> None:
    session = FakeSession()
    client = LLMClient(
        ModelConfig("secret", "https://example.test/v1", "demo"),
        session=session,
    )
    runtime = runtime_for()
    runtime.exchange.messages = [UserMessage(content="hello")]

    response = client.run(runtime)

    assert isinstance(client.llm, DeepSeek)
    assert session.request["url"] == "https://example.test/v1/chat/completions"
    assert session.request["json"]["messages"] == [{"role": "user", "content": "hello"}]
    assert response.message.content == "Hello"
    assert response.message.reasoning == "Greet."
    assert runtime.state.turn_usage["total_tokens"] == 4


def test_llm_client_runs_deepseek_sse_lifecycle() -> None:
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

    with pytest.raises(ModelRequestError, match="Model request failed") as exc_info:
        client.run(runtime)

    assert exc_info.value.diagnostics["provider"] == "deepseek"
    assert exc_info.value.diagnostics["request_outcome"] == "failed"


def test_llm_client_rejects_unknown_provider() -> None:
    config = ModelConfig("secret", "https://example.test/v1", "demo", provider="unknown")

    with pytest.raises(ModelConfigurationError, match="Unsupported model provider"):
        LLMClient(config)


def test_model_config_loads_provider_from_env_file(tmp_path, monkeypatch) -> None:
    for name in ("API_KEY", "BASE_URL", "MODEL", "MAX_TOKENS", "PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "API_KEY=secret\nBASE_URL=https://example.test/v1\nMODEL=demo\nPROVIDER=DEEPSEEK\n",
        encoding="utf-8",
    )

    config = ModelConfig.from_env(env_path)

    assert config.provider == "deepseek"


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def run(self, runtime):
        self.requests.append(runtime.exchange)
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


def test_agent_decision_explicitly_ends_previous_plan_mode() -> None:
    client = ScriptedClient([PreparedResponse(AssistantMessage(content="Ready to execute."))])
    planner = LLMPlanner(client, [], [])
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(
        task="Implement the plan",
        messages=[AssistantMessage(content="1. Make the change.")],
    )

    planner.decide(runtime)

    system = runtime.exchange.messages[0]
    assert isinstance(system, SystemMessage)
    assert "You are now in Agent mode" in (system.content or "")
    assert "previous Plan mode instructions" in (system.content or "")


def test_plan_decision_exposes_request_user_input_without_registering_it() -> None:
    client = ScriptedClient([PreparedResponse(AssistantMessage(content="1. Inspect the project."))])
    planner = LLMPlanner(client, [], [])
    registry = ToolRegistry()
    runtime = AgentRunner(planner, registry).new_runtime(task="Plan the change", mode="plan")

    planner.decide(runtime)

    assert registry.names() == []
    assert [spec.name for spec in runtime.exchange.allowed_tools] == ["request_user_input"]
    system = runtime.exchange.messages[0]
    assert isinstance(system, SystemMessage)
    assert "request_user_input" in (system.content or "")


def test_llm_planner_rejects_unknown_native_tool() -> None:
    client = ScriptedClient(
        [PreparedResponse(AssistantMessage(tool_messages=[ToolMessage(name="shell", call_id="call_1", arguments={})]))]
    )
    spec = ToolSpec("run_command", "Calculate")
    planner = LLMPlanner(client, [spec], [spec])
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task="run it")

    with pytest.raises(PlanningError, match="unavailable tool"):
        planner.decide(runtime)


def test_llm_plan_keeps_json_output_for_structured_operations() -> None:
    client = ScriptedClient(
        [
            PreparedResponse(
                AssistantMessage(
                    content=json.dumps(
                        {
                            "goal": "Calculate.",
                            "steps": [
                                {
                                    "id": "calculate",
                                    "description": "Calculate 2 + 2",
                                    "success_criteria": "Result is available",
                                    "tool": "run_command",
                                    "arguments": {"expression": "2 + 2"},
                                }
                            ],
                        }
                    )
                )
            )
        ]
    )
    spec = ToolSpec("run_command", "Calculate")
    planner = LLMPlanner(client, [spec], [spec])
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task="make a plan")

    plan = planner.create_plan(runtime)

    assert plan.steps[0].tool_message.name == "run_command"
    assert plan.steps[0].tool_message.arguments == {"expression": "2 + 2"}
    assert runtime.exchange.output_mode == "json"
    assert runtime.exchange.allowed_tools == []
    assert runtime.exchange.operation_tools == [spec]


def test_llm_empty_json_error_preserves_response_diagnostics() -> None:
    client = ScriptedClient(
        [
            PreparedResponse(
                AssistantMessage(reasoning="The response stopped before the JSON answer."),
                finish_reason="stop",
            )
        ]
    )
    planner = LLMPlanner(client, [], [])
    runtime = AgentRunner(planner, ToolRegistry()).new_runtime(task="make a plan")

    with pytest.raises(PlanningError, match="did not contain JSON content") as exc_info:
        planner.create_plan(runtime)

    assert exc_info.value.diagnostics == {
        "finish_reason": "stop",
        "content_chars": 0,
        "reasoning_chars": 44,
    }
