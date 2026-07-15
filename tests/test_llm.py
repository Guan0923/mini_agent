import json

import pytest

from mini_agent.planning import LLMPlanner, PlanningError
from mini_agent.providers import ChatCompletionsClient, DeepSeekChatCompletions, DeepSeekStreamDelta, ModelConfig, ModelRequestError


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "type": "tool_call",
                                "tool": "calculator",
                                "arguments": {"expression": "2 + 2"},
                            }
                        ),
                        "reasoning_content": "The user requested arithmetic.",
                    },
                    "finish_reason": "stop",
                },
            ],
            "id": "chatcmpl-test",
            "model": "deepseek-v4-flash",
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
                "completion_tokens_details": {"reasoning_tokens": 4},
            },
        }


class FakeSession:
    def __init__(self) -> None:
        self.request: dict | None = None

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.request = {"url": url, **kwargs}
        return FakeResponse()


class FakeStreamResponse(FakeResponse):
    def iter_lines(self, decode_unicode: bool = False) -> list[str]:
        return [
            'data: {"choices":[{"delta":{"reasoning_content":"Think ","content":"{\\"type\\": "}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"now.","content":"\\"final_answer\\",\\"answer\\":\\"Hi\\"}"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]

    def close(self) -> None:
        return None


class StreamSession:
    def post(self, url: str, **kwargs: object) -> FakeStreamResponse:
        assert kwargs["stream"] is True
        return FakeStreamResponse()


def test_deepseek_client_builds_and_parses_chat_completion() -> None:
    session = FakeSession()
    http_client = ChatCompletionsClient(session=session)
    client = DeepSeekChatCompletions(ModelConfig("secret", "https://example.test/v1", "demo"), client=http_client)

    completion = client.create([{"role": "user", "content": "hello"}])
    assert session.request is not None
    assert session.request["url"] == "https://example.test/v1/chat/completions"
    assert session.request["json"]["model"] == "demo"
    assert session.request["json"]["response_format"] == {"type": "json_object"}
    assert session.request["json"]["max_tokens"] == 8192
    assert completion.reasoning_content == "The user requested arithmetic."
    assert completion.usage is not None
    assert completion.usage.reasoning_tokens == 4
    assert client.complete_with_reasoning([{"role": "user", "content": "hello"}]) == (
        '{"type": "tool_call", "tool": "calculator", "arguments": {"expression": "2 + 2"}}',
        "The user requested arithmetic.",
    )


def test_llm_plan_is_validated() -> None:
    client = DeepSeekChatCompletions(
        ModelConfig("secret", "https://example.test", "demo"),
        client=ChatCompletionsClient(session=FakeSession()),
    )
    planner = LLMPlanner(client, ["calculator"], ["calculator"])
    action = planner.decide([{"role": "user", "content": "2 + 2"}], "agent")

    assert action.tool == "calculator"
    assert action.arguments == {"expression": "2 + 2"}
    assert action.reasoning == "The user requested arithmetic."


def test_deepseek_stream_yields_reasoning_and_content() -> None:
    client = DeepSeekChatCompletions(
        ModelConfig("secret", "https://example.test", "demo"),
        client=ChatCompletionsClient(session=StreamSession()),
    )

    deltas = list(client.stream_with_reasoning([{"role": "user", "content": "hello"}]))

    assert "".join(delta.reasoning_content or "" for delta in deltas) == "Think now."
    assert "".join(delta.content or "" for delta in deltas) == '{"type": "final_answer","answer":"Hi"}'
    assert deltas[-1].finish_reason == "stop"


def test_llm_plan_rejects_unknown_tool() -> None:
    planner = LLMPlanner.__new__(LLMPlanner)
    planner.tool_names = {"calculator"}
    planner.read_only_tool_names = {"calculator"}
    with pytest.raises(PlanningError, match="unavailable tool"):
        planner._parse_action('{"type":"tool_call","tool":"shell","arguments":{}}', {"calculator"})


def test_llm_execution_plan_is_validated() -> None:
    planner = LLMPlanner.__new__(LLMPlanner)
    plan = planner._parse_execution_plan(
        json.dumps(
            {
                "goal": "Calculate a value.",
                "steps": [
                    {
                        "id": "calculate",
                        "description": "Calculate 2 + 2",
                        "success_criteria": "The result is available.",
                        "tool": "calculator",
                        "arguments": {"expression": "2 + 2"},
                    }
                ],
            }
        ),
        {"calculator"},
    )

    assert plan.goal == "Calculate a value."
    assert plan.steps[0].action.arguments == {"expression": "2 + 2"}


def test_llm_strategy_selection_is_validated() -> None:
    selection = LLMPlanner._parse_strategy_selection(
        json.dumps({"strategy": "plan_execute", "reason": "The steps are concrete and independent."})
    )

    assert selection.strategy == "plan_execute"
    assert selection.reason == "The steps are concrete and independent."


def test_llm_step_evaluation_is_validated() -> None:
    evaluation = LLMPlanner._parse_step_evaluation(
        json.dumps({"decision": "replan", "reason": "The result invalidates the next step."})
    )

    assert evaluation.decision == "replan"
    assert evaluation.reason == "The result invalidates the next step."


class LengthLimitedStreamClient:
    def stream_with_reasoning(self, messages):
        yield DeepSeekStreamDelta(content='{"goal":', reasoning_content="Thinking", finish_reason="length")


def test_llm_reports_a_length_limited_json_stream() -> None:
    planner = LLMPlanner(LengthLimitedStreamClient(), ["calculator"], ["calculator"])

    with pytest.raises(ModelRequestError, match="max_tokens"):
        planner.create_plan([{"role": "user", "content": "calculate 2 + 2"}], "agent", on_reasoning=lambda _: None)


class DynamicPlanClient:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] | None = None

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.messages = messages
        return json.dumps(
            {
                "goal": "Inspect the project.",
                "steps": [
                    {
                        "id": "inspect",
                        "description": "List files",
                        "success_criteria": "Files are listed.",
                        "tool": "calculator",
                        "arguments": {"expression": "2 + 2"},
                    }
                ],
            }
        )


def test_dynamic_planning_uses_a_stage_prompt() -> None:
    client = DynamicPlanClient()
    planner = LLMPlanner(client, ["calculator"], ["calculator"])

    planner.create_dynamic_plan([{"role": "user", "content": "inspect first"}], "agent")

    assert client.messages is not None
    assert "first executable phase" in client.messages[0]["content"]


class StrategySelectionClient:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] | None = None

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.messages = messages
        return json.dumps({"strategy": "reactive", "reason": "The task is a direct calculation."})


def test_llm_selects_execution_strategy_before_running_a_workflow() -> None:
    client = StrategySelectionClient()
    planner = LLMPlanner(client, ["calculator"], ["calculator"])

    selection = planner.select_strategy([{"role": "user", "content": "calculate 2 + 2"}], "agent")

    assert selection.strategy == "reactive"
    assert client.messages is not None
    assert "route tasks" in client.messages[0]["content"]
