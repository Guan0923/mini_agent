from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from backend.api.active_turn_stream import ActiveTurnStream
from backend.api.app import create_app
from backend.api.chat import routes as chat_routes
from backend.api.chat.routes import _auto_title_main_thread, _startup_failure_message, _terminal_type_for_status
from backend.api.pause_control import TurnPauseController
from backend.api.routes import turns as turn_routes
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.domain import (
    CHECKPOINT_PREAMBLE,
    AssistantMessage,
    PlanningError,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from backend.domain.runtime_state import (
    APP_VERSION,
    InMemoryNodeStore,
    NodeFrame,
    NodeWriter,
    RuntimeRootState,
    RuntimeState,
    RuntimeStateTree,
    RuntimeStateValidationError,
)
from backend.planning.context_management import ContextCompactionResult
from backend.planning.llm import LLMPlanner
from backend.planning.prompts import load_title_prompt
from backend.planning.rule_based import RuleBasedPlanner
from backend.providers import ModelConfig, ModelConfigurationError
from backend.runtime import AgentApplication, AgentRunner, ConversationService, build_application
from backend.runtime.core.context import AgentRuntime, PreparedResponse, _chat_messages_from_nodes
from backend.runtime.core.contracts import InterruptDecision
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.runtime.planning.review import REQUEST_PLAN_REVIEW_NAME
from backend.sandbox import SandboxInitializationError
from backend.storage.sqlite import SQLiteSessionStore
from backend.storage.sqlite_schema import SCHEMA, SQLiteSchemaMixin
from backend.tools import ToolRegistry


def make_turn(
    *,
    turn_id: str = "turn_1",
    thread_id: str = "session_1",
    parent: RuntimeState | RuntimeRootState | None = None,
) -> RuntimeState:
    return RuntimeState.create(
        session_id="session_1",
        thread_id=thread_id,
        id=turn_id,
        parent=parent,
        user_content=[{"type": "text", "text": "hello"}],
        provider_name="local",
    )


def test_first_main_turn_persistence_leaves_sidebar_title_for_post_run_model_request(
    tmp_path: Path, monkeypatch
) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    session = store.create_session("新对话")
    store.create_sidebar_thread(
        session_id=session.session_id,
        thread_id=session.session_id,
        title="新对话",
    )
    prompt = f"  第一条   用户消息 {'字' * 90}  "
    root = store.ensure_root_node(session.session_id, id="turn_auto_title_root")
    turn = RuntimeState.create(
        session_id=session.session_id,
        thread_id=session.session_id,
        id="turn_auto_title",
        parent=root,
        user_content=[{"type": "text", "text": prompt}],
    )

    original_put_json_object = store._put_json_object

    def fail_turn_object(connection, session_id, namespace, object_id, payload, updated_at):
        if namespace == "runtime_node" and object_id == turn.id:
            raise RuntimeError("turn object failed")
        return original_put_json_object(connection, session_id, namespace, object_id, payload, updated_at)

    monkeypatch.setattr(store, "_put_json_object", fail_turn_object)
    with pytest.raises(RuntimeError, match="turn object failed"):
        store.create_node(turn)

    assert store.get_sidebar_thread(session.session_id).title == "新对话"
    assert store.load_nodes(session.session_id) == [root]

    monkeypatch.setattr(store, "_put_json_object", original_put_json_object)
    store.create_node(turn)
    sidebar = store.get_sidebar_thread(session.session_id)
    assert sidebar is not None
    assert sidebar.title == "新对话"
    assert sidebar.title_is_custom is False


def test_turn_persistence_never_owns_sidebar_auto_titles(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))

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
        parent=store.ensure_root_node(manual.session_id, id="turn_manual_root"),
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
            parent=store.ensure_root_node(automatic.session_id, id="turn_automatic_root"),
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
    assert store.get_sidebar_thread(automatic.session_id).title == "新对话"


def test_post_run_auto_title_falls_back_once_and_skips_reference_only_or_custom_threads(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))

    class FailingConversation:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def generate_title(self, text: str) -> str:
            self.calls.append(text)
            raise PlanningError("title provider unavailable")

    failing = FailingConversation()
    fallback_session = store.create_session("新对话")
    store.create_sidebar_thread(
        session_id=fallback_session.session_id,
        thread_id=fallback_session.session_id,
        title="新对话",
    )
    writer = NodeWriter(store)
    first = writer.create(
        RuntimeState.create(
            session_id=fallback_session.session_id,
            thread_id=fallback_session.session_id,
            parent=store.ensure_root_node(fallback_session.session_id, id="turn_fallback_root"),
            user_content=[{"type": "text", "text": "  第一条   消息超过十个字符  "}],
        )
    )
    first = writer.finalize(first, "success")

    _auto_title_main_thread(
        failing,
        store,
        session_id=fallback_session.session_id,
        thread_id=fallback_session.session_id,
        turn_id=first.id,
    )

    assert failing.calls == ["  第一条   消息超过十个字符  "]
    assert store.get_sidebar_thread(fallback_session.session_id).title == "第一条 消息超过十个"
    second = writer.create(
        RuntimeState.create(
            session_id=fallback_session.session_id,
            thread_id=fallback_session.session_id,
            parent=first,
            user_content=[{"type": "text", "text": "第二条消息"}],
        )
    )
    second = writer.finalize(second, "success")
    _auto_title_main_thread(
        failing,
        store,
        session_id=fallback_session.session_id,
        thread_id=fallback_session.session_id,
        turn_id=second.id,
    )
    assert len(failing.calls) == 1

    reference_session = store.create_session("新对话")
    store.create_sidebar_thread(
        session_id=reference_session.session_id,
        thread_id=reference_session.session_id,
        title="新对话",
    )
    reference = writer.create(
        RuntimeState.create(
            session_id=reference_session.session_id,
            thread_id=reference_session.session_id,
            parent=store.ensure_root_node(reference_session.session_id, id="turn_reference_root"),
            user_content=[
                {
                    "type": "text",
                    "text": "",
                    "references": [{"path": "README.md", "source": "project"}],
                }
            ],
        )
    )
    reference = writer.finalize(reference, "success")
    _auto_title_main_thread(
        failing,
        store,
        session_id=reference_session.session_id,
        thread_id=reference_session.session_id,
        turn_id=reference.id,
    )
    assert store.get_sidebar_thread(reference_session.session_id).title == "新对话"
    assert len(failing.calls) == 1

    custom_session = store.create_session("手工标题")
    store.create_sidebar_thread(
        session_id=custom_session.session_id,
        thread_id=custom_session.session_id,
        title="手工标题",
        title_is_custom=True,
    )
    custom = writer.create(
        RuntimeState.create(
            session_id=custom_session.session_id,
            thread_id=custom_session.session_id,
            parent=store.ensure_root_node(custom_session.session_id, id="turn_custom_root"),
            user_content=[{"type": "text", "text": "不能覆盖"}],
        )
    )
    custom = writer.finalize(custom, "success")
    _auto_title_main_thread(
        failing,
        store,
        session_id=custom_session.session_id,
        thread_id=custom_session.session_id,
        turn_id=custom.id,
    )
    assert store.get_sidebar_thread(custom_session.session_id).title == "手工标题"
    assert len(failing.calls) == 1


