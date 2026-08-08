from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from backend.configuration import ClientPaths
from backend.domain.runtime_state import (
    APP_VERSION,
    InMemoryNodeStore,
    NodeFrame,
    NodeWriter,
    RuntimeState,
    RuntimeStateTree,
    RuntimeStateValidationError,
    message_payload,
    recoverable,
)
from backend.providers.canonical import to_chat_completions, to_messages, to_responses
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge
from backend.storage.sqlite import SQLiteSessionStore


def test_node_json_shape_and_stable_timestamp() -> None:
    node = RuntimeState.create(session_id="session_a", data=message_payload("user", "hello"))
    encoded = node.to_dict()
    assert set(encoded) == {
        "session_id",
        "parent_session_id",
        "id",
        "parent_id",
        "version",
        "firstKeptEntryId",
        "compactionIdx",
        "user",
        "provider",
        "cwd",
        "timestamp",
        "status",
        "data",
    }
    assert encoded["version"] == APP_VERSION
    assert encoded["firstKeptEntryId"] == node.id
    assert RuntimeState.from_dict(json.loads(node.to_json())).timestamp == node.timestamp


def test_parent_reference_and_fork() -> None:
    tree = RuntimeStateTree()
    root = tree.create_child(session_id="source", data=message_payload("user", "root"))
    child = tree.create_child(session_id="source", parent=root, data=message_payload("assistant", "answer"))
    fork = tree.fork(child, session_id="fork")
    assert (child.parent_session_id, child.parent_id) == ("source", root.id)
    assert (fork.parent_session_id, fork.parent_id) == ("source", child.id)
    assert tree.ancestors("fork", fork.id) == [root, child, fork]


def test_sealed_nodes_are_replaced_only_at_delete() -> None:
    frames: list[NodeFrame] = []
    store = InMemoryNodeStore()
    writer = NodeWriter(store, emit=frames.append)
    node = writer.create(session_id="s", data={})
    writer.update(node.session_id, node.id, data=message_payload("assistant", "stream"))
    assert store.get_node("s", node.id).data == {}
    final = writer.delete("s", node.id)
    assert final.status == "success"
    assert store.get_node("s", node.id).data["type"] == "message"
    with pytest.raises(RuntimeStateValidationError):
        store.finalize_node(final)
    assert [frame.type for frame in frames] == ["node.create", "node.update", "node.delete"]


def test_create_persists_empty_placeholder_until_delete() -> None:
    store = InMemoryNodeStore()
    writer = NodeWriter(store)
    node = writer.create(session_id="s", data=message_payload("assistant", "dynamic"))
    assert store.get_node("s", node.id).data == {}
    assert writer.current("s", node.id).data["type"] == "message"
    sealed = writer.delete("s", node.id)
    assert sealed.data["message"]["content"][0]["text"] == "dynamic"


def test_failed_and_abort_lifecycle() -> None:
    store = InMemoryNodeStore()
    writer = NodeWriter(store)
    failed = writer.create(session_id="s")
    writer.fail("s", failed.id)
    assert store.get_node("s", failed.id).status == "failed"
    paused = writer.create(session_id="s", parent=failed)
    writer.abort("s", paused.id)
    assert store.get_node("s", paused.id).status == "abort"
    resumed = RuntimeStateTree([failed, paused]).resume(paused, data=message_payload("user", "continue"))
    assert resumed.parent_id == paused.id


def test_compaction_retains_recent_window_without_deleting_ancestors() -> None:
    tree = RuntimeStateTree()
    current = tree.create_child(session_id="s", data=message_payload("user", "0"))
    for index in range(1, 10):
        current = tree.create_child(session_id="s", parent=current, data=message_payload("assistant", str(index)))
    compacted = tree.compact(current, "summary")
    context = tree.model_input(compacted)
    assert context[0].data["type"] == "compaction"
    assert compacted.firstKeptEntryId == tree.ancestors("s", current.id)[-8].id
    assert len(tree.ancestors("s", current.id)) == 10


