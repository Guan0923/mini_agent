from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.session_store import session_store as web_session_store
from backend.api.state import WebAppState
from backend.domain import (
    AssistantMessage,
    RunState,
    SkillSnapshot,
    SystemMessage,
    ToolSpec,
    TracePersistenceError,
    UserMessage,
)
from backend.domain.runtime_state import RuntimeState as TurnState
from backend.planning.model_requests import ModelRequestExecutor
from backend.runtime import AgentRunner, ConversationService
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.tools import ToolRegistry
from tests.local_store import session_store


class RecordingClient:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, _runtime: AgentRuntime) -> PreparedResponse:
        self.calls += 1
        return PreparedResponse(AssistantMessage(content="ok"))


def bound_runtime(tmp_path: Path) -> tuple[AgentRuntime, object, TurnState]:
    store = session_store(tmp_path)
    session = store.create_session("Trace")
    root = store.ensure_root_node(session.session_id)
    turn = TurnState.create(
        session_id=session.session_id,
        thread_id=session.session_id,
        parent=root,
        user_content="hello",
    )
    store.create_node(turn)
    runtime = AgentRuntime.ephemeral(session_id=session.session_id, planner=object(), tools=object())
    runtime.services.runtime_store = store
    runtime.state.provider = "responses"
    runtime.state.provider_name = "local-test"
    runtime.state.model = "fake-model"
    runtime.state.current_run = RunState(
        task="hello",
        mode="agent",
        thread_id=turn.thread_id,
        turn_id=turn.id,
        data_idx=turn.current_data_idx,
        active_skills=[SkillSnapshot("audit", "Audit", "Secret: hidden", "C:/skill", "abc", source="project")],
    )
    return runtime, store, turn


