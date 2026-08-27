from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api import turns as turn_routes
from backend.api.active_turn_stream import ActiveTurnStream
from backend.api.app import create_app
from backend.api.chat import routes as chat_routes
from backend.api.chat.routes import _startup_failure_message, _terminal_type_for_status
from backend.api.pause_control import TurnPauseController
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.domain import CHECKPOINT_PREAMBLE, AssistantMessage, PlanningError, ToolMessage, UserMessage
from backend.domain.runtime_state import (
    APP_VERSION,
    InMemoryNodeStore,
    NodeFrame,
    NodeWriter,
    RuntimeState,
    RuntimeStateTree,
    RuntimeStateValidationError,
)
from backend.planning.context_management import ContextCompactionResult
from backend.planning.llm import LLMPlanner
from backend.planning.rule_based import RuleBasedPlanner
from backend.providers import ModelConfig, ModelConfigurationError
from backend.runtime import AgentApplication, AgentRunner, ConversationService, build_application
from backend.runtime.core.context import AgentRuntime, PreparedResponse
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.sandbox import SandboxInitializationError
from backend.storage.auth import LocalAuthStore
from backend.storage.sqlite import SQLiteSessionStore
from backend.storage.sqlite_schema import SCHEMA, SQLiteSchemaMixin
from backend.tools import ToolRegistry
from tui.runtime_nodes import RuntimeNodeReducer


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


def test_first_main_turn_atomically_auto_titles_its_sidebar_thread(tmp_path: Path, monkeypatch) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), "device")
    session = store.create_session("新对话")
    store.create_sidebar_thread(
        session_id=session.session_id,
        thread_id=session.session_id,
        title="新对话",
    )
    prompt = f"  第一条   用户消息 {'字' * 90}  "
    turn = RuntimeState.create(
        session_id=session.session_id,
        thread_id=session.session_id,
        id="turn_auto_title",
        user_content=[{"type": "text", "text": prompt}],
    )

    original_append_event = store._append_event

    def fail_turn_event(connection, session_id, *, kind, payload):
        if kind == "turn_upserted":
            raise RuntimeError("turn event failed")
        return original_append_event(connection, session_id, kind=kind, payload=payload)

    monkeypatch.setattr(store, "_append_event", fail_turn_event)
    with pytest.raises(RuntimeError, match="turn event failed"):
        store.create_node(turn)

    assert store.get_sidebar_thread(session.session_id).title == "新对话"
    assert store.load_nodes(session.session_id) == []

    monkeypatch.setattr(store, "_append_event", original_append_event)
    store.create_node(turn)
    sidebar = store.get_sidebar_thread(session.session_id)
    assert sidebar is not None
    assert sidebar.title == (f"第一条 用户消息 {'字' * 90}")[:80]
    assert sidebar.title_is_custom is False
    with sqlite3.connect(store.paths.session_db(session.session_id)) as connection:
        events = connection.execute(
            "SELECT kind,payload_json FROM json_events ORDER BY local_sequence DESC LIMIT 2"
        ).fetchall()
    assert [kind for kind, _payload in reversed(events)] == ["sidebar_thread_upserted", "turn_upserted"]