def test_provider_adapters_share_canonical_message() -> None:
    assistant = RuntimeState.create(
        session_id="s",
        data={
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "reasoning", "text": "thinking"},
                    {"type": "text", "text": "answer"},
                    {"type": "tool_call", "call_id": "call_1", "name": "read", "arguments": {"path": "a"}},
                ],
            },
        },
    )
    chat = to_chat_completions([assistant])
    responses = to_responses([assistant])
    messages = to_messages([assistant])
    assert chat[0]["tool_calls"][0]["function"]["name"] == "read"
    assert any(item["type"] == "function_call" for item in responses)
    assert any(block["type"] == "tool_use" for item in messages for block in item["content"])


def test_invalid_status_and_children_key_are_rejected() -> None:
    with pytest.raises(RuntimeStateValidationError):
        RuntimeState.create(session_id="s", data={}, id="x", parent_id="p")
    node = RuntimeState.create(session_id="s")
    with pytest.raises(RuntimeStateValidationError):
        RuntimeState.from_dict({**node.to_dict(), "children_id": []})
    with pytest.raises(RuntimeStateValidationError):
        RuntimeState.from_dict({"session_id": "s", "id": "n", "data": {}})


def test_node_timestamp_must_be_utc() -> None:
    with pytest.raises(RuntimeStateValidationError):
        RuntimeState.create(
            session_id="s",
            timestamp=datetime(2025, 1, 1, tzinfo=timezone(timedelta(hours=8))).isoformat(),
        )


def test_recovery_does_not_replay_side_effecting_content() -> None:
    safe = RuntimeState.create(
        session_id="s",
        data=message_payload(
            "tool_result",
            {"type": "tool_result", "call_id": "c1", "content": "read", "replay_safe": True},
        ),
    )
    unsafe = RuntimeState.create(
        session_id="s",
        data=message_payload(
            "tool_result",
            {"type": "tool_result", "call_id": "c2", "content": "write", "side_effect": True},
        ),
    )
    assert recoverable(safe)
    assert not recoverable(unsafe)


def test_sqlite_node_atomic_finalization_and_snapshot(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini-agent"), "device")
    session = store.create_session()
    writer = NodeWriter(store)
    node = writer.create(session_id=session.session_id, data=message_payload("user", "hello"))
    assert store.get_node(session.session_id, node.id).status == "failed"
    writer.update(node.session_id, node.id, data=message_payload("user", "hello"))
    writer.delete(node.session_id, node.id)
    snapshot = store.export_runtime_node_snapshot(session.session_id)
    assert snapshot["schema_version"] == 3
    assert len(snapshot["nodes"]) == 1
    assert "runtime" not in snapshot
    queued = store.pending_sync_operations()
    assert queued and queued[-1]["snapshot"]["nodes"]


def test_legacy_execution_bridge_emits_only_node_lifecycle_frames(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "bridge"), "device")
    session = store.create_session()
    frames: list[NodeFrame] = []
    bridge = RuntimeEventNodeBridge(store, session_id=session.session_id, prompt="hello", emit=frames.append)
    bridge.start()
    bridge.handle(RuntimeEvent("response_start"))
    bridge.handle(RuntimeEvent("response_delta", "A"))
    bridge.handle(RuntimeEvent("response_delta", "B"))
    bridge.finish("success", "AB")
    assert [frame.type for frame in frames][-1] == "node.delete"
    assert all(frame.type in {"node.create", "node.update", "node.delete"} for frame in frames)
    assert store.load_nodes(session.session_id)[-1].data["message"]["content"][0]["text"] == "AB"


