from __future__ import annotations

from backend.domain import AssistantMessage, ToolMessage, UserMessage
from backend.planning import RuleBasedPlanner
from backend.providers import ChatCompletionsAdapter, MessagesAdapter, ModelConfig, ResponsesAdapter
from backend.runtime import AgentRunner
from backend.runtime.core.contracts import InterruptDecision
from backend.tools import Tool, ToolRegistry


class DenialThenAnswerPlanner:
    name = "denial-then-answer"

    def __init__(self, tool_names: list[str]) -> None:
        self.tool_names = tool_names
        self.calls = 0
        self.feedback: list[ToolMessage] = []

    def decide(self, runtime) -> AssistantMessage:
        self.calls += 1
        if self.calls == 1:
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(name=name, call_id=f"call_{index}", arguments={})
                    for index, name in enumerate(self.tool_names)
                ]
            )
        previous = runtime.state.messages[-1]
        assert isinstance(previous, AssistantMessage)
        self.feedback = list(previous.tool_messages)
        return AssistantMessage(content="The requested tool call was not executed.")


def _run_denied_tools(tool_names: list[str]):
    invocations: list[str] = []
    decisions: list[str] = []
    planner = DenialThenAnswerPlanner(tool_names)
    tools = ToolRegistry(
        [
            Tool(
                name,
                f"Test {name}.",
                lambda _name=name, **_arguments: invocations.append(_name) or "executed",
                requires_confirmation=True,
                read_only=False,
            )
            for name in tool_names
        ]
    )
    runner = AgentRunner(planner, tools)

    def deny(request):
        decisions.append(request.data["tool"])
        return InterruptDecision("deny")

    runtime = runner.new_runtime(task="use a tool", interrupt=deny)
    state = runner.run(runtime)
    return runtime, state, planner, invocations, decisions


def test_denied_write_file_is_returned_to_the_model_without_ending_the_run() -> None:
    runtime, state, planner, invocations, decisions = _run_denied_tools(["write_file"])

    assert state.status == "completed"
    assert state.final_answer == "The requested tool call was not executed."
    assert planner.calls == 2
    assert invocations == []
    assert decisions == ["write_file"]
    denied = planner.feedback[0]
    assert denied.status == "failed"
    assert denied.retryable is False
    assert denied.failure_code == "user_denied"
    assert denied.content == "The user denied this write_file tool call."
    assert not any(event.kind == "cancelled" for event in state.events)
    failure = next(event for event in state.events if event.kind == "tool_failed")
    assert failure.data == {
        "tool": "write_file",
        "call_id": "call_0",
        "error": "The user denied this write_file tool call.",
        "failure_code": "user_denied",
    }
    persisted = type(runtime.state).from_dict(runtime.state.to_dict())
    persisted_denial = next(
        message.tool_messages[0]
        for message in persisted.messages
        if isinstance(message, AssistantMessage) and message.tool_messages
    )
    assert persisted_denial.failure_code == "user_denied"
    assert persisted_denial.content == "The user denied this write_file tool call."


def test_denied_tool_message_uses_the_dynamic_english_template() -> None:
    _, state, planner, invocations, decisions = _run_denied_tools(["run_command"])

    assert state.status == "completed"
    assert invocations == []
    assert decisions == ["run_command"]
    assert planner.feedback[0].content == "The user denied this run_command tool call."


def test_plan_mode_tool_denial_continues_the_plan_response() -> None:
    planner = DenialThenAnswerPlanner(["web_fetch"])
    tools = ToolRegistry(
        [
            Tool(
                "web_fetch",
                "Fetch a page.",
                lambda **_arguments: "executed",
                requires_confirmation=True,
                read_only=True,
            )
        ]
    )
    runner = AgentRunner(planner, tools)
    runtime = runner.new_runtime(
        task="plan a change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("deny"),
    )

    state = runner.run(runtime)

    assert state.status == "completed"
    assert state.final_answer == "The requested tool call was not executed."
    assert planner.feedback[0].content == "The user denied this web_fetch tool call."


def _provider_payloads(messages):
    config = ModelConfig("secret", "https://example.test/v1", "demo")
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="continue")
    runtime.state.model = "demo"
    runtime.state.request_parameters = {"max_tokens": 128}
    runtime.exchange.messages = messages
    return (
        ChatCompletionsAdapter(config).prepare_request(runtime),
        ResponsesAdapter(config).prepare_request(runtime),
        MessagesAdapter(config).prepare_request(runtime),
    )


def test_denial_stops_the_tool_batch_and_all_provider_pairs_remain_valid() -> None:
    _, state, planner, invocations, decisions = _run_denied_tools(["write_file", "run_command"])

    assert state.status == "completed"
    assert invocations == []
    assert decisions == ["write_file"]
    assert [tool.status for tool in planner.feedback] == ["failed", "failed"]
    assert [tool.retryable for tool in planner.feedback] == [False, False]
    assert [tool.failure_code for tool in planner.feedback] == ["user_denied", "user_denied_batch"]
    assert [tool.content for tool in planner.feedback] == [
        "The user denied this write_file tool call.",
        "Not executed because tool execution was interrupted.",
    ]

    history = [UserMessage(content="use tools"), AssistantMessage(tool_messages=planner.feedback)]
    chat, responses, messages = _provider_payloads(history)

    chat_calls = [call for item in chat["messages"] for call in item.get("tool_calls", [])]
    chat_results = [item for item in chat["messages"] if item.get("role") == "tool"]
    response_calls = [item for item in responses["input"] if item.get("type") == "function_call"]
    response_results = [item for item in responses["input"] if item.get("type") == "function_call_output"]
    message_calls = [
        block
        for item in messages["messages"]
        if item["role"] == "assistant"
        for block in item["content"]
        if block["type"] == "tool_use"
    ]
    message_results = [
        block
        for item in messages["messages"]
        if item["role"] == "user"
        for block in item["content"]
        if block["type"] == "tool_result"
    ]

    assert [call["id"] for call in chat_calls] == ["call_0", "call_1"]
    assert [item["tool_call_id"] for item in chat_results] == ["call_0", "call_1"]
    assert [call["call_id"] for call in response_calls] == ["call_0", "call_1"]
    assert [item["call_id"] for item in response_results] == ["call_0", "call_1"]
    assert [call["id"] for call in message_calls] == ["call_0", "call_1"]
    assert [item["tool_use_id"] for item in message_results] == ["call_0", "call_1"]
    assert chat_results[0]["content"] == "The user denied this write_file tool call."
    assert response_results[0]["output"] == "The user denied this write_file tool call."
    assert message_results[0]["content"] == "The user denied this write_file tool call."