def test_auto_title_never_overwrites_manual_or_established_sidebar_titles(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), "device")

    manual = store.create_session("手工标题")
    store.create_sidebar_thread(
        session_id=manual.session_id,
        thread_id=manual.session_id,
        title=manual.title,
        title_is_custom=True,
    )
    manual_turn = RuntimeState.create(
        session_id=manual.session_id,
        thread_id=manual.session_id,
        id="turn_manual",
        user_content=[{"type": "text", "text": "不能覆盖"}],
    )
    NodeWriter(store).create(manual_turn)
    assert store.get_sidebar_thread(manual.session_id).title == "手工标题"

    automatic = store.create_session("新对话")
    store.create_sidebar_thread(
        session_id=automatic.session_id,
        thread_id=automatic.session_id,
        title=automatic.title,
    )
    writer = NodeWriter(store)
    first = writer.create(
        RuntimeState.create(
            session_id=automatic.session_id,
            thread_id=automatic.session_id,
            id="turn_first",
            user_content=[{"type": "text", "text": "第一条消息"}],
        )
    )
    first = writer.finalize(first, "success")
    compacted = store.create_compact_turn(first.id, "压缩摘要", new_turn_id="turn_compact")
    compact_writer = NodeWriter(store)
    compacted = compact_writer.snapshot(compacted)
    compacted = compact_writer.finalize(compacted, "success")
    second = writer.create(
        RuntimeState.create(
            session_id=automatic.session_id,
            thread_id=automatic.session_id,
            id="turn_second",
            parent=compacted,
            user_content=[{"type": "text", "text": "第二条消息"}],
        )
    )
    writer.finalize(second, "failed")
    store.append_turn_version(first.id, {"type": "text", "text": "回退消息"})
    assert store.get_sidebar_thread(automatic.session_id).title == "第一条消息"


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

    missing_status = turn.to_dict()
    missing_status["data"][0][1]["content"].append({"type": "text", "text": "partial"})
    with pytest.raises(RuntimeStateValidationError, match="Item status"):
        RuntimeState.from_dict(missing_status)

    legacy_tool_status = turn.to_dict()
    legacy_tool_status["data"][0][1]["content"].append(
        {"type": "tool_result", "call_id": "legacy", "content": "done", "status": "succeeded"}
    )
    with pytest.raises(RuntimeStateValidationError, match="Item status"):
        RuntimeState.from_dict(legacy_tool_status)


def test_writer_emits_one_baseline_then_exact_incremental_operations() -> None:
    frames: list[NodeFrame] = []
    store = InMemoryNodeStore()
    writer = NodeWriter(store, emit=frames.append)
    turn = writer.create(make_turn())
    turn = writer.append_item(
        turn,
        {
            "type": "tool_call",
            "call_id": "call_1",
            "name": "read_file",
            "arguments": {},
            "replay_safe": True,
            "status": "running",
        },
    )
    turn = writer.set_item_status(turn, data_idx=0, message_idx=1, item_idx=0, status="success")
    turn = writer.append_item(
        turn,
        {"type": "tool_result", "call_id": "call_1", "content": "ok", "status": "success", "replay_safe": True},
    )
    turn = writer.append_item(turn, {"type": "text", "text": "answer ", "status": "running"}, persist=False)
    turn = writer.append_text(turn, data_idx=0, item_idx=2, delta="done", persist=True)
    turn = writer.set_item_status(turn, data_idx=0, message_idx=1, item_idx=2, status="success")
    turn = writer.finalize(turn, "success")
    assert [frame.type for frame in frames] == ["turn.snapshot", *["turn.delta"] * 7]
    assert [frame.revision for frame in frames] == list(range(8))
    assert frames[5].operations == (
        {"op": "append_text", "data_idx": 0, "message_idx": 1, "item_idx": 2, "delta": "done"},
    )
    assert frames[6].operations == (
        {"op": "set_item_status", "data_idx": 0, "message_idx": 1, "item_idx": 2, "status": "success"},
    )
    assert frames[-1].patch == {"status": "success"}
    assert all("turn" not in frame.to_dict() and "data" not in frame.to_dict() for frame in frames[1:])
    assert [item["type"] for item in store.get_node("session_1", "turn_1").assistant_items] == [
        "tool_call",
        "tool_result",
        "text",
    ]
    assert store.get_node("session_1", "turn_1").assistant_items[-1]["text"] == "answer done"

    child = writer.create(make_turn(turn_id="turn_2", parent=turn))
    assert frames[-1].type == "turn.snapshot" and frames[-1].revision == 0
    assert child.parent_id == turn.id


