from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.chat import routes as chat_routes
from backend.api.chat.routes import _terminal_type_for_status
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.domain import AssistantMessage, ToolMessage
from backend.domain.runtime_state import (
    APP_VERSION,
    InMemoryNodeStore,
    NodeFrame,
    NodeWriter,
    RuntimeState,
    RuntimeStateTree,
    RuntimeStateValidationError,
)
from backend.runtime import AgentRunner, ConversationService, build_application
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.storage.auth import LocalAuthStore
from backend.storage.sqlite import SQLiteSessionStore
from backend.storage.sqlite_schema import SCHEMA, SQLiteSchemaMixin
from backend.tools import ToolRegistry


def make_turn(
    *, turn_id: str = "turn_1", thread_id: str = "session_1", parent: RuntimeState | None = None
) -> RuntimeState:
    return RuntimeState.create(
        session_id="session_1",
        thread_id=thread_id,
        id=turn_id,
        parent=parent,
        user_content=[{"type": "text", "text": "hello"}],
        provider_name="local",
    )


def test_turn_shape_is_strict_and_has_no_synthetic_root() -> None:
    turn = make_turn()
    assert turn.version == APP_VERSION == "0.0.1"
    assert turn.parent_id == turn.parent_session_id == turn.parent_thread_id == ""
    assert turn.first_kept_item_size == 8
    assert turn.selected_messages[0]["role"] == "user"
    assert turn.selected_messages[1]["role"] == "assistant"
    payload = turn.to_dict()
    payload.pop("current_data_idx")
    with pytest.raises(RuntimeStateValidationError, match="missing required fields"):
        RuntimeState.from_dict(payload)


def test_writer_emits_full_turn_snapshots_and_persists_complete_items() -> None:
    frames: list[NodeFrame] = []
    store = InMemoryNodeStore()
    writer = NodeWriter(store, emit=frames.append)
    turn = writer.create(make_turn())
    turn = writer.append_item(
        turn, {"type": "tool_call", "call_id": "call_1", "name": "read_file", "arguments": {}, "replay_safe": True}
    )
    turn = writer.append_item(
        turn, {"type": "tool_result", "call_id": "call_1", "content": "ok", "status": "succeeded", "replay_safe": True}
    )
    turn = writer.finalize(turn, "success")
    assert [frame.type for frame in frames] == ["turn.create", "turn.update", "turn.update", "turn.update"]
    assert frames[-1].node.status == "success"
    assert [item["type"] for item in store.get_node("session_1", "turn_1").assistant_items] == [
        "tool_call",
        "tool_result",
    ]


def test_streamed_items_keep_canonical_order_across_model_and_tool_rounds() -> None:
    frames: list[NodeFrame] = []
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(
        store,
        session_id="session_1",
        thread_id="session_1",
        turn_id="turn_ordered",
        prompt="inspect",
        provider_name="local",
        emit=frames.append,
    )
    bridge.start()
    events = [
        RuntimeEvent("thinking_start"),
        RuntimeEvent("thinking_delta", "思考一"),
        RuntimeEvent("thinking_end"),
        RuntimeEvent(
            "assistant_message",
            data={
                "message": {
                    "reasoning": "思考一",
                    "tool_messages": [{"call_id": "call_1", "name": "read_file", "arguments": {}}],
                },
                "reasoning_streamed": True,
                "content_streamed": False,
            },
        ),
        RuntimeEvent("tool_call", "read_file", {"call_id": "call_1", "arguments": {}}),
        RuntimeEvent("tool_result", "result one", {"call_id": "call_1", "tool": "read_file"}),
        RuntimeEvent("response_start"),
        RuntimeEvent("response_delta", "回答一"),
        RuntimeEvent("response_end"),
        RuntimeEvent(
            "assistant_message",
            data={
                "message": {"content": "回答一", "tool_messages": []},
                "reasoning_streamed": False,
                "content_streamed": True,
            },
        ),
        RuntimeEvent("thinking_start"),
        RuntimeEvent("thinking_delta", "思考二"),
        RuntimeEvent("thinking_end"),
        RuntimeEvent(
            "assistant_message",
            data={
                "message": {
                    "reasoning": "思考二",
                    "tool_messages": [{"call_id": "call_2", "name": "glob", "arguments": {}}],
                },
                "reasoning_streamed": True,
                "content_streamed": False,
            },
        ),
        RuntimeEvent("tool_call", "glob", {"call_id": "call_2", "arguments": {}}),
        RuntimeEvent("response_start"),
        RuntimeEvent("response_delta", "回答二"),
        RuntimeEvent("response_end"),
        RuntimeEvent(
            "assistant_message",
            data={
                "message": {"content": "回答二", "tool_messages": []},
                "reasoning_streamed": False,
                "content_streamed": True,
            },
        ),
    ]
    for event in events:
        bridge.handle(event)
    bridge.finish("success")

    items = store.get_node("session_1", "turn_ordered").assistant_items
    assert [item["type"] for item in items] == [
        "reasoning",
        "tool_call",
        "tool_result",
        "text",
        "reasoning",
        "tool_call",
        "text",
    ]
    assert [item.get("text") for item in items if item["type"] in {"reasoning", "text"}] == [
        "思考一",
        "回答一",
        "思考二",
        "回答二",
    ]