def test_post_run_auto_title_preserves_manual_rename_during_model_request(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    session = store.create_session("新对话")
    store.create_sidebar_thread(session_id=session.session_id, thread_id=session.session_id, title="新对话")
    writer = NodeWriter(store)
    node = writer.create(
        RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            parent=store.ensure_root_node(session.session_id, id="turn_rename_root"),
            user_content=[{"type": "text", "text": "模型正在命名"}],
        )
    )
    node = writer.finalize(node, "success")

    class RenamingConversation:
        def generate_title(self, _text: str) -> str:
            store.update_sidebar_thread(session.session_id, title="运行中手工改名", title_is_custom=True)
            return "模型标题"

    _auto_title_main_thread(
        RenamingConversation(),
        store,
        session_id=session.session_id,
        thread_id=session.session_id,
        turn_id=node.id,
    )

    sidebar = store.get_sidebar_thread(session.session_id)
    assert sidebar is not None
    assert sidebar.title == "运行中手工改名"
    assert sidebar.title_is_custom is True


def test_post_run_auto_title_does_not_retry_after_the_root_turn(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    session = store.create_session("新对话")
    store.create_sidebar_thread(session_id=session.session_id, thread_id=session.session_id, title="新对话")
    writer = NodeWriter(store)
    root = writer.create(
        RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            parent=store.ensure_root_node(session.session_id, id="turn_default_title_root"),
            user_content=[{"type": "text", "text": "第一条消息"}],
        )
    )
    root = writer.finalize(root, "success")

    class DefaultTitleConversation:
        def __init__(self) -> None:
            self.calls = 0

        def generate_title(self, _text: str) -> str:
            self.calls += 1
            return "新对话"

    conversation = DefaultTitleConversation()
    _auto_title_main_thread(
        conversation,
        store,
        session_id=session.session_id,
        thread_id=session.session_id,
        turn_id=root.id,
    )
    assert conversation.calls == 1

    second = writer.create(
        RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            parent=root,
            user_content=[{"type": "text", "text": "第二条消息"}],
        )
    )
    second = writer.finalize(second, "success")
    _auto_title_main_thread(
        conversation,
        store,
        session_id=session.session_id,
        thread_id=session.session_id,
        turn_id=second.id,
    )

    assert conversation.calls == 1
    assert store.get_sidebar_thread(session.session_id).title == "新对话"


def test_root_and_turn_shapes_are_strict() -> None:
    root = RuntimeRootState.create("session_1", id="turn_root")
    assert root.to_dict() == {"session_id": "session_1", "thread_id": "session_1", "id": "turn_root"}
    with pytest.raises(RuntimeStateValidationError, match="must contain only"):
        RuntimeRootState.from_dict({**root.to_dict(), "status": "success"})

    turn = make_turn(parent=root)
    assert turn.version == APP_VERSION == "0.0.2"
    assert turn.parent_id == root.id
    assert turn.parent_session_id == turn.parent_thread_id == root.session_id
    assert turn.compaction_id == turn.id
    assert turn.first_kept_item_size == 8
    assert turn.model["temperature"] == 0.0
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


def test_turn_allows_consecutive_assistant_messages_but_rejects_other_role_violations() -> None:
    turn = make_turn().to_dict()
    turn["data"][0].append(
        {
            "role": "assistant",
            "content": [
                {
                    "type": "subagent",
                    "event": "agent_report",
                    "status": "success",
                    "text": "thread_path: /root/worker\nthread_status: success\ntask_result: 完成",
                    "delivery_id": "agent_report_1",
                }
            ],
        }
    )
    assert RuntimeState.from_dict(turn).data[0][-1]["role"] == "assistant"

    first_assistant = make_turn().to_dict()
    first_assistant["data"][0][0]["role"] = "assistant"
    with pytest.raises(RuntimeStateValidationError, match="role must be user"):
        RuntimeState.from_dict(first_assistant)

    consecutive_user = make_turn().to_dict()
    consecutive_user["data"][0].insert(
        1,
        {"role": "user", "content": [{"type": "text", "text": "again", "status": "success"}]},
    )
    with pytest.raises(RuntimeStateValidationError, match="Consecutive user"):
        RuntimeState.from_dict(consecutive_user)


