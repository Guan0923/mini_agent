import json
from pathlib import Path

import pytest

from backend.domain import AssistantMessage, PlanningError, ToolMessage
from backend.observability import EventFanout, JsonlRunLogger
from backend.planning import LLMPlanner, RuleBasedPlanner
from backend.runtime import (
    AgentRunner,
    ConversationService,
    PostgresCheckpointStore,
    PostgresSessionStore,
    PreparedResponse,
)
from backend.runtime.core.config import database_url_from_env, log_full_messages_from_env
from backend.runtime.execution.lifecycle.outcomes import fail_run
from backend.tools import Tool, ToolRegistry


class StaticCompletionClient:
    def __init__(self, content: str = "model answer", provider_options: dict | None = None) -> None:
        self.content = content
        self.provider_options = provider_options or {}

    def run(self, runtime) -> PreparedResponse:
        return PreparedResponse(
            AssistantMessage(content=self.content, provider_options=self.provider_options),
            usage={"total_tokens": 3},
            response_id="response_1",
            model="test-model",
            finish_reason="stop",
        )


class SequencedCompletionClient:
    def __init__(self, contents: list[str]) -> None:
        self._contents = iter(contents)

    def run(self, runtime) -> PreparedResponse:
        return PreparedResponse(AssistantMessage(content=next(self._contents)))


class ReasoningPlanner:
    name = "reasoning"

    def decide(self, runtime) -> AssistantMessage:
        return AssistantMessage(content="done", reasoning="first thought; second thought")


class OneToolThenAnswerPlanner:
    name = "one-tool-then-answer"

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, runtime) -> AssistantMessage:
        self.calls += 1
        if self.calls == 1:
            return AssistantMessage(
                content="I will use a tool.",
                reasoning="I need the tool result.",
                tool_messages=[ToolMessage(name="echo", call_id="call_echo", arguments={"value": "ok"})],
            )
        return AssistantMessage(content="done")


class ExplodingPlanner:
    name = "exploding"

    def decide(self, runtime) -> AssistantMessage:
        raise RuntimeError("unexpected failure")


class FailingCompletionClient:
    def run(self, runtime) -> PreparedResponse:
        raise PlanningError("provider token=visible-secret failed")


def test_session_runtime_messages_survive_restart_and_match_run_state(tmp_path: Path) -> None:
    store = PostgresSessionStore()
    service = ConversationService(AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)), store)

    state = service.run_task("calculate 2 + 2", mode="agent")

    assert service.active_session is not None
    reopened = PostgresSessionStore()
    messages = reopened.load_runtime_messages(service.active_session.session_id, state.run_id)

    assert messages == state.runtime_messages
    assert [message.sequence for message in messages] == list(range(1, len(messages) + 1))
    assert {"run_started", "response", "response", "response", "run_finished"} <= {message.kind for message in messages}
    assert reopened.load_conversation(service.active_session.session_id) == [
        {"role": "user", "content": "calculate 2 + 2"},
        {
            "role": "assistant",
            "content": "Hello! I can help with web search, file operations, and running commands in the workspace.",
        },
    ]


def test_unexpected_failure_still_ends_the_persisted_runtime_trace(tmp_path: Path) -> None:
    store = PostgresSessionStore()
    service = ConversationService(AgentRunner(ExplodingPlanner(), ToolRegistry(), strategy="reactive"), store)

    with pytest.raises(RuntimeError, match="unexpected failure"):
        service.run_task("fail", mode="agent")

    assert service.active_session is not None
    summary = store.get_session_summary(service.active_session.session_id)
    assert summary is not None and summary.last_run_id is not None
    messages = store.load_runtime_messages(service.active_session.session_id, summary.last_run_id)
    assert [message.kind for message in messages[-2:]] == ["error", "run_finished"]