def test_model_context_projects_only_successful_turn_items() -> None:
    node = RuntimeState.create(
        session_id="session_context",
        thread_id="session_context",
        user_content="inspect",
        data=[
            [
                {"role": "user", "content": [{"type": "text", "text": "inspect", "status": "success"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "kept", "status": "success"},
                        {"type": "text", "text": "partial", "status": "failed"},
                        {"type": "reasoning", "text": "unfinished", "status": "running"},
                        {
                            "type": "tool_call",
                            "call_id": "call_ok",
                            "name": "read_file",
                            "arguments": {},
                            "status": "success",
                        },
                        {
                            "type": "tool_result",
                            "call_id": "call_ok",
                            "content": "done",
                            "status": "success",
                        },
                        {
                            "type": "tool_call",
                            "call_id": "call_failed",
                            "name": "write_file",
                            "arguments": {},
                            "status": "failed",
                        },
                        {
                            "type": "tool_result",
                            "call_id": "call_failed",
                            "content": "denied",
                            "status": "failed",
                        },
                        {
                            "type": "tool_call",
                            "call_id": "call_orphan",
                            "name": "glob",
                            "arguments": {},
                            "status": "success",
                        },
                        {
                            "type": "tool_result",
                            "call_id": "result_orphan",
                            "content": "ignored",
                            "status": "success",
                        },
                    ],
                },
            ]
        ],
    )
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).empty_runtime(session_id="session_context")
    runtime.services.runtime_node_context = lambda: [node]

    projected = runtime.model_messages()

    assert isinstance(projected[0], UserMessage) and projected[0].content == "inspect"
    assert isinstance(projected[1], AssistantMessage)
    assert projected[1].content == "kept"
    assert projected[1].reasoning is None
    assert [(tool.call_id, tool.content, tool.status) for tool in projected[1].tool_messages] == [
        ("call_ok", "done", "succeeded")
    ]


def test_late_tool_failure_after_steering_settles_the_previous_assistant_call() -> None:
    frames: list[NodeFrame] = []
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(
        store,
        session_id="session_steering_status",
        thread_id="session_steering_status",
        turn_id="turn_steering_status",
        prompt="start",
        emit=frames.append,
    )
    bridge.start()
    bridge.handle(
        RuntimeEvent(
            "assistant_message",
            "",
            {
                "message": {
                    "role": "assistant",
                    "tool_messages": [
                        {
                            "name": "read_file",
                            "call_id": "call_stale",
                            "arguments": {"path": "README.md"},
                        }
                    ],
                }
            },
        )
    )
    bridge.handle(
        RuntimeEvent(
            "steering_applied",
            "In-run user input applied",
            {"content": "redirect", "steering_id": "steer_1"},
        )
    )
    bridge.handle(
        RuntimeEvent(
            "tool_failed",
            "Not executed because the user supplied new instructions.",
            {
                "tool": "read_file",
                "call_id": "call_stale",
                "error": "Not executed because the user supplied new instructions.",
            },
        )
    )
    bridge.handle(
        RuntimeEvent(
            "assistant_message",
            "",
            {"message": {"role": "assistant", "content": "redirected answer"}},
        )
    )

    completed = bridge.finish("success", "redirected answer")

    assert completed is not None and completed.status == "success"
    messages = completed.data[completed.current_data_idx]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    first_call = next(item for item in messages[1]["content"] if item.get("call_id") == "call_stale")
    assert first_call["type"] == "tool_call" and first_call["status"] == "failed"
    assert not any(item["status"] == "running" for message in messages for item in message["content"])


def test_active_turn_stream_rebases_late_subscribers_and_broadcasts_terminal() -> None:
    stream = ActiveTurnStream("turn_1")
    original = stream.subscribe("turn_1")
    turn = make_turn()
    stream.publish_frame(NodeFrame.snapshot(turn), turn)
    assert original.next_event()["revision"] == 0

    with_text = turn.clone()
    with_text.data[0][1]["content"].append({"type": "text", "text": "first", "status": "success"})
    first_delta = NodeFrame.delta(turn, with_text, revision=1)
    assert first_delta is not None
    stream.publish_frame(first_delta, with_text)
    assert original.next_event()["revision"] == 1

    late = stream.subscribe("turn_1")
    late_snapshot = late.next_event()
    assert late_snapshot["revision"] == 0
    assert late_snapshot["turn"]["data"][0][1]["content"][-1]["text"] == "first"

    completed = with_text.clone()
    completed.status = "success"
    final_delta = NodeFrame.delta(with_text, completed, revision=2)
    assert final_delta is not None
    stream.publish_frame(final_delta, completed)
    assert original.next_event()["revision"] == 2
    assert late.next_event()["revision"] == 1

    stream.publish_terminal("success", "turn_1")
    assert original.next_event()["terminal_type"] == "success"
    assert late.next_event()["terminal_type"] == "success"


def test_active_turn_stream_unsubscribe_does_not_close_other_subscribers() -> None:
    stream = ActiveTurnStream("turn_1")
    first = stream.subscribe("turn_1")
    second = stream.subscribe("turn_1")
    stream.unsubscribe(first.token)
    assert first.closed is True
    assert stream.subscriber_count == 1

    turn = make_turn()
    stream.publish_frame(NodeFrame.snapshot(turn), turn)
    assert second.next_event()["turn"]["id"] == "turn_1"


