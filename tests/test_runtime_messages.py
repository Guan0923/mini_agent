import json
from pathlib import Path

import pytest

from mini_agent.domain import AssistantMessage, PlanningError
from mini_agent.observability import JsonlRunLogger
from mini_agent.planning import LLMPlanner, RuleBasedPlanner
from mini_agent.runtime import (
    AgentRunner,
    ConversationService,
    PreparedResponse,
    SQLiteCheckpointStore,
    SQLiteSessionStore,
)
from mini_agent.runtime.config import log_full_messages_from_env
from mini_agent.tools import ToolRegistry


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


class ExplodingPlanner:
    name = "exploding"

    def decide(self, runtime) -> AssistantMessage:
        raise RuntimeError("unexpected failure")


class FailingCompletionClient:
    def run(self, runtime) -> PreparedResponse:
        raise PlanningError("provider token=visible-secret failed")


def test_session_runtime_messages_survive_restart_and_match_run_state(tmp_path: Path) -> None:
    database = tmp_path / "checkpoints.db"
    store = SQLiteSessionStore(database)
    service = ConversationService(AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)), store)

    state = service.run_task("calculate 2 + 2", mode="agent")

    assert service.active_session is not None
    reopened = SQLiteSessionStore(database)
    messages = reopened.load_runtime_messages(service.active_session.session_id, state.run_id)

    assert messages == state.runtime_messages
    assert [message.sequence for message in messages] == list(range(1, len(messages) + 1))
    assert {"run_started", "response", "response", "response", "run_finished"} <= {
        message.kind for message in messages
    }
    assert reopened.load_conversation(service.active_session.session_id) == [
        {"role": "user", "content": "calculate 2 + 2"},
        {"role": "assistant", "content": "Hello! I can help with web search, file operations, and running commands in the workspace."},
    ]


def test_unexpected_failure_still_ends_the_persisted_runtime_trace(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "checkpoints.db")
    service = ConversationService(AgentRunner(ExplodingPlanner(), ToolRegistry(), strategy="reactive"), store)

    with pytest.raises(RuntimeError, match="unexpected failure"):
        service.run_task("fail", mode="agent")

    assert service.active_session is not None
    summary = store.get_session_summary(service.active_session.session_id)
    assert summary is not None and summary.last_run_id is not None
    messages = store.load_runtime_messages(service.active_session.session_id, summary.last_run_id)
    assert [message.kind for message in messages[-2:]] == ["error", "run_finished"]


def test_restored_session_uses_the_current_runtime_message_policy(tmp_path: Path) -> None:
    database = tmp_path / "checkpoints.db"
    first_store = SQLiteSessionStore(database)
    first_service = ConversationService(AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path)), first_store)
    first_service.run_task("calculate 1 + 1", mode="agent")

    assert first_service.active_session is not None
    second_store = SQLiteSessionStore(database)
    second_service = ConversationService(
        AgentRunner(RuleBasedPlanner(), ToolRegistry(tmp_path), log_full_messages=False),
        second_store,
        session_id=first_service.active_session.session_id,
    )
    state = second_service.run_task("calculate 2 + 2", mode="agent")

    started = next(message for message in state.runtime_messages if message.kind == "run_started")
    assert isinstance(started.data["task"], dict)


def test_checkpoint_and_jsonl_share_the_same_ordered_runtime_timestamps(tmp_path: Path) -> None:
    checkpoints = SQLiteCheckpointStore(tmp_path / "checkpoints.db")
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
