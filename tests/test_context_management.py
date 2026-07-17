from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from mini_agent.domain import (
    AssistantMessage,
    PlanningError,
    RunState,
    SystemMessage,
    ToolMessage,
    ToolSpec,
    UserMessage,
)
from mini_agent.planning.context_management import ContextManager
from mini_agent.planning.llm import LLMPlanner
from mini_agent.providers import DeepSeek, ModelConfig, ModelConfigurationError
from mini_agent.runtime.context import AgentRuntime, PreparedResponse


@dataclass
class FakeEstimator:
    counts: list[int]
    context_size: int = 100

    def estimate_tokens(self, messages, tools, request_parameters) -> int:
        if len(self.counts) > 1:
            return self.counts.pop(0)
        return self.counts[0]


def runtime_for(messages, *, turn_start_index: int) -> AgentRuntime:
    runtime = AgentRuntime.ephemeral(
        session_id="session_context",
        planner=object(),
        tools=object(),
        messages=list(messages),
    )
    runtime.state.current_run = RunState(
        task="current",
        mode="agent",
        history=runtime.state.messages,
        turn_start_index=turn_start_index,
    )
    return runtime


def test_context_manager_removes_incomplete_messages_and_pending_tools() -> None:
    pending = ToolMessage(name="pending", call_id="call_pending")
    complete = ToolMessage(
        name="complete",
        call_id="call_complete",
        content="result",
        status="succeeded",
    )
    runtime = runtime_for(
        [
            UserMessage(content="  "),
            AssistantMessage(tool_messages=[pending, complete]),
            UserMessage(content="current"),
        ],
        turn_start_index=2,
    )
    events = []
    runtime.services.publish = events.append

    messages = ContextManager(FakeEstimator([20])).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda _transcript: "unused",
    )

    assert [message.role for message in runtime.state.messages] == ["assistant", "user"]
    assistant = runtime.state.messages[0]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.tool_messages == [complete]
    assert runtime.run.turn_start_index == 1
    assert messages[1:] == runtime.state.messages
    assert events[-1].kind == "context_cleaned"
    assert events[-1].data["removed_messages"] == 1
    assert events[-1].data["removed_tool_calls"] == 1


def test_provider_total_tokens_trigger_compression_and_keep_current_run() -> None:
    runtime = runtime_for(
        [
            UserMessage(content="old question"),
            AssistantMessage(content="old answer"),
            UserMessage(content="current"),
        ],
        turn_start_index=2,
    )
    runtime.state.usage = {"total_tokens": 80}
    transcripts: list[str] = []

    messages = ContextManager(FakeEstimator([20, 15])).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda transcript: transcripts.append(transcript) or "old exchange summary",
    )

    assert transcripts and "old question" in transcripts[0] and "old answer" in transcripts[0]
    assert isinstance(runtime.state.messages[0], SystemMessage)
    assert runtime.state.messages[0].name == "context_summary"
    assert runtime.state.messages[1] == UserMessage(content="current")
    assert runtime.run.turn_start_index == 1
    assert messages[1:] == runtime.state.messages
    compressed = next(event for event in runtime.run.events if event.kind == "context_compressed")
    assert compressed.data["provider_total_tokens"] == 80


def test_local_token_estimate_triggers_compression_with_tool_results() -> None:
    tool = ToolMessage(
        name="read_file",
        call_id="call_1",
        arguments={"path": "README.md"},
        content="important result",
        status="succeeded",
    )
    runtime = runtime_for(
        [
            UserMessage(content="inspect"),
            AssistantMessage(tool_messages=[tool]),
            UserMessage(content="current"),
        ],
        turn_start_index=2,
    )
    transcripts: list[str] = []

    ContextManager(FakeEstimator([80, 20])).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda transcript: transcripts.append(transcript) or "inspection summary",
    )

    assert "read_file" in transcripts[0]
    assert "README.md" in transcripts[0]
    assert "important result" in transcripts[0]


