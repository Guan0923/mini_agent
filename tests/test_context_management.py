from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from backend.configuration import ClientPaths
from backend.domain import (
    CHECKPOINT_PREAMBLE,
    AssistantMessage,
    PlanningError,
    RunState,
    SystemMessage,
    ToolMessage,
    ToolSpec,
    UserMessage,
)
from backend.planning.context_management import ContextManager
from backend.planning.llm import LLMPlanner
from backend.planning.llm.requests import COMPACTION_INSTRUCTION
from backend.planning.rule_based import RuleBasedPlanner
from backend.providers import ChatCompletions, ModelConfig, ModelConfigurationError
from backend.runtime import AgentRunner, ConversationService
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.runtime.core.events import CHECKPOINT_EVENT_KINDS
from backend.storage.sqlite import SQLiteSessionStore
from backend.tools import ToolRegistry

STRUCTURED_CHECKPOINT = """## Primary Request and Intent
- Keep the exact user goal.

## Key Technical Concepts
- RuntimeState Turn tree

## Files and Code
- backend/src/planning/llm/requests.py: compaction prompt

## Errors and Fixes
- (none)

## Pending Jobs
- Run focused tests.

## Current Work
- Unifying manual and automatic compaction.

## Next Step
- Verify the Compaction Turn.

## Critical Context
- Preserve the last 8 Items."""


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
    runtime.exchange.context["test_events"] = []
    runtime.services.publish = runtime.exchange.context["test_events"].append
    return runtime


def runtime_events(runtime: AgentRuntime):
    return runtime.exchange.context["test_events"]


def test_context_manager_preserves_incomplete_messages_and_pending_tools() -> None:
    pending = ToolMessage(name="pending", call_id="call_pending")
    complete = ToolMessage(
        name="complete",
        call_id="call_complete",
        content="result",
        status="succeeded",
    )
    original = [
        UserMessage(content="  "),
        AssistantMessage(tool_messages=[pending, complete]),
        UserMessage(content="current"),
    ]
    runtime = runtime_for(original, turn_start_index=2)

    messages = ContextManager(FakeEstimator([20])).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda _transcript: "unused",
    )

    assert runtime.state.messages == original
    assert messages[1:] == original
    assert runtime.run.turn_start_index == 2
    assert not any(event.kind.startswith("context_compaction") for event in runtime_events(runtime))


def test_automatic_compaction_compresses_only_completed_history() -> None:
    runtime = runtime_for(
        [
            UserMessage(content="old question"),
            AssistantMessage(content="old answer"),
            UserMessage(content="current"),
        ],
        turn_start_index=2,
    )
    transcripts: list[str] = []

    messages = ContextManager(FakeEstimator([100, 20])).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda transcript: transcripts.append(transcript) or "old exchange summary",
    )

    assert transcripts and "old question" in transcripts[0] and "old answer" in transcripts[0]
    assert runtime.state.messages == [
        SystemMessage(name="context_summary", content=f"{CHECKPOINT_PREAMBLE}\n\nold exchange summary"),
        UserMessage(content="current"),
    ]
    assert runtime.run.turn_start_index == 1
    assert messages[1:] == runtime.state.messages
    assert [event.kind for event in runtime_events(runtime) if event.kind.startswith("context_compaction")] == [
        "context_compaction_started",
        "context_compaction_completed",
    ]
    completed = next(event for event in runtime_events(runtime) if event.kind == "context_compaction_completed")
    assert completed.data["trigger"] == "automatic"
    assert completed.data["estimated_tokens_after"] == 20
    assert completed.data["target_tokens"] == 80


def test_automatic_compaction_keeps_active_message_unchanged() -> None:
    runtime = runtime_for(
        [
            UserMessage(content="old"),
            AssistantMessage(content="answer"),
            UserMessage(content="current"),
        ],
        turn_start_index=2,
    )
    active = AssistantMessage(tool_messages=[ToolMessage(name="pending", call_id="call_pending")])
    runtime.state.active_message = active

    ContextManager(FakeEstimator([100, 20])).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda _transcript: "summary",
    )

    assert runtime.state.active_message is active
    assert active.tool_messages[0].status == "pending"
    assert runtime.state.messages[-1] == UserMessage(content="current")