def test_turn_workspace_paths_round_trip_persist_fork_and_compact(tmp_path: Path) -> None:
    session_workspace = (tmp_path / "session-workspace").resolve()
    project_workspace = (tmp_path / "project-workspace").resolve()
    session_workspace.mkdir()
    project_workspace.mkdir()
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    session = store.create_session("workspace paths")
    root = store.ensure_root_node(session.session_id, id="turn_workspace_root")
    turn = RuntimeState.create(
        session_id=session.session_id,
        thread_id=session.session_id,
        id="turn_workspace",
        parent=root,
        user_content="hello",
        cwd=str(session_workspace),
        project_cwd=str(project_workspace),
    )

    restored = RuntimeState.from_dict(turn.to_dict())
    store.create_node(turn)
    persisted = store.find_node(turn.id)
    assert isinstance(persisted, RuntimeState)
    assert (persisted.cwd, persisted.project_cwd) == (str(session_workspace), str(project_workspace))

    tree = RuntimeStateTree([root, turn])
    forked = tree.fork(turn, id="turn_workspace_fork", thread_id="thread_workspace_fork")
    compacted = tree.compact(turn, "summary", id="turn_workspace_compact")
    assert (restored.cwd, restored.project_cwd) == (str(session_workspace), str(project_workspace))
    assert (forked.cwd, forked.project_cwd) == (str(session_workspace), str(project_workspace))
    assert (compacted.cwd, compacted.project_cwd) == (str(session_workspace), str(project_workspace))

    invalid = turn.to_dict()
    invalid["project_cwd"] = "relative/project"
    with pytest.raises(RuntimeStateValidationError, match="project_cwd must be an absolute path"):
        RuntimeState.from_dict(invalid)


def test_writer_emits_one_baseline_then_exact_incremental_operations() -> None:
    frames: list[NodeFrame] = []
    store = InMemoryNodeStore()
    writer = NodeWriter(store, emit=frames.append)
    turn = writer.create(make_turn(parent=store.ensure_root_node("session_1", id="turn_root")))
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


def test_model_context_projects_success_items_and_complete_failed_tool_pairs() -> None:
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
                            "tool": "write_file",
                            "content": "denied",
                            "status": "failed",
                            "retryable": False,
                            "failure_code": "user_denied",
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
    assert [
        (tool.call_id, tool.content, tool.status, tool.retryable, tool.failure_code)
        for tool in projected[1].tool_messages
    ] == [
        ("call_ok", "done", "succeeded", None, None),
        ("call_failed", "denied", "failed", False, "user_denied"),
    ]


def test_model_context_omits_malformed_tool_pairs() -> None:
    tool_items = [
        {
            "type": "tool_call",
            "call_id": "orphan",
            "name": "glob",
            "arguments": {},
            "status": "success",
        },
        {
            "type": "tool_call",
            "call_id": "duplicate",
            "name": "read_file",
            "arguments": {},
            "status": "success",
        },
        {
            "type": "tool_result",
            "call_id": "duplicate",
            "content": "first",
            "status": "success",
        },
        {
            "type": "tool_result",
            "call_id": "duplicate",
            "content": "second",
            "status": "success",
        },
        {
            "type": "tool_result",
            "call_id": "reversed",
            "content": "too early",
            "status": "success",
        },
        {
            "type": "tool_call",
            "call_id": "reversed",
            "name": "grep",
            "arguments": {},
            "status": "success",
        },
        {
            "type": "tool_call",
            "call_id": "wrong_name",
            "name": "write_file",
            "arguments": {},
            "status": "failed",
        },
        {
            "type": "tool_result",
            "call_id": "wrong_name",
            "tool": "edit_file",
            "content": "failed",
            "status": "failed",
        },
        {
            "type": "tool_call",
            "call_id": "wrong_status",
            "name": "write_file",
            "arguments": {},
            "status": "success",
        },
        {
            "type": "tool_result",
            "call_id": "wrong_status",
            "tool": "write_file",
            "content": "failed",
            "status": "failed",
        },
    ]
    node = RuntimeState.create(
        session_id="session_malformed_tools",
        thread_id="session_malformed_tools",
        user_content="inspect",
        data=[
            [
                {"role": "user", "content": [{"type": "text", "text": "inspect", "status": "success"}]},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "kept", "status": "success"}, *tool_items],
                },
            ]
        ],
    )

    projected = _chat_messages_from_nodes([node])

    assert isinstance(projected[1], AssistantMessage)
    assert projected[1].content == "kept"
    assert projected[1].tool_messages == []


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
            {"content": "redirect", "delivery_id": "delivery_1"},
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
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        assert client.get("/api/turns/anything/stream").status_code == 404
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state)
        writer = NodeWriter(store)
        turn = writer.create(
            RuntimeState.create(
                session_id=sidebar["session_id"],
                thread_id=sidebar["thread_id"],
                id="turn_completed_stream",
                parent=store.ensure_root_node(sidebar["session_id"], id="turn_completed_root"),
                user_content=[{"type": "text", "text": "hello"}],
            )
        )
        writer.finalize(turn, "success")

        response = client.get("/api/turns/turn_completed_stream/stream")
        payloads = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
        assert json.loads(payloads[0])["turn"]["status"] == "success"
        assert payloads[1] == '<SSE id="turn_completed_stream" type="success"></SSE>'
        assert client.get("/api/turns/missing/stream").status_code == 404