def test_turn_stream_endpoint_returns_terminal_snapshot_and_rejects_missing_turn(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))
    with TestClient(create_app(state)) as client:
        assert client.get("/api/turns/anything/stream").status_code == 401
        identity = client.post("/api/auth/guest").json()["user"]
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state, identity["id"])
        writer = NodeWriter(store)
        turn = writer.create(
            RuntimeState.create(
                session_id=sidebar["session_id"],
                thread_id=sidebar["thread_id"],
                id="turn_completed_stream",
                user_content=[{"type": "text", "text": "hello"}],
            )
        )
        writer.finalize(turn, "success")

        response = client.get("/api/turns/turn_completed_stream/stream")
        payloads = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
        assert json.loads(payloads[0])["turn"]["status"] == "success"
        assert payloads[1] == '<SSE id="turn_completed_stream" type="success"></SSE>'
        assert client.get("/api/turns/missing/stream").status_code == 404


def test_writer_rejects_a_delta_for_an_existing_turn_without_a_stream_baseline() -> None:
    store = InMemoryNodeStore()
    existing = make_turn()
    store.create_node(existing)
    writer = NodeWriter(store, emit=lambda _frame: None)
    changed = existing.clone()
    changed.status = "success"

    with pytest.raises(RuntimeStateValidationError, match="baseline snapshot"):
        writer.update(changed, persist=True)

    assert store.get_node(existing.session_id, existing.id).status == "running"


def test_long_text_delta_frames_grow_linearly_without_repeating_accumulated_text() -> None:
    def stream_size(chunk_count: int) -> int:
        frames: list[NodeFrame] = []
        writer = NodeWriter(InMemoryNodeStore(), emit=frames.append)
        turn = writer.create(make_turn())
        chunk = "abcdefghij"
        turn = writer.append_item(turn, {"type": "text", "text": chunk, "status": "running"}, persist=False)
        for _ in range(chunk_count - 1):
            turn = writer.append_text(turn, data_idx=0, item_idx=0, delta=chunk)
        writer.persist(turn)

        text_operations = [
            operation for frame in frames[1:] for operation in frame.operations if operation["op"] == "append_text"
        ]
        assert len(text_operations) == chunk_count - 1
        assert all(operation["delta"] == chunk for operation in text_operations)
        assert all("turn" not in frame.to_dict() and "data" not in frame.to_dict() for frame in frames[1:])
        return sum(len(frame.to_json().encode("utf-8")) for frame in frames)

    size_64 = stream_size(64)
    size_128 = stream_size(128)
    assert size_128 < size_64 * 2.1


def test_legacy_tui_reducer_applies_the_incremental_turn_contract() -> None:
    reducer = RuntimeNodeReducer()
    baseline = make_turn().to_dict()
    node = reducer.apply({"type": "turn.snapshot", "revision": 0, "turn": baseline})
    assert node is not None
    updated = reducer.apply(
        {
            "type": "turn.delta",
            "session_id": "session_1",
            "turn_id": "turn_1",
            "revision": 1,
            "operations": [
                {
                    "op": "append_item",
                    "data_idx": 0,
                    "message_idx": 1,
                    "item_idx": 0,
                    "item": {"type": "text", "text": "a", "status": "running"},
                },
                {"op": "append_text", "data_idx": 0, "message_idx": 1, "item_idx": 0, "delta": "b"},
            ],
        }
    )
    assert updated is not None and updated.data[0][1]["content"] == [
        {"type": "text", "text": "ab", "status": "running"}
    ]
    with pytest.raises(ValueError, match="not consecutive"):
        reducer.apply(
            {
                "type": "turn.delta",
                "session_id": "session_1",
                "turn_id": "turn_1",
                "revision": 3,
            }
        )


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
        RuntimeEvent("thinking_delta", "思考"),
        RuntimeEvent("thinking_delta", "一"),
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
        RuntimeEvent("response_delta", "回答"),
        RuntimeEvent("response_delta", "一"),
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
        RuntimeEvent("thinking_delta", "思考"),
        RuntimeEvent("thinking_delta", "二"),
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
        RuntimeEvent("response_delta", "回答"),
        RuntimeEvent("response_delta", "二"),
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
    text_deltas = [
        operation["delta"] for frame in frames for operation in frame.operations if operation["op"] == "append_text"
    ]
    assert text_deltas == ["一", "一", "二", "二"]
    assert [frame.revision for frame in frames] == list(range(len(frames)))


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
        "status": "success",
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
        original = writer.append_item(original, {"type": "text", "text": f"item-{index}", "status": "success"})
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
    assert items[0] == {"type": "compaction", "summary": "summary", "kept_item_count": 8, "status": "success"}
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

        main_pause = TurnPauseController()
        fork_pause = TurnPauseController()
        state.active_turn_cancellations = {
            (identity["id"], "turn_main"): main_pause,
            (identity["id"], "turn_fork"): fork_pause,
        }
        response = client.post("/api/turns/turn_main/pause")
        assert response.status_code == 200
        assert main_pause.is_requested() is True
        assert fork_pause.is_requested() is False
        assert store.find_node("turn_main").status == "running"
        assert store.find_node("turn_fork").status == "running"