def test_summary_failure_keeps_original_history_and_records_failure() -> None:
    original = [
        UserMessage(content="old"),
        AssistantMessage(content="answer"),
        UserMessage(content="current"),
    ]
    runtime = runtime_for(original, turn_start_index=2)

    with pytest.raises(PlanningError, match="failed"):
        ContextManager(FakeEstimator([100])).prepare(
            runtime,
            SystemMessage(content="system"),
            summarize=lambda _transcript: (_ for _ in ()).throw(PlanningError("failed")),
        )

    assert runtime.state.messages == original
    assert [event.kind for event in runtime_events(runtime) if event.kind.startswith("context_compaction")] == [
        "context_compaction_started",
        "context_compaction_failed",
    ]


def test_uncompressible_current_request_over_context_size_fails_without_deleting_context() -> None:
    original = [UserMessage(content="current")]
    runtime = runtime_for(original, turn_start_index=0)

    with pytest.raises(PlanningError, match="cannot be compacted before it finishes"):
        ContextManager(FakeEstimator([100])).prepare(
            runtime,
            SystemMessage(content="system"),
            summarize=lambda _transcript: "unused",
        )

    assert runtime.state.messages == original
    assert not any(event.kind.startswith("context_compaction") for event in runtime_events(runtime))


def test_manual_compaction_summarizes_all_finished_history_and_tool_results() -> None:
    succeeded = ToolMessage(name="search", call_id="call_success", content="found", status="succeeded")
    failed = ToolMessage(name="write", call_id="call_failed", content="denied", status="failed")
    indeterminate = ToolMessage(
        name="deploy",
        call_id="call_unknown",
        content="outcome unknown",
        status="indeterminate",
    )
    runtime = runtime_for(
        [
            UserMessage(content="old question"),
            AssistantMessage(tool_messages=[succeeded, failed, indeterminate]),
            UserMessage(content="latest question"),
            AssistantMessage(content="latest answer"),
        ],
        turn_start_index=2,
    )
    transcripts: list[str] = []

    result = ContextManager(FakeEstimator([20])).compact(
        runtime,
        summarize=lambda transcript: transcripts.append(transcript) or "durable summary",
    )

    assert result.compacted is True
    assert result.previous_messages == 4
    assert result.remaining_messages == 1
    assert transcripts and all(value in transcripts[0] for value in ("search", "found", "denied", "outcome unknown"))
    assert runtime.state.messages == [
        SystemMessage(name="context_summary", content=f"{CHECKPOINT_PREAMBLE}\n\ndurable summary"),
    ]
    assert runtime.run.turn_start_index == 1
    assert [event.kind for event in runtime_events(runtime) if event.kind.startswith("context_compaction")] == [
        "context_compaction_started",
        "context_compaction_completed",
    ]
    assert runtime_events(runtime)[-1].data["trigger"] == "manual"


def test_manual_compaction_failure_leaves_history_unchanged() -> None:
    original = [
        UserMessage(content="old question"),
        AssistantMessage(content="old answer"),
        UserMessage(content="latest question"),
    ]
    runtime = runtime_for(original, turn_start_index=2)

    with pytest.raises(PlanningError, match="summary failed"):
        ContextManager(FakeEstimator([20])).compact(
            runtime,
            summarize=lambda _transcript: (_ for _ in ()).throw(PlanningError("summary failed")),
        )

    assert runtime.state.messages == original
    assert [event.kind for event in runtime_events(runtime) if event.kind.startswith("context_compaction")] == [
        "context_compaction_started",
        "context_compaction_failed",
    ]


def test_candidate_over_context_target_keeps_history_unchanged() -> None:
    original = [UserMessage(content="old"), AssistantMessage(content="answer")]
    runtime = runtime_for(original, turn_start_index=2)

    with pytest.raises(PlanningError, match="configured context target"):
        ContextManager(FakeEstimator([100, 81])).compact(runtime, summarize=lambda _transcript: "too large")

    assert runtime.state.messages == original
    failed = runtime_events(runtime)[-1]
    assert failed.kind == "context_compaction_failed"
    assert failed.data["target_tokens"] == 80