def test_restored_session_uses_the_current_runtime_message_policy(tmp_path: Path) -> None:
    first_store = PostgresSessionStore()
    first_service = ConversationService(AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)), first_store)
    first_service.run_task("calculate 1 + 1", mode="agent")

    assert first_service.active_session is not None
    second_store = PostgresSessionStore()
    second_service = ConversationService(
        AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path), log_full_messages=False),
        second_store,
        session_id=first_service.active_session.session_id,
    )
    state = second_service.run_task("calculate 2 + 2", mode="agent")

    started = next(message for message in state.runtime_messages if message.kind == "run_started")
    assert isinstance(started.data["task"], dict)


def test_checkpoint_and_jsonl_share_the_same_ordered_runtime_timestamps(tmp_path: Path) -> None:
    checkpoints = PostgresCheckpointStore()
    logger = JsonlRunLogger(tmp_path / "logs")
    runner = AgentRunner(ReasoningPlanner(), ToolRegistry(), strategy="reactive", checkpoints=checkpoints)
    runtime = runner.new_runtime(task="think", on_event=logger)

    state = runner.run(runtime)
    saved = checkpoints.load(state.run_id)
    records = [json.loads(line) for line in logger.path_for(state.run_id).read_text(encoding="utf-8").splitlines()]

    assert saved is not None
    assert [message.kind for message in state.runtime_messages].count("thinking") == 1
    assert [record["kind"] for record in records].count("thinking") == 1
    assert "thinking_delta" not in [record["kind"] for record in records]
    state_by_kind = {message.kind: message for message in state.runtime_messages}
    record_by_kind = {record["kind"]: record for record in records}
    assert state_by_kind["thinking"].timestamp == record_by_kind["thinking"]["timestamp"]
    assert state_by_kind["thinking"].data == record_by_kind["thinking"]["data"]
    assert [record["sequence"] for record in records] == [message.sequence for message in state.runtime_messages]
    assert saved.runtime_messages[-1] == state.runtime_messages[-1]


def test_model_exchange_messages_are_logged_as_normalized_request_and_response(tmp_path: Path) -> None:
    planner = LLMPlanner(StaticCompletionClient(provider_options={"deepseek": {"raw": "not logged"}}), [], [])
    runner = AgentRunner(planner, ToolRegistry(), strategy="reactive")

    state = runner.run(runner.new_runtime(task="say hello"))

    request = next(message for message in state.runtime_messages if message.kind == "model_request")
    response = next(message for message in state.runtime_messages if message.kind == "model_response")
    assert request.data["exchange_id"] == response.data["exchange_id"]
    assert request.data["operation"] == "decision"
    assert request.data["messages"][0]["role"] == "system"
    assert request.data["messages"][-1]["content"] == "say hello"
    assert response.data["message"]["content"] == "model answer"
    assert response.data["usage"] == {"total_tokens": 3}
    assert "provider_options" not in request.data["messages"][0]
    assert "provider_options" not in response.data["message"]


def test_each_model_call_receives_a_distinct_exchange_id() -> None:
    planner = LLMPlanner(
        SequencedCompletionClient(['{"strategy":"reactive","reason":"Direct answer."}', "model answer"]),
        [],
        [],
    )
    runner = AgentRunner(planner, ToolRegistry())
    state = runner.run(runner.new_runtime(task="say hello"))

    exchange_ids = [
        message.data["exchange_id"] for message in state.runtime_messages if message.kind == "model_request"
    ]
    assert len(exchange_ids) == 2
    assert len(set(exchange_ids)) == 2


def test_model_request_failure_is_recorded_without_secret_content() -> None:
    planner = LLMPlanner(FailingCompletionClient(), [], [])
    runner = AgentRunner(planner, ToolRegistry(), strategy="reactive", log_full_messages=False)

    state = runner.run(runner.new_runtime(task="fail"))

    error = next(message for message in state.runtime_messages if message.kind == "model_error")
    assert state.status == "failed"
    assert error.data["error_type"] == "PlanningError"
    assert "visible-secret" not in json.dumps(error.__dict__, ensure_ascii=False)