def test_fork_sidebar_title_is_copied_once_unless_explicitly_named(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))
    with TestClient(create_app(state)) as client:
        assert client.post("/api/auth/guest").status_code == 200
        identity = client.get("/api/auth/me").json()
        source_sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state, identity["id"])
        writer = NodeWriter(store)
        source = writer.create(
            RuntimeState.create(
                session_id=source_sidebar["session_id"],
                thread_id=source_sidebar["thread_id"],
                id="turn_fork_source",
                user_content=[{"type": "text", "text": "源对话标题"}],
            )
        )
        writer.finalize(source, "success")

        inherited_response = client.post("/api/turns/turn_fork_source/fork", json={})
        assert inherited_response.status_code == 201
        inherited = inherited_response.json()["sidebar_thread"]
        assert inherited["title"] == "源对话标题"
        assert inherited["title_is_custom"] is False

        renamed = client.patch(
            f"/api/sidebar-threads/{source_sidebar['thread_id']}",
            json={"title": "源对话已改名"},
        )
        assert renamed.status_code == 200
        assert store.get_sidebar_thread(inherited["thread_id"]).title == "源对话标题"

        explicit_response = client.post(
            "/api/turns/turn_fork_source/fork",
            json={"title": "手工分支标题"},
        )
        assert explicit_response.status_code == 201
        explicit = explicit_response.json()["sidebar_thread"]
        assert explicit["title"] == "手工分支标题"
        assert explicit["title_is_custom"] is True


def test_plan_handoff_creates_agent_child_with_raw_plan_message(tmp_path: Path) -> None:
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
    assert len(turns) == 2
    plan, agent = turns
    assert plan.status == agent.status == "success"
    assert plan.running_mode == "plan"
    assert agent.running_mode == "agent"
    assert agent.parent_id == plan.id
    assert plan.selected_messages[0]["content"] == [{"type": "text", "text": "plan the change", "status": "success"}]
    assert agent.selected_messages[0]["content"] == [
        {"type": "text", "text": "Implement the reviewed change.", "status": "success"}
    ]
    assert any(
        item.get("type") == "text" and item.get("text") == "Implemented from the reviewed plan."
        for item in agent.assistant_items
    )