class FakeEncoding:
    def __init__(self, length: int) -> None:
        self.ids = list(range(length))


class RecordingTokenizer:
    def __init__(self) -> None:
        self.values: list[str] = []

    def encode(self, value: str) -> FakeEncoding:
        self.values.append(value)
        return FakeEncoding(len(value))


def test_chat_completions_token_estimator_counts_wire_messages_tools_and_output_reserve() -> None:
    tokenizer = RecordingTokenizer()
    loaded: list[str] = []
    chat_completions = ChatCompletions(
        ModelConfig(
            "secret",
            "https://example.test/v1",
            "demo",
            max_tokens=7,
            tokenizer_model="custom-tokenizer",
        ),
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

    first = chat_completions.estimate_tokens(messages, tools, {})
    second = chat_completions.estimate_tokens(messages, tools, {"max_tokens": 3})

    assert loaded == ["custom-tokenizer"]
    assert first == len(tokenizer.values[0]) + 7
    assert second == len(tokenizer.values[1]) + 3
    serialized = tokenizer.values[0]
    assert "hello 中文" in serialized
    assert "tool result" in serialized
    assert "Look up text" in serialized
    arguments = json.loads(serialized)["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {"query": "中文"}


def test_chat_completions_tokenizer_load_failure_is_actionable() -> None:
    def fail(_identifier: str):
        raise OSError("offline")

    chat_completions = ChatCompletions(
        ModelConfig("secret", "https://example.test/v1", "demo", tokenizer_model="custom-tokenizer"),
        tokenizer_loader=fail,
    )

    with pytest.raises(ModelConfigurationError, match="offline"):
        chat_completions.estimate_tokens([UserMessage(content="hello")], [], {})


class ContextAwareClient:
    context_size = 100

    def __init__(self) -> None:
        self.operations: list[tuple[str | None, bool]] = []
        self.estimates = [100, 20]
        self.summary_prompt: str | None = None

    def estimate_tokens(self, messages, tools, request_parameters) -> int:
        if len(self.estimates) > 1:
            return self.estimates.pop(0)
        return self.estimates[0]

    def run(self, runtime: AgentRuntime) -> PreparedResponse:
        self.operations.append((runtime.exchange.operation, runtime.exchange.stream))
        if runtime.exchange.operation == "summarize":
            self.summary_prompt = runtime.exchange.messages[0].content
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
    assert client.summary_prompt is not None and "acting as a compaction engine" in client.summary_prompt
    assert runtime.state.turn_usage == {"total_tokens": 9}


def test_compaction_instruction_requires_every_checkpoint_section_in_order() -> None:
    headings = [
        "## Primary Request and Intent",
        "## Key Technical Concepts",
        "## Files and Code",
        "## Errors and Fixes",
        "## Pending Jobs",
        "## Current Work",
        "## Next Step",
        "## Critical Context",
    ]

    positions = [COMPACTION_INSTRUCTION.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert 'Write "(none)" for an empty section' in COMPACTION_INSTRUCTION
    assert CHECKPOINT_PREAMBLE in COMPACTION_INSTRUCTION


class StructuredCompactionClient:
    context_size = 100_000

    def __init__(self) -> None:
        self.operations: list[str | None] = []

    def estimate_tokens(self, messages, tools, request_parameters) -> int:
        return 100

    def run(self, runtime: AgentRuntime) -> PreparedResponse:
        self.operations.append(runtime.exchange.operation)
        content = STRUCTURED_CHECKPOINT if runtime.exchange.operation == "summarize" else "initial answer"
        return PreparedResponse(AssistantMessage(content=content), {"total_tokens": 1})


def test_conversation_compact_turn_uses_llm_summary_and_finalizes_exact_source(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    client = StructuredCompactionClient()
    service = ConversationService(
        AgentRunner(LLMPlanner(client, [], []), ToolRegistry(tmp_path / "workspace")),
        store,
    )

    completed = service.run_task("preserve this request", mode="agent")
    assert completed.status == "completed"
    assert service.active_session is not None
    source = store.load_nodes(service.active_session.session_id)[-1]

    compacted = service.compact_turn(source.id, "turn_structured_compact")

    assert client.operations == ["decision", "summarize"]
    assert compacted.id == compacted.compaction_id == "turn_structured_compact"
    assert compacted.parent_id == source.id
    assert compacted.status == "success"
    assert compacted.assistant_items[0] == {
        "type": "compaction",
        "summary": STRUCTURED_CHECKPOINT,
        "kept_item_count": 2,
        "status": "success",
    }
    assert CHECKPOINT_PREAMBLE not in compacted.assistant_items[0]["summary"]
    assert service.runtime is not None
    projected = service.runtime.model_messages()
    assert any(
        isinstance(message, UserMessage) and message.content == f"{CHECKPOINT_PREAMBLE}\n\n{STRUCTURED_CHECKPOINT}"
        for message in projected
    )


def test_rule_planner_rejects_manual_context_compaction() -> None:
    runner = AgentRunner(RuleBasedPlanner(), ToolRegistry())
    runtime = runner.empty_runtime(session_id="session_rule")
    runtime.state.messages = [UserMessage(content="latest")]
    runtime.state.current_run = RunState(
        task="latest",
        mode="agent",
        history=runtime.state.messages,
        turn_start_index=0,
        status="completed",
    )

    with pytest.raises(PlanningError, match="requires the LLM planner"):
        runner.compact_context(runtime)

    assert runtime.state.messages == [UserMessage(content="latest")]

    runtime.state.current_run = None
    with pytest.raises(PlanningError, match="requires the LLM planner"):
        runner.compact_context(runtime)

    assert runtime.state.messages == [UserMessage(content="latest")]

    runtime.state.current_run = RunState(task="active", mode="agent", history=runtime.state.messages)
    runtime.state.status = "running"
    with pytest.raises(RuntimeError, match="Current turn is still running"):
        runner.compact_context(runtime)


def test_compaction_events_are_checkpointed() -> None:
    assert {
        "context_compaction_started",
        "context_compaction_completed",
        "context_compaction_failed",
    } <= CHECKPOINT_EVENT_KINDS


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
            "TOKENIZER_MODEL": "custom-tokenizer",
        },
    )

    assert default.context_size == 1_024_000
    assert default.tokenizer_model == ""
    assert configured.context_size == 2_048_000
    assert configured.tokenizer_model == "custom-tokenizer"


def test_context_manager_publishes_usage_before_and_after_compression() -> None:
    runtime = runtime_for(
        [
            UserMessage(content="old"),
            AssistantMessage(content="answer"),
            UserMessage(content="current"),
        ],
        turn_start_index=2,
    )
    events = []
    runtime.services.publish = events.append

    ContextManager(FakeEstimator([100, 20])).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda _transcript: "summary",
    )

    usage = [event for event in events if event.kind == "context_usage"]
    assert [(event.data["phase"], event.data["estimated_tokens"]) for event in usage] == [
        ("before_compaction", 100),
        ("after_compaction", 20),
    ]
    assert all(event.data["target_tokens"] == 80 for event in usage)


def test_context_manager_uses_input_estimate_without_output_reservation() -> None:
    class SplitEstimator:
        context_size = 100

        def estimate_tokens(self, _messages, _tools, _parameters) -> int:
            return 95

        def estimate_input_tokens(self, _messages, _tools, _parameters) -> int:
            return 90

    runtime = runtime_for([UserMessage(content="current")], turn_start_index=0)
    events = []
    runtime.services.publish = events.append

    ContextManager(SplitEstimator()).prepare(
        runtime,
        SystemMessage(content="system"),
        summarize=lambda _transcript: "unused",
    )

    assert not any(event.kind.startswith("context_compaction") for event in runtime_events(runtime))
    usage = next(event for event in events if event.kind == "context_usage")
    assert usage.data["input_tokens"] == 90
    assert usage.data["estimated_tokens"] == 90
    assert runtime.exchange.context["estimated_input_tokens"] == 90