def test_tool_approval_persists_only_the_interactive_decision_item() -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(
        store,
        session_id="session_1",
        turn_id="turn_approval",
        prompt="search",
        provider_name="local",
        emit=lambda _frame: None,
    )
    bridge.start()
    approval_data = {
        "call_id": "call_search",
        "tool": "web_search",
        "arguments": {"query": "local test"},
    }
    bridge.handle(RuntimeEvent("tool_call", "web_search", approval_data))
    bridge.handle(RuntimeEvent("approval_requested", "Call tool web_search?", approval_data))
    bridge.handle_input(
        {
            "kind": "tool",
            "message": "Call tool web_search?",
            "data": {**approval_data, "decision_id": "dec_search", "kind": "tool"},
        }
    )
    bridge.handle(RuntimeEvent("approval_granted", "Call tool web_search?", approval_data))
    bridge.handle(RuntimeEvent("tool_result", "local result", approval_data))

    items = store.get_node("session_1", "turn_approval").assistant_items
    assert [item["type"] for item in items] == ["tool_call", "approval", "tool_result"]
    assert items[1] == {
        "type": "approval",
        "event": "decision_requested",
        "decision_id": "dec_search",
        "kind": "tool",
        **approval_data,
        "text": "Call tool web_search?",
    }


def test_failed_tool_item_preserves_failure_metadata() -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(
        store,
        session_id="session_1",
        turn_id="turn_failed_tool",
        prompt="write",
        provider_name="local",
        emit=lambda _frame: None,
    )
    bridge.start()
    bridge.handle(RuntimeEvent("tool_call", "write_file", {"call_id": "call_denied", "arguments": {}}))
    bridge.handle(
        RuntimeEvent(
            "tool_failed",
            "denied",
            {"call_id": "call_denied", "tool": "write_file", "failure_code": "user_denied"},
        )
    )

    assert store.get_node("session_1", "turn_failed_tool").assistant_items[-1]["failure_code"] == "user_denied"


def test_one_running_turn_per_thread_but_parallel_threads_are_allowed() -> None:
    store = InMemoryNodeStore([make_turn()])
    with pytest.raises(ValueError, match="one running Turn"):
        store.create_node(make_turn(turn_id="turn_2"))
    fork_payload = make_turn(turn_id="turn_fork").to_dict()
    fork_payload["thread_id"] = "thread_fork"
    fork_payload["parent_thread_id"] = "session_1"
    store.create_node(RuntimeState.from_dict(fork_payload))


def test_sqlite_rewind_fork_and_compact_are_atomic(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), "device")
    session = store.create_session("main")
    store.create_sidebar_thread(session_id=session.session_id, thread_id=session.session_id, title="main")
    writer = NodeWriter(store)
    original = writer.create(
        RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            id="turn_original",
            user_content=[{"type": "text", "text": "v1"}],
        )
    )
    for index in range(10):
        original = writer.append_item(original, {"type": "text", "text": f"item-{index}"})
    writer.finalize(original, "success")

    rewound = store.append_turn_version("turn_original", {"type": "text", "text": "v2"})
    assert rewound.current_data_idx == 1 and len(rewound.data) == 2 and rewound.status == "running"
    writer.finalize(rewound, "success")
    selected = store.set_turn_current_data("turn_original", 0)
    assert selected.current_data_idx == 0

    forked = store.fork_turn_node("turn_original", new_turn_id="turn_fork", thread_id="thread_fork")
    assert forked.parent_id == ""
    assert forked.parent_thread_id == session.session_id
    assert forked.data == selected.data
    assert forked.compaction_id == forked.id

    compacted = store.create_compact_turn("turn_original", "summary", new_turn_id="turn_compact")
    items = compacted.assistant_items
    assert compacted.compaction_id == compacted.id
    assert items[0] == {"type": "compaction", "summary": "summary", "kept_item_count": 8}
    assert len(items[1:]) == 8


def test_missing_ancestor_and_bad_version_index_are_rejected() -> None:
    root = make_turn()
    child = make_turn(turn_id="turn_2", parent=root)
    with pytest.raises(RuntimeStateValidationError, match="parent is missing"):
        RuntimeStateTree([child]).ancestors(child)
    payload = root.to_dict()
    payload["current_data_idx"] = 5
    with pytest.raises(RuntimeStateValidationError, match="out of range"):
        RuntimeState.from_dict(payload)

    bad_thread = child.to_dict()
    bad_thread["parent_thread_id"] = "thread_wrong"
    with pytest.raises(RuntimeStateValidationError, match="parent_thread_id"):
        RuntimeStateTree([root, RuntimeState.from_dict(bad_thread)]).ancestors(("session_1", "turn_2"))

    cross_session = child.to_dict()
    cross_session["session_id"] = "session_2"
    cross_session["thread_id"] = "session_2"
    with pytest.raises(RuntimeStateValidationError, match="across Sessions"):
        RuntimeStateTree([root, RuntimeState.from_dict(cross_session)]).ancestors(("session_2", "turn_2"))