def test_plan_compaction_handoff_emits_plan_compact_agent_nodes_in_one_stream(tmp_path: Path) -> None:
    class CompactingPlanPlanner:
        name = "compacting-plan"

        def decide(self, runtime):
            if runtime.run.mode == "plan":
                return AssistantMessage(
                    tool_messages=[
                        ToolMessage(
                            name=REQUEST_PLAN_REVIEW_NAME,
                            call_id="review_compact",
                            arguments={"plan": "Implement after a real compaction."},
                        )
                    ]
                )
            return AssistantMessage(content="Implemented after compaction.")

        def compact_context(self, runtime):
            assert runtime.services.publish is not None
            runtime.services.publish(
                RuntimeEvent(
                    "context_compaction_completed",
                    "Conversation context compacted manually",
                    {"summary": "deterministic compact summary"},
                )
            )
            return ContextCompactionResult(True, 2, 1, "deterministic compact summary")

    frames: list[NodeFrame] = []
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), "device")
    service = ConversationService(AgentRunner(CompactingPlanPlanner(), ToolRegistry()), store)
    session = service.new_session("Plan compaction")
    bridge = RuntimeEventNodeBridge(
        store,
        session_id=session.session_id,
        thread_id=session.session_id,
        prompt="plan the compacted change",
        running_mode="plan",
        emit=frames.append,
    )
    service.attach_runtime_node_bridge(bridge, events_external=False)

    result = service.run_task(
        "plan the compacted change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("implement_and_compaction"),
    )

    assert result.status == "completed"
    snapshots = [frame.turn for frame in frames if frame.type == "turn.snapshot"]
    assert len(snapshots) == 3 and all(node is not None for node in snapshots)
    plan, compact, agent = snapshots
    assert plan is not None and compact is not None and agent is not None
    assert compact.parent_id == plan.id
    assert agent.parent_id == compact.id
    assert [node.running_mode for node in (plan, compact, agent)] == ["plan", "plan", "agent"]
    assert compact.assistant_items[0]["type"] == "compaction"
    assert compact.assistant_items[0]["summary"] == "deterministic compact summary"
    assert agent.selected_messages[0]["content"] == [
        {"type": "text", "text": "Implement after a real compaction.", "status": "success"}
    ]
    assert [node.status for node in store.load_nodes(session.session_id)] == ["success", "success", "success"]


def test_plan_compaction_failure_keeps_successful_plan_and_records_redacted_reason(tmp_path: Path) -> None:
    class FailingCompactionPlanner:
        name = "failing-compaction"

        def decide(self, _runtime):
            return AssistantMessage(
                tool_messages=[
                    ToolMessage(
                        name=REQUEST_PLAN_REVIEW_NAME,
                        call_id="review_failure",
                        arguments={"plan": "Keep this plan available."},
                    )
                ]
            )

        def compact_context(self, runtime):
            assert runtime.services.publish is not None
            runtime.services.publish(
                RuntimeEvent(
                    "context_compaction_failed",
                    "Conversation context compaction failed",
                    {"error": "summary provider unavailable; api_key=super-secret"},
                )
            )
            raise PlanningError("summary provider unavailable; api_key=super-secret")

    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"), "device")
    service = ConversationService(AgentRunner(FailingCompactionPlanner(), ToolRegistry()), store)

    result = service.run_task(
        "plan a change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("implement_and_compaction"),
    )

    assert result.status == "completed"
    assert result.mode == "plan"
    assert service.active_session is not None
    turns = store.load_nodes(service.active_session.session_id)
    assert len(turns) == 1
    assert turns[0].status == "success" and turns[0].running_mode == "plan"
    error = next(item for item in turns[0].assistant_items if item["type"] == "error")
    assert "summary provider unavailable" in error["message"]
    assert "super-secret" not in error["message"]
    assert "[REDACTED]" in error["message"]


def test_sse_terminal_mapping_distinguishes_user_pause_from_network_pause() -> None:
    assert _terminal_type_for_status("success", None) == "success"
    assert _terminal_type_for_status("paused", "user") == "success"
    assert _terminal_type_for_status("paused", "network") == "failed"
    assert _terminal_type_for_status("failed", "agent") == "failed"


def test_sandbox_startup_failure_message_explains_fail_closed_behavior() -> None:
    message = _startup_failure_message(SandboxInitializationError("Windows Sandbox Broker 未安装或当前不可用。"))

    assert message == ("Sandbox 初始化失败：Windows Sandbox Broker 未安装或当前不可用。 Agent 已停止，未降级执行。")


def test_http_sse_surfaces_sandbox_failure_before_turn_baseline(tmp_path: Path, monkeypatch) -> None:
    state = WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))
    state.model_config_for_user = lambda _user_id: None

    def fail_sandbox_startup(*_args, **_kwargs):
        raise SandboxInitializationError("Windows Sandbox Broker 已安装，但健康检查未通过。")

    monkeypatch.setattr(chat_routes, "build_user_application", fail_sandbox_startup)

    with TestClient(create_app(state)) as client:
        assert client.post("/api/auth/guest").status_code == 200
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        turn_id = "turn_sandbox_startup_failure"
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
    assert payloads == [
        f'<SSE id="{turn_id}" type="failed">Sandbox 初始化失败：Windows Sandbox Broker 已安装，但健康检查未通过。 Agent 已停止，未降级执行。</SSE>'
    ]