def test_root_turn_is_listed_but_rejects_every_turn_operation(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state)
        assert store.load_nodes(sidebar["session_id"]) == []
        root = store.ensure_root_node(sidebar["session_id"], id="turn_root_operations")
        assert store.ensure_root_node(sidebar["session_id"], id="turn_ignored") == root

        requests = [
            ("get", f"/api/turns/{root.id}/stream", None),
            (
                "post",
                f"/api/turns/{root.id}/rewind",
                {"message": {"role": "user", "content": [{"type": "text", "text": "x"}]}},
            ),
            ("post", f"/api/turns/{root.id}/resume", {}),
            ("post", f"/api/turns/{root.id}/pause", None),
            (
                "post",
                f"/api/turns/{root.id}/steer",
                {"delivery_id": "s1", "message_ids": ["message-1"]},
            ),
            ("post", f"/api/turns/{root.id}/fork", {}),
            ("post", f"/api/turns/{root.id}/compact", None),
            ("patch", f"/api/turns/{root.id}/current-data", {"current_data_idx": 0}),
            ("patch", f"/api/turns/{root.id}/config", {}),
        ]
        for method, url, body in requests:
            response = client.request(method, url, json=body)
            assert response.status_code == 409, (method, url, response.text)
            assert "根 Turn" in response.json()["detail"]


def test_writer_rejects_a_delta_for_an_existing_turn_without_a_stream_baseline() -> None:
    store = InMemoryNodeStore()
    existing = make_turn(parent=store.ensure_root_node("session_1", id="turn_root"))
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
        store = InMemoryNodeStore()
        writer = NodeWriter(store, emit=frames.append)
        turn = writer.create(make_turn(parent=store.ensure_root_node("session_1", id="turn_root")))
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
    root = RuntimeRootState.create("session_1", id="turn_root")
    store = InMemoryNodeStore([root, make_turn(parent=root)])
    with pytest.raises(ValueError, match="one running Turn"):
        store.create_node(make_turn(turn_id="turn_2", parent=root))
    fork_payload = make_turn(turn_id="turn_fork", parent=root).to_dict()
    fork_payload["thread_id"] = "thread_fork"
    fork_payload["parent_thread_id"] = "session_1"
    store.create_node(RuntimeState.from_dict(fork_payload))