def test_bridge_switches_to_handoff_session_without_mixing_nodes() -> None:
    store = InMemoryNodeStore()
    frames: list[NodeFrame] = []
    bridge = RuntimeEventNodeBridge(store, session_id="source", prompt="plan", emit=frames.append)
    bridge.start()
    bridge.handle(RuntimeEvent("response_start", data={"session_id": "source", "run_id": "run-source"}))
    bridge.handle(RuntimeEvent("response_delta", "proposal", {"session_id": "source", "run_id": "run-source"}))
    bridge.handle(
        RuntimeEvent(
            "response_start",
            data={"session_id": "handoff", "run_id": "run-handoff", "task": "implement"},
        )
    )
    bridge.handle(RuntimeEvent("response_delta", "done", {"session_id": "handoff", "run_id": "run-handoff"}))
    bridge.finish("success", "done")

    source_answers = [node for node in store.all_nodes("source") if node.role == "assistant"]
    handoff_answers = [node for node in store.all_nodes("handoff") if node.role == "assistant"]
    assert source_answers[-1].content[0]["text"] == "proposal"
    assert handoff_answers[-1].content[0]["text"] == "done"
    assert all(frame.node.session_id in {"source", "handoff"} for frame in frames)


def test_bridge_projects_control_events_into_canonical_content_blocks() -> None:
    store = InMemoryNodeStore()
    frames: list[NodeFrame] = []
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=frames.append)
    bridge.start()
    bridge.handle(RuntimeEvent("plan", "Plan created", {"revision": 1}))
    bridge.handle(RuntimeEvent("skills_selected", "skill-a", {"skills": ["skill-a"]}))
    bridge.handle(RuntimeEvent("subagent_started", "Child started", {"task_id": "t1"}))
    bridge.handle(RuntimeEvent("user_input_requested", "Choose", {"decision_id": "d1"}))
    bridge.finish("success", "done")

    assistant = [node for node in store.all_nodes("s") if node.role == "assistant"][-1]
    block_types = [block["type"] for block in assistant.content]
    assert block_types == ["plan", "skill_snapshot", "subagent", "question", "text"]
    assert all(frame.type in {"node.create", "node.update", "node.delete"} for frame in frames)


def test_bridge_compaction_points_to_summary_and_retains_recent_ancestors() -> None:
    store = InMemoryNodeStore()
    frames: list[NodeFrame] = []
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=frames.append)
    bridge.start()
    for index in range(10):
        bridge.handle(RuntimeEvent("response_delta", str(index)))
        bridge.handle(RuntimeEvent("response_start"))
        bridge.handle(RuntimeEvent("response_delta", ""))
        bridge.finish("success", str(index))
        if index < 9:
            bridge = RuntimeEventNodeBridge(store, session_id="s", prompt=f"next-{index}", emit=frames.append)
            bridge.start()
    # A fresh bridge on the current leaf can receive a compaction callback.
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="", emit=frames.append)
    bridge.start()
    bridge.handle(RuntimeEvent("context_compaction_completed", "summary", {"summary": "summary"}))
    compacted = store.get_node("s", bridge.last_node.id)
    assert compacted is not None
    assert compacted.data["type"] == "compaction"
    assert compacted.compactionIdx == compacted.id
    assert compacted.firstKeptEntryId


def test_sqlite_fork_loads_cross_session_ancestors(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "fork"), "device")
    source = store.create_session()
    writer = NodeWriter(store)
    source_node = writer.create(session_id=source.session_id, data=message_payload("user", "root"))
    source_node = writer.delete(source_node.session_id, source_node.id)
    fork = store.create_session()
    fork_node = writer.create(
        session_id=fork.session_id,
        parent=(source_node.session_id, source_node.id),
        data=message_payload("user", "fork"),
    )
    writer.delete(fork_node.session_id, fork_node.id)

    loaded = store.load_nodes(fork.session_id)
    assert [(node.session_id, node.id) for node in loaded] == [
        (source_node.session_id, source_node.id),
        (fork_node.session_id, fork_node.id),
    ]