class HttpCompactionClient:
    context_size = 100_000

    def __init__(self, summary: str, *, failure: str = "") -> None:
        self.summary = summary
        self.failure = failure
        self.operations: list[str | None] = []
        self.transcripts: list[str] = []

    def estimate_tokens(self, messages, tools, request_parameters) -> int:
        return 100

    def run(self, runtime: AgentRuntime) -> PreparedResponse:
        self.operations.append(runtime.exchange.operation)
        self.transcripts.extend(
            message.content
            for message in runtime.exchange.messages
            if getattr(message, "role", "") == "user" and isinstance(message.content, str)
        )
        if self.failure:
            raise PlanningError(self.failure)
        return PreparedResponse(AssistantMessage(content=self.summary), {"total_tokens": 1})


def test_http_compact_uses_llm_bridge_and_never_accepts_a_supplied_summary(tmp_path: Path, monkeypatch) -> None:
    state = WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))
    model_config = ModelConfig("secret", "https://example.test/v1", "checkpoint-model")
    resolved_provider_names: list[str | None] = []

    def resolve_provider(_user_id: str, provider_name: str | None):
        resolved_provider_names.append(provider_name)
        return model_config

    state.model_config_for_user = lambda _user_id: model_config
    state.model_config_for_provider_name = resolve_provider
    summary = """## Primary Request and Intent
- Preserve the HTTP request.

## Key Technical Concepts
- TestClient and SQLite

## Files and Code
- backend/src/api/turns.py: synchronous Compact endpoint

## Errors and Fixes
- (none)

## Pending Jobs
- (none)

## Current Work
- Creating a Compaction Turn.

## Next Step
- Reload the Turn tree.

## Critical Context
- The summary came from the LLM operation."""
    llm_client = HttpCompactionClient(summary)

    with TestClient(create_app(state)) as client:
        assert client.post("/api/auth/guest").status_code == 200
        identity = client.get("/api/auth/me").json()
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state, identity["id"])
        seed_service = ConversationService(
            AgentRunner(
                RuleBasedPlanner(), ToolRegistry(state.session_workspace(identity["id"], sidebar["session_id"]))
            ),
            store,
            session_id=sidebar["session_id"],
        )
        completed = seed_service.run_task("seed compact history", mode="agent")
        assert completed.status == "completed"
        source = store.load_nodes(sidebar["session_id"])[-1]
        source = NodeWriter(store).update_config(source, provider_name="checkpoint-provider")
        newer = seed_service.run_task("newer branch must not replace the requested source", mode="agent")
        assert newer.status == "completed"

        def compact_application(_state, user_id: str, **_kwargs):
            return AgentApplication(
                AgentRunner(
                    LLMPlanner(llm_client, [], []), ToolRegistry(state.session_workspace(user_id, source.session_id))
                ),
                session_store(state, user_id),
                None,
            )

        monkeypatch.setattr(turn_routes, "build_user_application", compact_application)
        compact_operation = client.get("/openapi.json").json()["paths"]["/api/turns/{turn_id}/compact"]["post"]
        assert "requestBody" not in compact_operation
        response = client.post(
            f"/api/turns/{source.id}/compact",
            json={"summary": "caller-controlled summary must be ignored", "id": "caller-id"},
        )

        assert response.status_code == 201
        compacted = response.json()
        assert compacted["id"] != "caller-id"
        assert compacted["parent_id"] == source.id
        assert compacted["compactionId"] == compacted["id"]
        assert compacted["status"] == "success"
        assert compacted["data"][0][1]["content"][0]["summary"] == summary
        assert CHECKPOINT_PREAMBLE not in compacted["data"][0][1]["content"][0]["summary"]
        assert llm_client.operations == ["summarize"]
        assert resolved_provider_names == ["checkpoint-provider"]
        assert llm_client.transcripts and "seed compact history" in llm_client.transcripts[0]
        assert "newer branch must not replace the requested source" not in llm_client.transcripts[0]

        node_count = len(store.load_nodes(source.session_id))
        failing_client = HttpCompactionClient("", failure="summary provider failed")

        def failing_application(_state, user_id: str, **_kwargs):
            return AgentApplication(
                AgentRunner(
                    LLMPlanner(failing_client, [], []),
                    ToolRegistry(state.session_workspace(user_id, source.session_id)),
                ),
                session_store(state, user_id),
                None,
            )

        monkeypatch.setattr(turn_routes, "build_user_application", failing_application)
        failed = client.post(f"/api/turns/{compacted['id']}/compact")
        assert failed.status_code == 502
        assert failed.json()["detail"] == "上下文压缩失败，请稍后重试。"
        assert len(store.load_nodes(source.session_id)) == node_count

        state.active_runtime_stream_locks = {
            "__lock__": threading.RLock(),
            "keys": {(identity["id"], source.thread_id)},
        }
        conflict = client.post(f"/api/turns/{source.id}/compact")
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "当前 Thread 已有 running Turn。"

        state.active_runtime_stream_locks["keys"].clear()
        node_count = len(store.load_nodes(source.session_id))

        def missing_model(_user_id: str):
            raise ModelConfigurationError("model is missing")

        state.model_config_for_user = missing_model
        state.model_config_for_provider_name = lambda _user_id, _provider_name: missing_model(_user_id)
        missing = client.post(f"/api/turns/{source.id}/compact")
        assert missing.status_code == 422
        assert missing.json()["detail"] == "模型未配置：model is missing"
        assert len(store.load_nodes(source.session_id)) == node_count

        state.model_config_for_user = lambda _user_id: model_config
        state.model_config_for_provider_name = resolve_provider

        def sandbox_failure(*_args, **_kwargs):
            raise SandboxInitializationError("Broker unavailable")

        monkeypatch.setattr(turn_routes, "build_user_application", sandbox_failure)
        unavailable = client.post(f"/api/turns/{source.id}/compact")
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == ("Sandbox 初始化失败：Broker unavailable Agent 已停止，未降级执行。")
        assert len(store.load_nodes(source.session_id)) == node_count