def test_summary_failure_keeps_original_history() -> None:
    original = [
        UserMessage(content="old"),
        AssistantMessage(content="answer"),
        UserMessage(content="current"),
    ]
    runtime = runtime_for(original, turn_start_index=2)

    messages = ContextManager(FakeEstimator([80])).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda _transcript: (_ for _ in ()).throw(PlanningError("failed")),
    )

    assert runtime.state.messages == original
    assert messages[1:] == original
    assert not any(event.kind == "context_compressed" for event in runtime.run.events)


def test_uncompressible_current_request_over_context_size_fails() -> None:
    runtime = runtime_for([UserMessage(content="current")], turn_start_index=0)

    with pytest.raises(PlanningError, match="exceeds the model context window"):
        ContextManager(FakeEstimator([100])).prepare(
            runtime,
            SystemMessage(content="system"),
            summarize=lambda _transcript: "unused",
        )


def test_repeated_compression_merges_existing_summary() -> None:
    runtime = runtime_for(
        [
            SystemMessage(name="context_summary", content="[Conversation summary]\nfirst summary"),
            UserMessage(content="follow-up"),
            AssistantMessage(content="follow-up answer"),
            UserMessage(content="current"),
        ],
        turn_start_index=3,
    )
    transcripts: list[str] = []

    ContextManager(FakeEstimator([80, 20])).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda transcript: transcripts.append(transcript) or "merged summary",
    )

    assert "first summary" in transcripts[0]
    assert "follow-up answer" in transcripts[0]
    assert runtime.state.messages[0] == SystemMessage(
        name="context_summary",
        content="[Conversation summary]\nmerged summary",
    )
    assert runtime.state.messages[1] == UserMessage(content="current")


def test_compression_that_still_exceeds_context_size_fails() -> None:
    runtime = runtime_for(
        [
            UserMessage(content="old"),
            AssistantMessage(content="answer"),
            UserMessage(content="current"),
        ],
        turn_start_index=2,
    )

    with pytest.raises(PlanningError, match="exceeds the model context window"):
        ContextManager(FakeEstimator([100, 100])).prepare(
            runtime,
            SystemMessage(content="system"),
            summarize=lambda _transcript: "summary",
        )

    assert isinstance(runtime.state.messages[0], SystemMessage)


def test_run_state_round_trips_turn_start_index_and_legacy_defaults_to_zero() -> None:
    restored = RunState.from_dict(RunState(task="task", mode="agent", turn_start_index=4).to_dict())
    legacy = RunState.from_dict({"task": "legacy", "mode": "agent", "run_id": "run_legacy"})

    assert restored.turn_start_index == 4
    assert legacy.turn_start_index == 0


class FakeEncoding:
    def __init__(self, length: int) -> None:
        self.ids = list(range(length))


class RecordingTokenizer:
    def __init__(self) -> None:
        self.values: list[str] = []

    def encode(self, value: str) -> FakeEncoding:
        self.values.append(value)
        return FakeEncoding(len(value))