def test_turn_trace_persists_ordered_versioned_redacted_requests(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    client = RecordingClient()
    executor = ModelRequestExecutor(client)
    runtime.exchange.context["trace_base_system_prompt"] = "base"
    runtime.exchange.context["trace_user_preferences"] = "Cookie=private"
    tools = [
        ToolSpec("read", "Read", {"type": "object"}),
        ToolSpec(
            "mcp_demo_search",
            "Search",
            {"type": "object"},
            {"mini_agent": {"trace_origin": {"kind": "mcp", "server": "demo", "tool": "search"}}},
        ),
    ]

    for content in ("one", "two"):
        executor.run(
            runtime,
            [SystemMessage(content="effective"), UserMessage(content=content)],
            operation="decision",
            output_mode="tools",
            allowed_tools=tools,
            stream=False,
        )

    traces = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert client.calls == 2
    assert [trace.sequence for trace in traces] == [1, 2]
    assert traces[0].base_system_prompt == "base"
    assert traces[0].effective_system_prompt == "effective"
    assert traces[0].user_preferences == "Cookie=[REDACTED]"
    assert traces[0].tools[0]["origin"] == {"kind": "local", "tool": "read"}
    assert traces[0].tools[1]["origin"] == {"kind": "mcp", "server": "demo", "tool": "search"}
    assert "wire_request" not in traces[0].to_dict()
    assert "wire_response" not in traces[0].to_dict()
    assert runtime.run.model_calls == 2


def test_turn_trace_captures_skill_and_tool_changes_per_request(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    runtime.run.active_skills = []
    executor = ModelRequestExecutor(RecordingClient())
    executor.run(
        runtime,
        [SystemMessage(content="agent"), UserMessage(content="first")],
        operation="decision",
        output_mode="tools",
        allowed_tools=[ToolSpec("write", "Write", {"type": "object"})],
    )
    runtime.run.active_skills = [SkillSnapshot("audit", "Audit", "Follow audit.", "C:/skill", "abc", source="project")]
    executor.run(
        runtime,
        [SystemMessage(content="plan"), UserMessage(content="second")],
        operation="decision",
        output_mode="tools",
        allowed_tools=[ToolSpec("read", "Read", {"type": "object"})],
    )

    first, second = store.load_turn_trace(turn.session_id, turn.id, 0)
    assert first.skills == []
    assert [tool["name"] for tool in first.tools] == ["write"]
    assert second.skills[0]["instructions"] == "Follow audit."
    assert second.skills[0]["source"] == "project"
    assert second.skills[0]["sha256"] == "abc"
    assert [tool["name"] for tool in second.tools] == ["read"]


def test_turn_trace_omits_transport_fields_and_redacts_nested_secrets(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    runtime.state.request_parameters = {
        "headers": {"Authorization": "Bearer private"},
        "credentials": {"password": "private"},
        "nested": {"accessToken": "private", "safe": "visible"},
    }
    ModelRequestExecutor(RecordingClient()).run(
        runtime,
        [SystemMessage(content="system"), UserMessage(content="Token=private")],
        operation="decision",
        output_mode="text",
    )

    trace = store.load_turn_trace(turn.session_id, turn.id, 0)[0].to_dict()
    encoded = str(trace)
    assert "headers" not in trace["request_parameters"]
    assert "credentials" not in trace["request_parameters"]
    assert trace["request_parameters"]["nested"] == {"accessToken": "[REDACTED]", "safe": "visible"}
    assert "wire_request" not in encoded
    assert "wire_response" not in encoded
    assert "private" not in encoded


def test_turn_trace_write_failure_prevents_transport(tmp_path: Path) -> None:
    runtime, _store, _turn = bound_runtime(tmp_path)
    client = RecordingClient()
    runtime.services.runtime_store = object()

    with pytest.raises(TracePersistenceError, match="not sent"):
        ModelRequestExecutor(client).run(
            runtime,
            [SystemMessage(content="system"), UserMessage(content="hello")],
            operation="decision",
            output_mode="text",
        )

    assert client.calls == 0
    assert runtime.run.model_calls == 0


def test_turn_trace_write_failure_marks_the_canonical_turn_failed(tmp_path: Path, monkeypatch) -> None:
    class Planner:
        name = "trace-failure"

        def __init__(self, client: RecordingClient) -> None:
            self.executor = ModelRequestExecutor(client)

        def decide(self, runtime: AgentRuntime) -> AssistantMessage:
            return self.executor.run(
                runtime,
                [SystemMessage(content="system"), *runtime.state.messages],
                operation="decision",
                output_mode="text",
            ).message

    store = session_store(tmp_path / "store")
    client = RecordingClient()
    service = ConversationService(
        AgentRunner(Planner(client), ToolRegistry(), checkpoints=store),
        store,
    )

    def fail_trace(*_args, **_kwargs) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save_turn_trace", fail_trace)
    with pytest.raises(TracePersistenceError, match="not sent"):
        service.run_task("audit me", mode="agent")

    assert client.calls == 0
    assert service.active_session is not None
    state = store.load_runtime(service.active_session.session_id)
    assert state is not None and state.current_run is not None
    assert state.current_run.status == "failed"
    turn = store.find_node(state.current_run.turn_id)
    assert turn is not None and turn.status == "failed"
    assert any(
        item.get("type") == "error" and "Local trace persistence failed" in str(item.get("message"))
        for message in turn.data[state.current_run.data_idx]
        for item in message["content"]
    )


def test_trace_versions_are_isolated(tmp_path: Path) -> None:
    runtime, store, turn = bound_runtime(tmp_path)
    turn.data.append(turn.data[0])
    turn.current_data_idx = 1
    store.update_node(turn)
    runtime.run.data_idx = 1
    ModelRequestExecutor(RecordingClient()).run(
        runtime,
        [SystemMessage(content="system"), UserMessage(content="version two")],
        operation="decision",
        output_mode="text",
    )

    assert store.load_turn_trace(turn.session_id, turn.id, 0) == []
    assert len(store.load_turn_trace(turn.session_id, turn.id, 1)) == 1


def test_turn_trace_api_validates_turn_and_version(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = web_session_store(state)
        root = store.ensure_root_node(sidebar["session_id"])
        turn = TurnState.create(
            session_id=sidebar["session_id"],
            thread_id=sidebar["thread_id"],
            parent=root,
            user_content="hello",
        )
        store.create_node(turn)

        response = client.get(f"/api/turns/{turn.id}/trace", params={"data_idx": 0})
        assert response.status_code == 200
        assert response.json()["turn"]["id"] == turn.id
        assert response.json()["requests"] == []
        assert client.get(f"/api/turns/{turn.id}/trace", params={"data_idx": 9}).status_code == 422
        assert client.get("/api/turns/missing/trace", params={"data_idx": 0}).status_code == 404