def test_sqlite_rewind_fork_and_compact_are_atomic(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    session = store.create_session("main")
    store.create_sidebar_thread(session_id=session.session_id, thread_id=session.session_id, title="main")
    writer = NodeWriter(store)
    root = store.ensure_root_node(session.session_id, id="turn_root")
    original = writer.create(
        RuntimeState.create(
            session_id=session.session_id,
            thread_id=session.session_id,
            id="turn_original",
            parent=root,
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
    assert forked.parent_id == root.id
    assert forked.parent_thread_id == session.session_id
    assert forked.data == selected.data
    assert forked.compaction_id == forked.id
    tree = RuntimeStateTree(store.load_nodes(session.session_id))
    assert [node.id for node in tree.ancestors(selected)] == [root.id, selected.id]
    assert [node.id for node in tree.ancestors(forked)] == [root.id, forked.id]
    nested_fork = store.fork_turn_node("turn_fork", new_turn_id="turn_nested_fork", thread_id="thread_nested")
    tree = RuntimeStateTree(store.load_nodes(session.session_id))
    assert nested_fork.parent_thread_id == root.thread_id
    assert [node.id for node in tree.ancestors(nested_fork)] == [root.id, nested_fork.id]

    compacted = store.create_compact_turn("turn_original", "summary", new_turn_id="turn_compact")
    items = compacted.assistant_items
    assert compacted.compaction_id == compacted.id
    assert items[0] == {"type": "compaction", "summary": "summary", "kept_item_count": 8, "status": "success"}
    assert len(items[1:]) == 8


def test_missing_ancestor_and_bad_version_index_are_rejected() -> None:
    root = RuntimeRootState.create("session_1", id="turn_root")
    first = make_turn(parent=root)
    child = make_turn(turn_id="turn_2", parent=first)
    with pytest.raises(RuntimeStateValidationError, match="parent is missing"):
        RuntimeStateTree([child]).ancestors(child)
    payload = first.to_dict()
    payload["current_data_idx"] = 5
    with pytest.raises(RuntimeStateValidationError, match="out of range"):
        RuntimeState.from_dict(payload)

    bad_thread = child.to_dict()
    bad_thread["parent_thread_id"] = "thread_wrong"
    with pytest.raises(RuntimeStateValidationError, match="parent_thread_id"):
        RuntimeStateTree([root, first, RuntimeState.from_dict(bad_thread)]).ancestors(("session_1", "turn_2"))

    cross_session = child.to_dict()
    cross_session["session_id"] = "session_2"
    cross_session["thread_id"] = "session_2"
    with pytest.raises(RuntimeStateValidationError, match="across Sessions"):
        RuntimeStateTree([root, first, RuntimeState.from_dict(cross_session)]).ancestors(("session_2", "turn_2"))


@pytest.mark.parametrize("schema_version", [9, 10, 11, 12, 13, 14, 15])
def test_legacy_database_is_rejected_without_migration_or_deletion(tmp_path: Path, schema_version: int) -> None:
    path = tmp_path / f"v{schema_version}.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "INSERT INTO store_metadata(session_id,schema_version,created_at,updated_at) VALUES ('s',?,'x','x')",
        (schema_version,),
    )
    connection.commit()
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="requires v16"):
        SQLiteSchemaMixin._assert_supported_schema(connection)
    assert path.exists()
    connection.close()
    assert path.read_bytes() == before


def test_pause_targets_only_the_requested_turn_in_parallel_threads(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        assert client.get("/api/turns", params={"session_id": sidebar["session_id"]}).json() == []
        store = session_store(state)
        writer = NodeWriter(store)
        original = writer.create(
            RuntimeState.create(
                session_id=sidebar["session_id"],
                thread_id=sidebar["thread_id"],
                id="turn_main",
                parent=store.ensure_root_node(sidebar["session_id"], id="turn_main_root"),
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
            "turn_main": main_pause,
            "turn_fork": fork_pause,
        }
        response = client.post("/api/turns/turn_main/pause")
        assert response.status_code == 200
        assert main_pause.is_requested() is True
        assert fork_pause.is_requested() is False
        assert store.find_node("turn_main").status == "running"
        assert store.find_node("turn_fork").status == "running"


def test_fork_sidebar_title_always_appends_branch_suffix(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / "web")
    with TestClient(create_app(state)) as client:
        source_sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state)
        writer = NodeWriter(store)
        source = writer.create(
            RuntimeState.create(
                session_id=source_sidebar["session_id"],
                thread_id=source_sidebar["thread_id"],
                id="turn_fork_source",
                parent=store.ensure_root_node(source_sidebar["session_id"], id="turn_fork_root"),
                user_content=[{"type": "text", "text": "源对话标题"}],
            )
        )
        writer.finalize(source, "success")
        store.update_sidebar_thread(source_sidebar["thread_id"], title="源对话标题", title_is_custom=False)

        inherited_response = client.post("/api/turns/turn_fork_source/fork", json={})
        assert inherited_response.status_code == 201
        inherited_payload = inherited_response.json()
        inherited = inherited_payload["sidebar_thread"]
        assert inherited["title"] == "源对话标题（分支）"
        assert inherited["title_is_custom"] is False

        nested_response = client.post(
            f"/api/turns/{inherited_payload['turn']['id']}/fork",
            json={"title": "这个旧字段必须被忽略"},
        )
        assert nested_response.status_code == 201
        nested = nested_response.json()["sidebar_thread"]
        assert nested["title"] == "源对话标题（分支）（分支）"
        assert nested["title_is_custom"] is False

        renamed = client.patch(
            f"/api/sidebar-threads/{source_sidebar['thread_id']}",
            json={"title": "源对话已改名"},
        )
        assert renamed.status_code == 200
        assert store.get_sidebar_thread(inherited["thread_id"]).title == "源对话标题（分支）"

        explicit_response = client.post(
            "/api/turns/turn_fork_source/fork",
            json={"title": "手工分支标题"},
        )
        assert explicit_response.status_code == 201
        explicit = explicit_response.json()["sidebar_thread"]
        assert explicit["title"] == "源对话已改名（分支）"
        assert explicit["title_is_custom"] is False


def test_plan_handoff_creates_agent_child_with_approved_plan_message(tmp_path: Path) -> None:
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

    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
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
    nodes = store.load_nodes(service.active_session.session_id)
    assert isinstance(nodes[0], RuntimeRootState)
    turns = [node for node in nodes if isinstance(node, RuntimeState)]
    assert len(turns) == 2
    plan, agent = turns
    assert plan.status == agent.status == "success"
    assert plan.running_mode == "plan"
    assert agent.running_mode == "agent"
    assert agent.parent_id == plan.id
    assert plan.selected_messages[0]["content"] == [{"type": "text", "text": "plan the change", "status": "success"}]
    assert any(
        item.get("event") == "handoff_created" and item.get("text") == "Implement the reviewed change."
        for item in plan.assistant_items
    )
    assert agent.selected_messages[0]["content"] == [
        {
            "type": "text",
            "text": "<approved_plan>\nImplement the reviewed change.\n</approved_plan>",
            "status": "success",
        }
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
    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
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
        {
            "type": "text",
            "text": "<approved_plan>\nImplement after a real compaction.\n</approved_plan>",
            "status": "success",
        }
    ]
    assert [node.status for node in store.load_nodes(session.session_id) if isinstance(node, RuntimeState)] == [
        "success",
        "success",
        "success",
    ]


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

    store = SQLiteSessionStore(ClientPaths(tmp_path / "data"))
    service = ConversationService(AgentRunner(FailingCompactionPlanner(), ToolRegistry()), store)

    result = service.run_task(
        "plan a change",
        mode="plan",
        interrupt=lambda _request: InterruptDecision("implement_and_compaction"),
    )

    assert result.status == "completed"
    assert result.mode == "plan"
    assert service.active_session is not None
    turns = [node for node in store.load_nodes(service.active_session.session_id) if isinstance(node, RuntimeState)]
    assert len(turns) == 1
    assert turns[0].status == "success" and turns[0].running_mode == "plan"
    error = next(item for item in turns[0].assistant_items if item["type"] == "error")
    assert "summary provider unavailable" in error["message"]
    assert "super-secret" not in error["message"]
    assert "[REDACTED]" in error["message"]


def test_http_resume_plan_compaction_handoff_uses_one_bridge_and_fake_model(
    tmp_path: Path,
    monkeypatch,
    local_sandbox_runtime: None,
) -> None:
    state = WebAppState(tmp_path / "web")
    monkeypatch.setattr(
        state,
        "model_config",
        lambda *_args, **_kwargs: ModelConfig("test", "https://example.test/v1", "fake-plan-model"),
    )

    class FakePlanClient:
        context_size = 100_000

        def __init__(self) -> None:
            self.operations: list[tuple[str | None, str]] = []

        def estimate_tokens(self, messages, tools, request_parameters) -> int:
            return 100

        def estimate_input_tokens(self, messages, tools, request_parameters) -> int:
            return 100

        def run(self, runtime: AgentRuntime) -> PreparedResponse:
            operation = runtime.exchange.operation
            self.operations.append((operation, runtime.run.mode))
            if operation == "summarize":
                return PreparedResponse(AssistantMessage(content="HTTP resumed Plan summary."), {"total_tokens": 2})
            if operation == "title":
                return PreparedResponse(AssistantMessage(content="恢复计划测试"), {"total_tokens": 1})
            if runtime.run.mode == "plan":
                return PreparedResponse(
                    AssistantMessage(
                        tool_messages=[
                            ToolMessage(
                                name=REQUEST_PLAN_REVIEW_NAME,
                                call_id="review_http_resume",
                                arguments={"plan": "Implement the HTTP resumed Plan."},
                            )
                        ]
                    ),
                    {"total_tokens": 3},
                )
            return PreparedResponse(AssistantMessage(content="Implemented through HTTP resume."), {"total_tokens": 4})

    fake_client = FakePlanClient()

    def local_application(_state, *, session_id: str, workspace=None, **_kwargs):
        application = build_application(
            workspace or state.session_workspace(session_id),
            planner_name="rule",
            paths=state.paths,
        )
        application.runner.planner = LLMPlanner(fake_client, [], [])
        return application

    monkeypatch.setattr(chat_routes, "build_local_application", local_application)

    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        initial_application = local_application(state, session_id=sidebar["session_id"])
        initial = initial_application.open_conversation(sidebar["session_id"])
        paused = initial.run_task(
            "plan an HTTP resumed change",
            mode="plan",
            suspend_requested=lambda: True,
        )
        assert paused.status == "cancelled" and paused.stop_reason == "user_paused"
        store = session_store(state)
        assert store.find_node(paused.turn_id).status == "paused"

        accepted = client.post(
            f"/api/turns/{paused.turn_id}/resume",
            json={"running_mode": "plan", "permission_mode": "read_only"},
        )
        assert accepted.status_code == 202
        decision_id = ""
        deadline = monotonic() + 10
        while monotonic() < deadline:
            turn = store.find_node(paused.turn_id)
            if isinstance(turn, RuntimeState):
                decision_id = next(
                    (
                        str(item.get("decision_id") or "")
                        for item in turn.assistant_items
                        if item.get("event") == "decision_requested"
                    ),
                    "",
                )
            if decision_id:
                decision = client.post(
                    "/api/decisions",
                    json={"decision_id": decision_id, "choice": "implement_and_compaction"},
                )
                if decision.status_code == 200:
                    break
            sleep(0.02)
        else:
            pytest.fail("resumed Plan decision did not become active")

        deadline = monotonic() + 15
        while monotonic() < deadline:
            turns = [node for node in store.load_nodes(sidebar["session_id"]) if isinstance(node, RuntimeState)]
            if len(turns) == 3 and all(turn.status in {"success", "failed"} for turn in turns):
                break
            sleep(0.02)
        response = client.get(
            f"/api/turns/{paused.turn_id}/stream",
            params={"session_id": sidebar["session_id"]},
        )

        assert response.status_code == 200
        payloads = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
        assert payloads[-1].endswith('type="success"></SSE>')
        frames = [json.loads(payload) for payload in payloads[:-1]]
        snapshots = [frame["turn"] for frame in frames if frame["type"] == "turn.snapshot"]
        assert len(snapshots) == 3
        plan, compact, agent = snapshots
        assert compact["parent_id"] == plan["id"] and agent["parent_id"] == compact["id"]
        assert [plan["running_mode"], compact["running_mode"], agent["running_mode"]] == [
            "plan",
            "plan",
            "agent",
        ]

        turns = [node for node in store.load_nodes(sidebar["session_id"]) if isinstance(node, RuntimeState)]
        assert [turn.status for turn in turns] == ["success", "success", "success"]
        trace = store.load_turn_trace(sidebar["session_id"], plan["id"], plan["current_data_idx"])
        assert trace is not None
        trace_events = [item.item.get("event") for item in trace.items]
        assert trace_events.count("decision_requested") == 1
        assert trace_events.count("approval_granted") == 1

    assert ("summarize", "plan") in fake_client.operations
    assert fake_client.operations.count(("decision", "agent")) == 1


def test_sse_terminal_mapping_distinguishes_user_pause_from_network_pause() -> None:
    assert _terminal_type_for_status("success", None) == "success"
    assert _terminal_type_for_status("paused", "user") == "success"
    assert _terminal_type_for_status("paused", "network") == "failed"
    assert _terminal_type_for_status("failed", "agent") == "failed"


def test_sandbox_startup_failure_message_explains_fail_closed_behavior() -> None:
    message = _startup_failure_message(SandboxInitializationError("Windows Sandbox Broker 未安装或当前不可用。"))

    assert message == ("Sandbox 初始化失败：Windows Sandbox Broker 未安装或当前不可用。 Agent 已停止，未降级执行。")


def test_http_sse_surfaces_sandbox_failure_before_turn_baseline(tmp_path: Path, monkeypatch) -> None:
    state = WebAppState(tmp_path / "web")
    monkeypatch.setattr(
        state, "model_config", lambda *_args, **_kwargs: ModelConfig("test", "https://example.test/v1", "test")
    )

    def fail_sandbox_startup(*_args, **_kwargs):
        raise SandboxInitializationError("Windows Sandbox Broker 已安装，但健康检查未通过。")

    monkeypatch.setattr(chat_routes, "build_local_application", fail_sandbox_startup)

    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        turn_id = "turn_sandbox_startup_failure"
        accepted = client.post(
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
        assert accepted.status_code == 202
        response = client.get(
            f"/api/turns/{turn_id}/stream",
            params={"session_id": sidebar["session_id"]},
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
    state = WebAppState(tmp_path / "web")
    model_config = ModelConfig("secret", "https://example.test/v1", "checkpoint-model")
    resolved_provider_names: list[str | None] = []

    def resolve_provider(provider_name: str | None = None):
        resolved_provider_names.append(provider_name)
        return model_config

    state.model_config = resolve_provider
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
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        store = session_store(state)
        seed_service = ConversationService(
            AgentRunner(RuleBasedPlanner(), ToolRegistry(state.session_workspace(sidebar["session_id"]))),
            store,
            session_id=sidebar["session_id"],
        )
        completed = seed_service.run_task("seed compact history", mode="agent")
        assert completed.status == "completed"
        source = store.load_nodes(sidebar["session_id"])[-1]
        source = NodeWriter(store).update_config(source, provider_name="checkpoint-provider")
        newer = seed_service.run_task("newer branch must not replace the requested source", mode="agent")
        assert newer.status == "completed"

        def compact_application(_state, **_kwargs):
            return AgentApplication(
                AgentRunner(LLMPlanner(llm_client, [], []), ToolRegistry(state.session_workspace(source.session_id))),
                session_store(state),
            )

        monkeypatch.setattr(turn_routes, "build_local_application", compact_application)
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

        def failing_application(_state, **_kwargs):
            return AgentApplication(
                AgentRunner(
                    LLMPlanner(failing_client, [], []),
                    ToolRegistry(state.session_workspace(source.session_id)),
                ),
                session_store(state),
            )

        monkeypatch.setattr(turn_routes, "build_local_application", failing_application)
        failed = client.post(f"/api/turns/{compacted['id']}/compact")
        assert failed.status_code == 502
        assert failed.json()["detail"] == "上下文压缩失败，请稍后重试。"
        assert len(store.load_nodes(source.session_id)) == node_count

        state.active_runtime_stream_locks = {
            "__lock__": threading.RLock(),
            "keys": {source.thread_id},
        }
        conflict = client.post(f"/api/turns/{source.id}/compact")
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == "当前 Thread 已有 running Turn。"

        state.active_runtime_stream_locks["keys"].clear()
        node_count = len(store.load_nodes(source.session_id))

        def missing_model(_provider_name: str | None = None):
            raise ModelConfigurationError("model is missing")

        state.model_config = missing_model
        missing = client.post(f"/api/turns/{source.id}/compact")
        assert missing.status_code == 422
        assert missing.json()["detail"] == "模型未配置：model is missing"
        assert len(store.load_nodes(source.session_id)) == node_count

        state.model_config = resolve_provider

        def sandbox_failure(*_args, **_kwargs):
            raise SandboxInitializationError("Broker unavailable")

        monkeypatch.setattr(turn_routes, "build_local_application", sandbox_failure)
        unavailable = client.post(f"/api/turns/{source.id}/compact")
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"] == ("Sandbox 初始化失败：Broker unavailable Agent 已停止，未降级执行。")
        assert len(store.load_nodes(source.session_id)) == node_count


def test_real_sqlite_http_sse_round_trip_reconstructs_the_persisted_turn_from_deltas(
    tmp_path: Path,
    monkeypatch,
    local_sandbox_runtime: None,
) -> None:
    state = WebAppState(tmp_path / "web")
    monkeypatch.setattr(
        state, "model_config", lambda *_args, **_kwargs: ModelConfig("test", "https://example.test/v1", "test")
    )

    def local_application(_state, *, session_id: str, workspace=None, **_kwargs):
        return build_application(
            workspace or state.session_workspace(session_id),
            planner_name="rule",
            paths=state.paths,
        )

    monkeypatch.setattr(chat_routes, "build_local_application", local_application)

    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        assert client.get("/api/turns", params={"session_id": sidebar["session_id"]}).json() == []
        turn_id = "turn_http_sse"
        accepted = client.post(
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

        assert accepted.status_code == 202
        assert accepted.json() == {
            "turn_id": turn_id,
            "delivery_id": f"turn-start:{turn_id}",
            "status": "accepted",
        }
        stream_response = client.get(
            f"/api/turns/{turn_id}/stream",
            params={"session_id": sidebar["session_id"]},
        )
        payloads = [
            line.removeprefix("data: ") for line in stream_response.text.splitlines() if line.startswith("data: ")
        ]
        assert payloads[-1] == f'<SSE id="{turn_id}" type="success"></SSE>'
        frames = [json.loads(payload) for payload in payloads[:-1]]
        assert frames[0]["type"] == "turn.snapshot" and frames[0]["revision"] == 0
        assert all(frame["type"] == "turn.delta" for frame in frames[1:])
        assert [frame["revision"] for frame in frames] == list(range(len(frames)))
        if len(frames) == 1:
            assert frames[0]["turn"]["status"] == "success"
        else:
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
        assert len(turns) == 2
        root, persisted = turns
        assert root == {
            "session_id": sidebar["session_id"],
            "thread_id": sidebar["session_id"],
            "id": root["id"],
        }
        assert root["id"].startswith("turn_")
        assert reconstructed == persisted
        assert persisted["id"] == turn_id
        assert persisted["parent_id"] == root["id"]
        assert persisted["compactionId"] == turn_id
        assert persisted["data"][0][0]["content"] == [{"type": "text", "text": "hello   sidebar", "status": "success"}]
        assert persisted["data"][0][1]["content"][-1]["type"] == "text"
        refreshed_sidebar = next(
            item for item in client.get("/api/sidebar-threads").json() if item["thread_id"] == sidebar["thread_id"]
        )
        assert refreshed_sidebar["title"] == "hello side"
        assert refreshed_sidebar["title_is_custom"] is False
        assert state.active_turn_streams == {}


def test_real_http_sse_progressively_loads_user_skill_through_read_file(
    tmp_path: Path,
    monkeypatch,
    local_sandbox_runtime: None,
) -> None:
    state = WebAppState(tmp_path / "web")
    marker = "PROGRESSIVE_SKILL_MARKER"
    manifest = state.paths.skills_dir / "demo-skill" / "SKILL.md"
    reference = manifest.parent / "references" / "guide.md"
    reference.parent.mkdir(parents=True)
    manifest.write_text(
        "---\nname: demo-skill\ndescription: Use for progressive loading tests.\n---\n"
        f"Read references/guide.md and remember {marker}.\n",
        encoding="utf-8",
    )
    reference.write_text("Nested Skill reference.", encoding="utf-8")
    monkeypatch.setattr(
        state, "model_config", lambda *_args, **_kwargs: ModelConfig("test", "https://example.test/v1", "test")
    )

    class ProgressiveSkillClient:
        def __init__(self) -> None:
            self.decision_requests: list[list] = []

        def run(self, runtime: AgentRuntime) -> PreparedResponse:
            if runtime.exchange.operation == "title":
                return PreparedResponse(AssistantMessage(content="Skill loading"))
            messages = list(runtime.exchange.messages)
            self.decision_requests.append(messages)
            if len(self.decision_requests) == 1:
                return PreparedResponse(
                    AssistantMessage(
                        tool_messages=[
                            ToolMessage(
                                name="read_file",
                                call_id="load_demo_skill",
                                arguments={"path": str(manifest)},
                            )
                        ]
                    )
                )
            loaded = any(
                marker in (tool.content or "")
                for message in messages
                if isinstance(message, AssistantMessage)
                for tool in message.tool_messages
            )
            return PreparedResponse(AssistantMessage(content=f"Skill loaded: {loaded}"))

    model = ProgressiveSkillClient()

    def local_application(_state, *, session_id: str, workspace=None, **_kwargs):
        application = build_application(
            workspace or state.session_workspace(session_id),
            planner_name="rule",
            paths=state.paths,
        )
        tools = application.runner.tools
        application.runner.planner = LLMPlanner(model, tools.specs(), tools.read_only_specs())
        return application

    monkeypatch.setattr(chat_routes, "build_local_application", local_application)

    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        accepted = client.post(
            "/api/turns",
            json={
                "id": "turn_progressive_skill",
                "session_id": sidebar["session_id"],
                "thread_id": sidebar["thread_id"],
                "parent_id": "",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Use $demo-skill."}],
                },
                "permission_mode": "read_only",
                "running_mode": "agent",
            },
        )

        assert accepted.status_code == 202
        response = client.get(
            "/api/turns/turn_progressive_skill/stream",
            params={"session_id": sidebar["session_id"]},
        )
        assert response.status_code == 200
        assert '<SSE id="turn_progressive_skill" type="success"></SSE>' in response.text
        assert len(model.decision_requests) == 2
        first_system = model.decision_requests[0][0].content or ""
        assert "Use for progressive loading tests." in first_system
        assert manifest.as_posix() in first_system
        assert marker not in first_system
        assert any(
            marker in (tool.content or "")
            for message in model.decision_requests[1]
            if isinstance(message, AssistantMessage)
            for tool in message.tool_messages
        )

        persisted = client.get("/api/turns", params={"session_id": sidebar["session_id"]}).json()[-1]
        items = [item for message in persisted["data"][0] for item in message["content"]]
        tool_call = next(item for item in items if item["type"] == "tool_call")
        tool_result = next(item for item in items if item["type"] == "tool_result")
        assert tool_call["call_id"] == tool_result["call_id"] == "load_demo_skill"
        assert tool_result["status"] == "success"
        assert all(item["type"] != "skill_snapshot" for item in items)
        assert any(item.get("text") == "Skill loaded: True" for item in items)


def test_real_http_sse_generates_title_with_isolated_model_request(
    tmp_path: Path,
    monkeypatch,
    local_sandbox_runtime: None,
) -> None:
    state = WebAppState(tmp_path / "web")
    monkeypatch.setattr(
        state, "model_config", lambda *_args, **_kwargs: ModelConfig("test", "https://example.test/v1", "test")
    )

    class DedicatedTitleClient:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def run(self, runtime: AgentRuntime) -> PreparedResponse:
            self.requests.append(
                {
                    "operation": runtime.exchange.operation,
                    "messages": list(runtime.exchange.messages),
                    "stream": runtime.exchange.stream,
                    "tools": list(runtime.exchange.allowed_tools),
                    "parameters": dict(runtime.exchange.context.get("request_parameters") or {}),
                }
            )
            if runtime.exchange.operation == "title":
                return PreparedResponse(AssistantMessage(content="“模型生成的对话标题很长”"), {"total_tokens": 2})
            return PreparedResponse(AssistantMessage(content="主回答完成。"), {"total_tokens": 5})

    title_client = DedicatedTitleClient()

    def local_application(_state, *, session_id: str, workspace=None, **_kwargs):
        application = build_application(
            workspace or state.session_workspace(session_id),
            planner_name="rule",
            paths=state.paths,
        )
        application.runner.planner = LLMPlanner(
            title_client,
            [],
            [],
            user_preferences="这项偏好不能进入标题系统提示词",
        )
        return application

    monkeypatch.setattr(chat_routes, "build_local_application", local_application)

    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        accepted = client.post(
            "/api/turns",
            json={
                "id": "turn_model_title",
                "session_id": sidebar["session_id"],
                "thread_id": sidebar["thread_id"],
                "parent_id": "",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "请分析这个复杂错误"}],
                },
                "permission_mode": "read_only",
                "running_mode": "agent",
            },
        )

        assert accepted.status_code == 202
        response = client.get(
            "/api/turns/turn_model_title/stream",
            params={"session_id": sidebar["session_id"]},
        )
        assert response.status_code == 200
        payloads = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
        assert payloads[-1] == '<SSE id="turn_model_title" type="success"></SSE>'
        refreshed = next(
            item for item in client.get("/api/sidebar-threads").json() if item["thread_id"] == sidebar["thread_id"]
        )
        assert refreshed["title"] == "模型生成的对话标题很"
        assert refreshed["title_is_custom"] is False

    assert [request["operation"] for request in title_client.requests] == ["decision", "title"]
    title_request = title_client.requests[-1]
    assert title_request["messages"] == [
        SystemMessage(content=load_title_prompt()),
        UserMessage(content="请分析这个复杂错误"),
    ]
    assert title_request["stream"] is False
    assert title_request["tools"] == []
    assert title_request["parameters"] == {
        "thinking": {"type": "disabled"},
        "max_tokens": 32,
        "temperature": 0.0,
    }