def test_real_sqlite_http_sse_round_trip_reconstructs_the_persisted_turn_from_deltas(
    tmp_path: Path, monkeypatch
) -> None:
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
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        turn_id = "turn_http_sse"
        response = client.post(
            "/api/turns",
            json={
                "id": turn_id,
                "session_id": sidebar["session_id"],
                "thread_id": sidebar["thread_id"],
                "parent_id": "",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "  hello   sidebar  "}],
                },
                "permission_mode": "read_only",
                "running_mode": "agent",
            },
        )

        assert response.status_code == 200
        payloads = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
        assert payloads[-1] == f'<SSE id="{turn_id}" type="success"></SSE>'
        frames = [json.loads(payload) for payload in payloads[:-1]]
        assert frames[0]["type"] == "turn.snapshot" and frames[0]["revision"] == 0
        assert all(frame["type"] == "turn.delta" for frame in frames[1:])
        assert [frame["revision"] for frame in frames] == list(range(len(frames)))
        assert frames[-1]["patch"]["status"] == "success"
        assert all("turn" not in frame and "data" not in frame for frame in frames[1:])

        reconstructed = frames[0]["turn"]
        for frame in frames[1:]:
            reconstructed.update(frame.get("patch", {}))
            for operation in frame.get("operations", []):
                messages = reconstructed["data"][operation["data_idx"]]
                if operation["op"] == "append_message":
                    assert operation["message_idx"] == len(messages)
                    messages.append(operation["message"])
                    continue
                items = messages[operation["message_idx"]]["content"]
                if operation["op"] == "append_item":
                    assert operation["item_idx"] == len(items)
                    items.append(operation["item"])
                elif operation["op"] == "append_text":
                    items[operation["item_idx"]]["text"] += operation["delta"]
                else:
                    items[operation["item_idx"]]["status"] = operation["status"]

        turns = client.get("/api/turns", params={"session_id": sidebar["session_id"]}).json()
        assert len(turns) == 1
        assert reconstructed == turns[0]
        assert turns[0]["id"] == turn_id
        assert turns[0]["data"][0][0]["content"] == [{"type": "text", "text": "hello   sidebar", "status": "success"}]
        assert turns[0]["data"][0][1]["content"][-1]["type"] == "text"
        refreshed_sidebar = next(
            item for item in client.get("/api/sidebar-threads").json() if item["thread_id"] == sidebar["thread_id"]
        )
        assert refreshed_sidebar["title"] == "hello sidebar"
        assert refreshed_sidebar["title_is_custom"] is False
        assert state.active_turn_streams == {}