def test_failed_run_closes_canonical_history_and_survives_restart(tmp_path: Path) -> None:
    store = PostgresSessionStore()
    planner = LLMPlanner(FailingCompletionClient(), [], [])
    service = ConversationService(AgentRunner(planner, ToolRegistry(), strategy="reactive"), store)

    state = service.run_task("fail", mode="agent")

    assert state.status == "failed"
    assert service.runtime is not None
    expected = [
        ("user", "fail"),
        ("assistant", state.final_answer),
    ]
    assert [(message.role, message.content) for message in service.runtime.state.messages] == expected

    reopened = PostgresSessionStore()
    restored = reopened.load_runtime(service.runtime.state.session_id)
    assert restored is not None
    assert [(message.role, message.content) for message in restored.messages] == expected
    assert reopened.load_conversation(service.runtime.state.session_id) == [
        {"role": role, "content": content} for role, content in expected
    ]


def test_fail_run_records_the_same_assistant_error_only_once() -> None:
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry(), strategy="reactive").new_runtime(task="fail")

    fail_run(runtime, "Decision failed.")
    fail_run(runtime, "Decision failed.")

    assistant_errors = [
        message
        for message in runtime.state.messages
        if isinstance(message, AssistantMessage) and message.content == "Decision failed."
    ]
    assert len(assistant_errors) == 1


def test_summary_mode_redacts_secret_message_content_everywhere_in_the_audit_trace(tmp_path: Path) -> None:
    logger = JsonlRunLogger(tmp_path / "logs", include_full_messages=False)
    planner = LLMPlanner(StaticCompletionClient("token=visible-secret"), [], [])
    runner = AgentRunner(planner, ToolRegistry(), strategy="reactive", log_full_messages=False)
    runtime = runner.new_runtime(task="API_KEY=visible-secret", on_event=logger)

    state = runner.run(runtime)
    request = next(message for message in state.runtime_messages if message.kind == "model_request")
    response = next(message for message in state.runtime_messages if message.kind == "model_response")
    records = [json.loads(line) for line in logger.path_for(state.run_id).read_text(encoding="utf-8").splitlines()]
    rendered = json.dumps([message.__dict__ for message in state.runtime_messages], ensure_ascii=False)
    logged = json.dumps(records, ensure_ascii=False)

    assert isinstance(request.data["messages"][-1]["content"], dict)
    assert isinstance(response.data["message"]["content"], dict)
    assert "visible-secret" not in rendered
    assert "visible-secret" not in logged
    assert "[REDACTED]" in rendered


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("FALSE", False)])
def test_log_full_messages_reads_boolean_from_env_file(tmp_path: Path, value: str, expected: bool) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(f"LOG_FULL_MESSAGES={value}\n", encoding="utf-8")

    assert log_full_messages_from_env(env_path, environ={}) is expected