def test_deepseek_token_estimator_counts_wire_messages_tools_and_output_reserve() -> None:
    tokenizer = RecordingTokenizer()
    loaded: list[str] = []
    deepseek = DeepSeek(
        ModelConfig("secret", "https://example.test/v1", "demo", max_tokens=7),
        tokenizer_loader=lambda identifier: loaded.append(identifier) or tokenizer,
    )
    tool_message = ToolMessage(
        name="lookup",
        call_id="call_1",
        arguments={"query": "中文"},
        content="tool result",
        status="succeeded",
    )
    messages = [
        UserMessage(content="hello 中文"),
        AssistantMessage(tool_messages=[tool_message]),
    ]
    tools = [ToolSpec("lookup", "Look up text", {"type": "object", "properties": {"query": {"type": "string"}}})]

    first = deepseek.estimate_tokens(messages, tools, {})
    second = deepseek.estimate_tokens(messages, tools, {"max_tokens": 3})

    assert loaded == ["deepseek-ai/DeepSeek-V3"]
    assert first == len(tokenizer.values[0]) + 7
    assert second == len(tokenizer.values[1]) + 3
    serialized = tokenizer.values[0]
    assert "hello 中文" in serialized
    assert "tool result" in serialized
    assert "Look up text" in serialized
    arguments = json.loads(serialized)["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {"query": "中文"}


def test_deepseek_tokenizer_load_failure_is_actionable() -> None:
    def fail(_identifier: str):
        raise OSError("offline")

    deepseek = DeepSeek(
        ModelConfig("secret", "https://example.test/v1", "demo"),
        tokenizer_loader=fail,
    )

    with pytest.raises(ModelConfigurationError, match="TOKENIZER_MODEL"):
        deepseek.estimate_tokens([UserMessage(content="hello")], [], {})


class ContextAwareClient:
    context_size = 100

    def __init__(self) -> None:
        self.operations: list[tuple[str | None, bool]] = []

    def estimate_tokens(self, messages, tools, request_parameters) -> int:
        return 20

    def run(self, runtime: AgentRuntime) -> PreparedResponse:
        self.operations.append((runtime.exchange.operation, runtime.exchange.stream))
        if runtime.exchange.operation == "summarize":
            runtime.state.turn_usage = {"total_tokens": 5}
            return PreparedResponse(AssistantMessage(content="compressed history"), {"total_tokens": 5})
        runtime.state.turn_usage = {"total_tokens": 9}
        return PreparedResponse(AssistantMessage(content="final"), {"total_tokens": 9})


def test_llm_planner_runs_non_streaming_summary_before_normal_request() -> None:
    client = ContextAwareClient()
    planner = LLMPlanner(client, [], [])
    runtime = runtime_for(
        [
            UserMessage(content="old"),
            AssistantMessage(content="answer"),
            UserMessage(content="current"),
        ],
        turn_start_index=2,
    )
    runtime.services.planner = planner
    runtime.state.usage = {"total_tokens": 80}
    runtime.exchange.on_reasoning = lambda _chunk: None

    response = planner.decide(runtime)

    assert response.content == "final"
    assert client.operations == [("summarize", False), ("decision", True)]
    assert runtime.state.turn_usage == {"total_tokens": 9}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"CONTEXT_SIZE": "not-a-number"}, "CONTEXT_SIZE must be an integer"),
        ({"CONTEXT_SIZE": "8192"}, "greater than MAX_TOKENS"),
        ({"TOKENIZER_MODEL": " "}, "TOKENIZER_MODEL must not be empty"),
    ],
)
def test_model_config_rejects_invalid_context_settings(
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
) -> None:
    environment = {
        "API_KEY": "secret",
        "BASE_URL": "https://example.test/v1",
        "MODEL": "demo",
        **overrides,
    }

    with pytest.raises(ModelConfigurationError, match=message):
        ModelConfig.from_env(tmp_path / ".env", environ=environment)


def test_model_config_loads_context_defaults_and_overrides(tmp_path: Path) -> None:
    default = ModelConfig.from_env(
        tmp_path / ".env",
        environ={
            "API_KEY": "secret",
            "BASE_URL": "https://example.test/v1",
            "MODEL": "demo",
        },
    )
    configured = ModelConfig.from_env(
        tmp_path / ".env",
        environ={
            "API_KEY": "secret",
            "BASE_URL": "https://example.test/v1",
            "MODEL": "demo",
            "CONTEXT_SIZE": "2048000",
            "TOKENIZER_MODEL": "deepseek-ai/custom-tokenizer",
        },
    )

    assert default.context_size == 1_024_000
    assert default.tokenizer_model == "deepseek-ai/DeepSeek-V3"
    assert configured.context_size == 2_048_000
    assert configured.tokenizer_model == "deepseek-ai/custom-tokenizer"