def test_v9_database_is_rejected_without_migration_or_deletion(tmp_path: Path) -> None:
    path = tmp_path / "v9.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO store_metadata(session_id,schema_version,created_at,updated_at) VALUES ('s',9,'x','x')"
    )
    connection.commit()
    with pytest.raises(RuntimeError, match="schema v9"):
        SQLiteSchemaMixin._assert_supported_schema(connection)
    assert path.exists()
    connection.close()


def test_pause_targets_only_the_requested_turn_in_parallel_threads(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))
    with TestClient(create_app(state)) as client:
        assert client.post("/api/auth/guest").status_code == 200
        identity = client.get("/api/auth/me").json()
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state, identity["id"])
        writer = NodeWriter(store)
        original = writer.create(
            RuntimeState.create(
                session_id=sidebar["session_id"],
                thread_id=sidebar["thread_id"],
                id="turn_main",
                user_content=[{"type": "text", "text": "main"}],
            )
        )
        writer.finalize(original, "success")
        forked = store.fork_turn_node("turn_main", new_turn_id="turn_fork", thread_id="thread_fork")
        store.create_sidebar_thread(
            session_id=sidebar["session_id"],
            thread_id=forked.thread_id,
            title="fork",
        )
        store.append_turn_version("turn_main", {"type": "text", "text": "main again"})
        store.append_turn_version("turn_fork", {"type": "text", "text": "fork again"})

        cancelled: list[str] = []
        state.active_turn_cancellations = {
            (identity["id"], "turn_main"): lambda: cancelled.append("turn_main"),
            (identity["id"], "turn_fork"): lambda: cancelled.append("turn_fork"),
        }
        response = client.post("/api/turns/turn_main/pause")
        assert response.status_code == 200
        assert cancelled == ["turn_main"]
        assert store.find_node("turn_main").status == "running"
        assert store.find_node("turn_fork").status == "running"


def test_plan_handoff_appends_to_the_same_turn(tmp_path: Path) -> None:
    class PlanHandoffPlanner:
        name = "plan-handoff"

        def decide(self, runtime):
            if runtime.run.mode == "plan":
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name=REQUEST_PLAN_REVIEW_NAME,
                            call_id="review_1",
                            arguments={"plan": "Implement the reviewed change."},
                        )
                    ]
                )
            return AssistantMessage(content="Implemented from the reviewed plan.")

    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), "device")
    service = ConversationService(
        AgentRunner(PlanHandoffPlanner(), ToolRegistry()),
        store,
    )

    result = service.run_task(
        "plan the change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("implement"),
    )

    assert result.status == "completed"
    assert service.active_session is not None
    turns = store.load_nodes(service.active_session.session_id)
    assert len(turns) == 1
    assert turns[0].status == "success"
    assert turns[0].selected_messages[0]["content"] == [{"type": "text", "text": "plan the change"}]
    assert any(
        item.get("type") == "text" and item.get("text") == "Implemented from the reviewed plan."
        for item in turns[0].assistant_items
    )


def test_sse_terminal_mapping_distinguishes_user_pause_from_network_pause() -> None:
    assert _terminal_type_for_status("success", None) == "success"
    assert _terminal_type_for_status("paused", "user") == "success"
    assert _terminal_type_for_status("paused", "network") == "failed"
    assert _terminal_type_for_status("failed", "agent") == "failed"


def test_real_sqlite_http_sse_round_trip_uses_one_complete_turn(tmp_path: Path, monkeypatch) -> None:
    state = WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))
    state.model_config_for_user = lambda _user_id: None

    def local_application(_state, user_id: str, *, session_id: str, workspace=None, **_kwargs):
        return build_application(
            workspace or state.session_workspace(user_id, session_id),
            planner_name="rule",
            paths=state.user_paths(user_id),
        )

    monkeypatch.setattr(chat_routes, "build_user_application", local_application)

    with TestClient(create_app(state)) as client:
        assert client.post("/api/auth/guest").status_code == 200
        sidebar = client.post("/api/sidebar-threads", json={"title": "SSE round trip"}).json()
        turn_id = "turn_http_sse"
        response = client.post(
            "/api/turns",
            json={
                "id": turn_id,
                "session_id": sidebar["session_id"],
                "thread_id": sidebar["thread_id"],
                "parent_id": "",
                "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                "permission_mode": "read_only",
                "running_mode": "agent",
            },
        )

        assert response.status_code == 200
        payloads = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
        assert payloads[-1] == f'<SSE id="{turn_id}" type="success"></SSE>'
        frames = [json.loads(payload) for payload in payloads[:-1]]
        assert frames[0]["type"] == "turn.create"
        assert frames[-1]["type"] == "turn.update"
        assert frames[-1]["turn"]["status"] == "success"

        turns = client.get("/api/turns", params={"session_id": sidebar["session_id"]}).json()
        assert len(turns) == 1
        assert turns[0]["id"] == turn_id
        assert turns[0]["data"][0][0]["content"] == [{"type": "text", "text": "hello"}]
        assert turns[0]["data"][0][1]["content"][-1]["type"] == "text"