def test_log_full_messages_rejects_invalid_env_value(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("LOG_FULL_MESSAGES=yes\n", encoding="utf-8")

    with pytest.raises(ValueError, match="LOG_FULL_MESSAGES"):
        log_full_messages_from_env(env_path, environ={})


def test_database_url_is_required_and_environment_overrides_env_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    with pytest.raises(ValueError, match="DATABASE_URL"):
        database_url_from_env(env_path, environ={})

    env_path.write_text("DATABASE_URL=postgresql://file\n", encoding="utf-8")
    assert (
        database_url_from_env(env_path, environ={"DATABASE_URL": "postgresql://environment"})
        == "postgresql://environment"
    )


class StreamingCompletionClient:
    def run(self, runtime) -> PreparedResponse:
        if runtime.exchange.operation == "strategy":
            return PreparedResponse(AssistantMessage(content='{"strategy":"reactive","reason":"Direct response."}'))
        assert runtime.exchange.on_reasoning is not None
        assert runtime.exchange.on_content is not None
        runtime.exchange.on_reasoning("Think.")
        runtime.exchange.on_content("Hel")
        runtime.exchange.on_content("lo")
        return PreparedResponse(
            AssistantMessage(content="Hello", reasoning="Think."),
            usage={"total_tokens": 7},
        )


def test_response_stream_is_renderable_but_not_persisted_chunk_by_chunk(tmp_path: Path) -> None:
    events = []
    logger = JsonlRunLogger(tmp_path / "logs")
    planner = LLMPlanner(StreamingCompletionClient(), [], [])
    runner = AgentRunner(planner, ToolRegistry(), strategy="auto")
    runtime = runner.new_runtime(
        task="say hello",
        on_event=EventFanout([events.append, logger]),
    )

    state = runner.run(runtime)

    stream_kinds = [
        event.kind for event in events if event.kind.startswith("thinking_") or event.kind.startswith("response_")
    ]
    assert stream_kinds == [
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "response_start",
        "response_delta",
        "response_delta",
        "response_end",
    ]
    final = next(event for event in events if event.kind == "response")
    assert final.message == "Hello"
    assert final.data["streamed"] is True
    transient = {"response_start", "response_delta", "response_end"}
    assert transient.isdisjoint(message.kind for message in state.runtime_messages)

    records = [json.loads(line) for line in logger.path_for(state.run_id).read_text(encoding="utf-8").splitlines()]
    assert transient.isdisjoint(record["kind"] for record in records)
    assert [record["kind"] for record in records].count("response") == 1


def test_assistant_message_bounds_non_stream_content_before_tool_execution_and_is_transient() -> None:
    events = []
    runner = AgentRunner(
        OneToolThenAnswerPlanner(),
        ToolRegistry([Tool("echo", "Echoes a value.", lambda value: value)]),
        strategy="reactive",
    )

    state = runner.run(runner.new_runtime(task="use a tool", on_event=events.append))

    assistant = next(event for event in events if event.kind == "assistant_message")
    tool_call = next(event for event in events if event.kind == "tool_call")
    assert events.index(assistant) < events.index(tool_call)
    assert assistant.data["exchange_id"] is None
    assert assistant.data["reasoning_streamed"] is False
    assert assistant.data["content_streamed"] is False
    assert assistant.data["message"] == {
        "name": "assistant",
        "role": "assistant",
        "content": "I will use a tool.",
        "reasoning": "I need the tool result.",
        "logprobs": None,
        "tool_messages": [
            {
                "name": "echo",
                "role": "tool",
                "content": None,
                "call_id": "call_echo",
                "arguments": {"value": "ok"},
                "status": "pending",
                "retryable": None,
                "provider_options": {},
            }
        ],
        "provider_options": {},
    }
    assert "thinking_start" not in [event.kind for event in events]
    assert "assistant_message" not in [message.kind for message in state.runtime_messages]


def test_tool_lifecycle_events_are_correlated_by_call_id() -> None:
    events = []
    runner = AgentRunner(
        OneToolThenAnswerPlanner(),
        ToolRegistry([Tool("echo", "Echoes a value.", lambda value: value)]),
        strategy="reactive",
    )

    runner.run(runner.new_runtime(task="use a tool", on_event=events.append))

    lifecycle = [event for event in events if event.kind in {"tool_call", "tool_result"}]
    assert [event.data["call_id"] for event in lifecycle] == ["call_echo", "call_echo"]


class ExplodingStreamingPlanner:
    name = "exploding-stream"

    def decide(self, runtime) -> AssistantMessage:
        assert runtime.exchange.on_content is not None
        runtime.exchange.on_content("partial")
        raise RuntimeError("stream interrupted")


def test_unexpected_failure_closes_open_response_stream() -> None:
    events = []
    runner = AgentRunner(ExplodingStreamingPlanner(), ToolRegistry(), strategy="reactive")
    runtime = runner.new_runtime(task="fail while streaming", on_event=events.append)

    with pytest.raises(RuntimeError, match="stream interrupted"):
        runner.run(runtime)

    kinds = [event.kind for event in events]
    assert kinds.index("response_start") < kinds.index("response_delta")
    assert kinds.index("response_delta") < kinds.index("response_end")
    assert kinds[-1] == "response_end"
    transient = {"response_start", "response_delta", "response_end"}
    assert transient.isdisjoint(message.kind for message in runtime.run.runtime_messages)
