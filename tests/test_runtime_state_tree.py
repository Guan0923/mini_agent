from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.sessions.projection import project_node_transcript
from backend.configuration import ClientPaths
from backend.domain import AssistantMessage
from backend.domain.runtime_state import (
    APP_VERSION,
    FAILED_TERMINAL_MESSAGE,
    ROOT_MODEL,
    InMemoryNodeStore,
    NodeFrame,
    NodeWriter,
    RuntimeState,
    RuntimeStateTree,
    RuntimeStateValidationError,
    compaction_payload,
    create_root_node,
    message_payload,
    recoverable,
    session_root_id,
)
from backend.providers.canonical import to_chat_completions, to_messages, to_responses
from backend.providers.token_usage import normalize_provider_usage
from backend.runtime.core.context import _chat_messages_from_nodes
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
        "provider_name",
        "model",
        "permission_mode",
        "running_mode",
        "usage",
        "cwd",
        "timestamp",
        "status",
        "data",
    }
    assert encoded["version"] == APP_VERSION
    assert encoded["firstKeptEntryId"] == node.id
    assert RuntimeState.from_dict(json.loads(node.to_json())).timestamp == node.timestamp


def test_runtime_model_and_usage_validation_and_removed_data_types() -> None:
    node = RuntimeState.create(
        session_id="session_a",
        model={
            "reasoning_effort": "high",
            "current_model": "gpt-x",
            "context_length": 128000,
            "output_length": 16000,
            "thinking": "enable",
            "temperature": 1.0,
        },
        usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )
    assert RuntimeState.from_dict(node.to_dict()).to_dict() == node.to_dict()
    with pytest.raises(RuntimeStateValidationError):
        RuntimeState.create(session_id="s", model={"context_length": 10, "output_length": 10})
    with pytest.raises(RuntimeStateValidationError):
        RuntimeState.create(session_id="s", usage={"total_tokens": -1})
    with pytest.raises(RuntimeStateValidationError):
        RuntimeState.create(session_id="s", data={"type": "model_change", "model": "old"})


def test_session_root_is_deterministic_and_uses_neutral_runtime_defaults() -> None:
    first = create_root_node("session_root")
    second = create_root_node("session_root")

    assert first.id == second.id == session_root_id("session_root")
    assert first.status == "success"
    assert first.data == {"type": "root"}
    assert first.model == {
        **ROOT_MODEL,
    }
    assert first.firstKeptEntryId == first.id
    assert first.compactionIdx == first.id
    assert RuntimeState.from_dict(first.to_dict()).to_dict() == first.to_dict()


def test_children_and_rewind_branches_inherit_both_ancestor_indexes() -> None:
    root = create_root_node("index-session")
    tree = RuntimeStateTree([root])

    first = tree.create_child(
        session_id=root.session_id,
        parent=root,
        data=message_payload("user", "first"),
    )
    original_assistant = tree.create_child(
        session_id=root.session_id,
        parent=first,
        data=message_payload("assistant", "old"),
    )
    # A rewind branch is allowed to use a parent that already has children.
    branch_user = tree.create_child(
        session_id=root.session_id,
        parent=first,
        data=message_payload("user", "edited"),
    )

    for node in (first, original_assistant, branch_user):
        assert node.firstKeptEntryId == root.id
        assert node.compactionIdx == root.id
    assert root.firstKeptEntryId == root.id
    assert root.compactionIdx == root.id

    summary = tree.compact(branch_user, "summary", retention=1)
    descendant = tree.create_child(
        session_id=root.session_id,
        parent=summary,
        data=message_payload("user", "after compaction"),
    )
    assert summary.firstKeptEntryId == branch_user.id
    assert summary.compactionIdx == summary.id
    assert descendant.firstKeptEntryId == summary.firstKeptEntryId
    assert descendant.compactionIdx == summary.compactionIdx


def test_model_input_ignores_invalid_compaction_index_outside_path() -> None:
    root = create_root_node("path-session")
    tree = RuntimeStateTree([root])
    user = tree.create_child(
        session_id=root.session_id,
        parent=root,
        data=message_payload("user", "hello"),
    )
    summary = tree.create_child(
        session_id=root.session_id,
        parent=user,
        data=compaction_payload("summary"),
        first_kept_entry_id="other-session:kept",
        compaction_idx="other-session:summary",
    )

    context = tree.model_input(summary)
    assert [node.id for node in context] == [summary.id, user.id]


def test_root_payload_rejects_extra_fields() -> None:
    with pytest.raises(RuntimeStateValidationError):
        RuntimeState.create(session_id="s", data={"type": "root", "message": "unexpected"}, status="success")


def test_root_is_immutable_and_empty_sessions_expose_it_as_the_leaf(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "store"), "device")
    session = store.create_session()
    root = store.get_session_root(session.session_id)

    assert root is not None
    assert store.get_session_summary(session.session_id).last_node_id == root.id  # type: ignore[union-attr]
    assert store.get_session_summary(session.session_id).message_count == 0  # type: ignore[union-attr]
    with pytest.raises(RuntimeStateValidationError, match="immutable"):
        root.with_status("abort")
    with pytest.raises(RuntimeStateValidationError, match="immutable"):
        root.with_data({"type": "root", "changed": True})
    with pytest.raises(RuntimeStateValidationError, match="session store"):
        NodeWriter(store).create(session_id=session.session_id, parent=root, data={"type": "root"})


def test_v4_database_migration_adds_root_without_losing_message_tree(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "store"), "device")
    session = store.create_session("legacy")
    writer = NodeWriter(store)
    root = store.get_session_root(session.session_id)
    assert root is not None
    user = writer.create(session_id=session.session_id, parent=root, data=message_payload("user", "hello"))
    user = writer.delete(user.session_id, user.id)

    with sqlite3.connect(store.paths.session_db(session.session_id)) as connection:
        connection.execute("UPDATE session_meta SET schema_version=4")
        connection.execute(
            "UPDATE runtime_nodes SET parent_session_id='',parent_id='' WHERE session_id=? AND id=?",
            (session.session_id, user.id),
        )
        connection.execute("DELETE FROM runtime_nodes WHERE session_id=? AND id=?", (session.session_id, root.id))

    reopened = SQLiteSessionStore(ClientPaths(tmp_path / "store"), "device")
    migrated_root = reopened.get_session_root(session.session_id)
    migrated_user = reopened.get_node(session.session_id, user.id)
    assert migrated_root is not None
    assert migrated_root.id == session_root_id(session.session_id)
    assert migrated_user is not None
    assert (migrated_user.parent_session_id, migrated_user.parent_id) == migrated_root.key
    assert reopened.get_session_summary(session.session_id).message_count == 1  # type: ignore[union-attr]
    with sqlite3.connect(reopened.paths.session_db(session.session_id)) as connection:
        assert connection.execute("SELECT schema_version FROM session_meta").fetchone() == (7,)


def test_v4_snapshot_import_adds_root_and_writes_v6(tmp_path: Path) -> None:
    source = SQLiteSessionStore(ClientPaths(tmp_path / "source"), "device_a")
    session = source.create_session("snapshot")
    writer = NodeWriter(source)
    root = source.get_session_root(session.session_id)
    assert root is not None
    user = writer.create(session_id=session.session_id, parent=root, data=message_payload("user", "hello"))
    user = writer.delete(user.session_id, user.id)
    snapshot = source.export_runtime_node_snapshot(session.session_id)
    snapshot["schema_version"] = 4
    snapshot["nodes"] = [
        {
            **node,
            "parent_session_id": "",
            "parent_id": "",
        }
        for node in snapshot["nodes"]
        if node["data"].get("type") != "root"
    ]

    replica = SQLiteSessionStore(ClientPaths(tmp_path / "replica"), "device_b")
    replica.apply_runtime_node_snapshot(snapshot, local_device_id="device_b")
    restored_root = replica.get_session_root(session.session_id)
    restored_user = replica.get_node(session.session_id, user.id)
    assert restored_root is not None
    assert restored_user is not None
    assert (restored_user.parent_session_id, restored_user.parent_id) == restored_root.key
    assert replica.export_runtime_node_snapshot(session.session_id)["schema_version"] == 7


def _snapshot_with_title(snapshot: dict[str, object], title: str, custom: object) -> dict[str, object]:
    value = json.loads(json.dumps(snapshot))
    value["session"] = {**value["session"], "title": title}
    if custom is None:
        value["session"].pop("title_is_custom", None)
    else:
        value["session"]["title_is_custom"] = custom
    return value


def test_v5_snapshot_title_inference_and_v6_round_trip(tmp_path: Path) -> None:
    source = SQLiteSessionStore(ClientPaths(tmp_path / "source"), "device_a")
    session = source.create_session("新对话")
    writer = NodeWriter(source)
    root = source.get_session_root(session.session_id)
    assert root is not None
    user = writer.create(session_id=session.session_id, parent=root, data=message_payload("user", "  快照  消息 "))
    writer.delete(user.session_id, user.id)
    snapshot = source.export_runtime_node_snapshot(session.session_id)
    assert snapshot["schema_version"] == 7
    assert snapshot["session"]["title_is_custom"] is False

    # v5 snapshot without the field: placeholder title is backfilled from the
    # first user message and stays automatic.
    replica = SQLiteSessionStore(ClientPaths(tmp_path / "replica"), "device_b")
    replica.apply_runtime_node_snapshot(
        _snapshot_with_title(snapshot, "New session", None),
        local_device_id="device_b",
    )
    restored = replica.get_session(session.session_id)
    assert restored is not None
    assert restored.title == "快照 消息"
    assert restored.title_is_custom is False
    assert replica.export_runtime_node_snapshot(session.session_id)["schema_version"] == 7

    # v5 snapshot with a non-placeholder title: conservatively custom.
    conservative = SQLiteSessionStore(ClientPaths(tmp_path / "conservative"), "device_b")
    conservative.apply_runtime_node_snapshot(
        _snapshot_with_title(snapshot, "云端旧标题", None),
        local_device_id="device_b",
    )
    restored = conservative.get_session(session.session_id)
    assert restored is not None
    assert restored.title == "云端旧标题"
    assert restored.title_is_custom is True

    # v6 snapshot carries the flag and is trusted verbatim.
    trusted = SQLiteSessionStore(ClientPaths(tmp_path / "trusted"), "device_b")
    trusted.apply_runtime_node_snapshot(
        _snapshot_with_title(snapshot, "云端新标题", True),
        local_device_id="device_b",
    )
    restored = trusted.get_session(session.session_id)
    assert restored is not None
    assert restored.title == "云端新标题"
    assert restored.title_is_custom is True


def test_remote_snapshot_carries_title_is_custom(tmp_path: Path) -> None:
    source = SQLiteSessionStore(ClientPaths(tmp_path / "source"), "device_a")
    session = source.create_session("新对话")
    source.start_turn(session.session_id, "run-sync", "同步标题")
    writer = NodeWriter(source)
    root = source.get_session_root(session.session_id)
    assert root is not None
    user = writer.create(session_id=session.session_id, parent=root, data=message_payload("user", "同步标题"))
    writer.delete(user.session_id, user.id)
    snapshot = source.export_runtime_node_snapshot(session.session_id)
    assert snapshot["session"]["title"] == "同步标题"
    assert snapshot["session"]["title_is_custom"] is False

    replica = SQLiteSessionStore(ClientPaths(tmp_path / "replica"), "device_b")
    replica.apply_remote_snapshot(
        {
            "session_id": session.session_id,
            "owner_device_id": "device_a",
            "revision": 1,
            "snapshot": snapshot,
        },
        local_device_id="device_b",
    )
    restored = replica.get_session(session.session_id)
    assert restored is not None
    assert restored.title == "同步标题"
    assert restored.title_is_custom is False


def test_provider_usage_aliases_and_partial_fields_are_normalized() -> None:
    assert normalize_provider_usage(
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2},
        }
    ) == {
        "input_tokens": 10,
        "cached_tokens": 3,
        "output_tokens": 4,
        "reasoning_tokens": 2,
        "total_tokens": 14,
    }
    assert normalize_provider_usage({"cache_creation_input_tokens": 7})["cached_tokens"] == 7
    assert normalize_provider_usage({"prompt_tokens": 3}) == {
        "input_tokens": 3,
        "cached_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }


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


def test_runtime_config_updates_dynamic_node_without_touching_placeholder() -> None:
    store = InMemoryNodeStore()
    frames: list[NodeFrame] = []
    writer = NodeWriter(store, emit=frames.append)
    node = writer.create(
        session_id="s",
        data=message_payload("assistant", "draft"),
        model={
            "reasoning_effort": "medium",
            "current_model": "old-model",
            "context_length": 128000,
            "output_length": 8192,
            "thinking": "enable",
            "temperature": 1.0,
        },
    )
    updated = writer.update_config(
        node,
        provider_name="work-openai",
        model={"current_model": "new-model", "reasoning_effort": "high"},
        permission_mode="full_access",
        running_mode="plan",
    )
    placeholder = store.get_node("s", node.id)
    assert placeholder is not None and placeholder.data == {}
    assert placeholder.provider_name == ""
    assert updated.provider_name == "work-openai"
    assert updated.model["current_model"] == "new-model"
    assert updated.model["reasoning_effort"] == "high"
    assert updated.permission_mode == "full_access"
    assert updated.running_mode == "plan"
    assert frames[-1].type == "node.update"


def test_runtime_config_updates_merge_pending_partial_fields() -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=lambda _frame: None)
    bridge.start()
    runtime = SimpleNamespace(
        state=SimpleNamespace(),
        services=SimpleNamespace(pending_runtime_config=None),
    )
    bridge.bind_runtime(runtime)

    bridge.apply_runtime_config({"model": {"reasoning_effort": "high"}})
    bridge.apply_runtime_config({"permission_mode": "full_access"})

    assert runtime.services.pending_runtime_config == {
        "model": {"reasoning_effort": "high"},
        "permission_mode": "full_access",
    }


def test_runtime_config_rejects_invalid_provider_policy_without_mutation() -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=lambda _frame: None)
    bridge.start()
    current = bridge.writer.current("s", bridge.assistant.id)

    with pytest.raises(RuntimeStateValidationError):
        bridge.apply_runtime_config({"permission_mode": "root"})
    with pytest.raises(RuntimeStateValidationError):
        bridge.apply_runtime_config({"running_mode": "interactive"})
    with pytest.raises(RuntimeStateValidationError):
        bridge.apply_runtime_config({"provider_name": "   "})

    assert bridge.writer.current("s", bridge.assistant.id).to_dict() == current.to_dict()


def test_model_context_replaces_same_id_failed_placeholder_with_dynamic_leaf() -> None:
    store = InMemoryNodeStore()
    writer = NodeWriter(store)
    parent = writer.create(session_id="s", data=message_payload("user", "hello"))
    parent = writer.delete("s", parent.id)
    dynamic = writer.create(session_id="s", parent=parent, data=message_payload("assistant", "streamed"))
    tree = RuntimeStateTree(store.load_nodes("s"))

    context = tree.model_input(dynamic)

    assert [(item.id, item.data) for item in context] == [
        (parent.id, parent.data),
        (dynamic.id, dynamic.data),
    ]
    assert context[-1].data["message"]["content"][0]["text"] == "streamed"


def test_root_is_not_returned_as_model_context_or_transcript_message() -> None:
    root = create_root_node("s")
    tree = RuntimeStateTree([root])
    user = tree.create_child(session_id="s", parent=root, data=message_payload("user", "hello"))

    assert [node.id for node in tree.model_input(user)] == [user.id]
    assert project_node_transcript([root, user])[0]["role"] == "user"
    assert project_node_transcript([root, user])[0]["source_node_id"] == root.id


def test_transcript_projects_timeline_metadata_for_user_and_steering_messages() -> None:
    root = create_root_node("s", timestamp="2026-01-01T00:00:00+00:00")
    user = RuntimeState.create(
        session_id="s",
        parent=root,
        id="user-1",
        timestamp="2026-01-01T00:00:01+00:00",
        data=message_payload(
            "user", [{"type": "text", "text": " first "}, {"type": "reasoning", "text": "hidden"}], source="user"
        ),
        status="success",
    )
    steering = RuntimeState.create(
        session_id="s",
        parent=user,
        id="steering-1",
        timestamp="2026-01-01T00:00:02+00:00",
        data=message_payload("user", "steer", source="steering"),
        status="success",
    )
    assistant = RuntimeState.create(
        session_id="s",
        parent=steering,
        id="assistant-1",
        timestamp="2026-01-01T00:00:03+00:00",
        data=message_payload("assistant", "answer"),
        status="success",
    )

    transcript = project_node_transcript([root, user, steering, assistant])

    assert [item["role"] for item in transcript] == ["user", "user", "assistant"]
    assert transcript[0]["timeline_seq"] == 1
    assert transcript[0]["timeline_time"] == 1_767_225_601_000
    assert transcript[0]["timeline_text"] == "first …"
    assert transcript[0]["timeline_source"] == "user"
    assert transcript[1]["timeline_seq"] == 2
    assert transcript[1]["timeline_source"] == "steering"
    assert "timeline_seq" not in transcript[2]


def test_transcript_projects_structured_references_on_user_messages() -> None:
    root = create_root_node("s")
    tree = RuntimeStateTree([root])
    user = tree.create_child(
        session_id="s",
        parent=root,
        data=message_payload(
            "user",
            "请查看上传的配置",
            references=[{"source": "upload", "path": "config.yaml"}, {"source": "project", "path": "README.md"}],
        ),
    )
    transcript = project_node_transcript([root, user])
    assert transcript[0]["references"] == [
        {"source": "upload", "path": "config.yaml"},
        {"source": "project", "path": "README.md"},
    ]
    # Malformed metadata never crashes the projection.
    bogus = tree.create_child(
        session_id="s",
        parent=user,
        data=message_payload("user", "bogus", references="not-a-list"),
    )
    assert project_node_transcript([root, user, bogus])[-1].get("references") is None


def test_compaction_accepts_a_dynamic_leaf_before_durable_finalization() -> None:
    store = InMemoryNodeStore()
    writer = NodeWriter(store)
    parent = writer.create(session_id="s", data=message_payload("user", "hello"))
    parent = writer.delete("s", parent.id)
    dynamic = writer.create(session_id="s", parent=parent, data=message_payload("assistant", "streamed"))
    tree = RuntimeStateTree(store.load_nodes("s"))

    summary = tree.compact(dynamic, "summary")

    assert summary.data["type"] == "compaction"
    assert summary.parent_id == dynamic.id
    context = tree.model_input(summary)
    assert any(item.id == summary.id and item.data["summary"] == "summary" for item in context)
    assert all(item.data for item in context)


def test_provider_adapters_deduplicate_dynamic_leaf_and_drop_placeholder() -> None:
    placeholder = RuntimeState.create(session_id="s", id="node", data={})
    dynamic = RuntimeState.create(
        session_id="s",
        id="node",
        data=message_payload("assistant", "streamed"),
    )
    assert to_chat_completions([placeholder, dynamic]) == [{"role": "assistant", "content": "streamed"}]
    assert to_chat_completions([placeholder]) == []


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


def test_failed_node_records_generic_reason_for_ui_and_next_model_turn() -> None:
    store = InMemoryNodeStore()
    writer = NodeWriter(store)
    node = writer.create(session_id="s")

    failed = writer.fail("s", node.id)

    error = failed.data["message"]["error"]
    assert failed.status == "failed"
    assert error == {"category": "unknown", "message": FAILED_TERMINAL_MESSAGE}
    assert project_node_transcript([failed])[0]["error"] == FAILED_TERMINAL_MESSAGE
    assert to_chat_completions([failed]) == [{"role": "assistant", "content": FAILED_TERMINAL_MESSAGE}]


def test_provider_adapters_preserve_terminal_reason_for_mapping_nodes() -> None:
    terminal = {
        "status": "abort",
        "data": {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [],
                "error": {
                    "category": "user",
                    "message": "The run was aborted at the user's request.",
                },
            },
        },
    }
    expected = "The run was aborted at the user's request."

    assert to_chat_completions([{"status": "abort", "data": {}}]) == [
        {"role": "assistant", "content": "The run was aborted for an unknown reason."}
    ]
    for adapter in (to_chat_completions, to_responses, to_messages):
        rendered = json.dumps(adapter([terminal]), ensure_ascii=False)
        assert expected in rendered
        assert rendered.count(expected) == 1


def test_provider_adapters_do_not_duplicate_an_existing_terminal_reason() -> None:
    reason = "The run was aborted at the user's request."
    terminal = {
        "status": "abort",
        "data": {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": reason}],
                "error": {"category": "user", "message": reason},
            },
        },
    }

    for adapter in (to_chat_completions, to_responses, to_messages):
        rendered = json.dumps(adapter([terminal]), ensure_ascii=False)
        assert rendered.count(reason) == 1


@pytest.mark.parametrize(
    ("events", "category", "expected"),
    [
        (
            [
                RuntimeEvent("tool_failed", "disk unavailable", {"tool": "write", "call_id": "c1"}),
                RuntimeEvent("error", "Stopped"),
            ],
            "tool",
            "internal tool error",
        ),
        (
            [
                RuntimeEvent("model_error", "request failed", {"error_type": "ModelTransportError"}),
                RuntimeEvent("error", "Decision failed"),
            ],
            "network",
            "network error",
        ),
        ([RuntimeEvent("error", "planner failed")], "agent", "agent encountered an internal error"),
        (
            [RuntimeEvent("cancelled", "Run cancelled by user", {"stop_reason": "user_cancelled"})],
            "user",
            "user's request",
        ),
    ],
)
def test_bridge_classifies_abort_reason_for_projection_and_model_context(
    events: list[RuntimeEvent], category: str, expected: str
) -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=lambda _frame: None)
    bridge.start()

    for event in events:
        bridge.handle(event)

    terminal = bridge.last_node
    assert terminal is not None and terminal.status == "abort"
    error = terminal.data["message"]["error"]
    assert error["category"] == category
    assert expected in error["message"]
    transcript = project_node_transcript(store.all_nodes("s"))
    assert expected in transcript[-1]["error"]
    assert expected in str(to_chat_completions([terminal])[-1]["content"])


@pytest.mark.parametrize(
    ("error_type", "category"),
    [("ModelTransportError", "network"), ("TaskPreparationError", "tool"), ("PlanningError", "agent")],
)
def test_bridge_keeps_known_uncaught_exception_categories(error_type: str, category: str) -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=lambda _frame: None)
    bridge.start()

    error = type(error_type, (RuntimeError,), {})()
    terminal = bridge.finish_exception(error)

    assert terminal is not None and terminal.status == "abort"
    assert terminal.data["message"]["error"]["category"] == category


def test_unknown_uncaught_exception_keeps_empty_failed_placeholder() -> None:
    store = InMemoryNodeStore()
    frames: list[NodeFrame] = []
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=frames.append)
    bridge.start()
    dynamic = bridge.assistant
    assert dynamic is not None

    terminal = bridge.finish_exception(RuntimeError("unexpected"))

    persisted = store.get_node("s", dynamic.id)
    assert terminal is not None
    assert persisted is not None and persisted.status == "failed" and persisted.data == {}
    assert not any(frame.type == "node.delete" and frame.node.id == dynamic.id for frame in frames)


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


def test_chat_completions_omits_control_only_assistant_messages() -> None:
    control = RuntimeState.create(
        session_id="s",
        data=message_payload(
            "assistant",
            [{"type": "question", "event": "decision_requested", "text": "Choose one"}],
        ),
    )
    reasoning = RuntimeState.create(
        session_id="s",
        data=message_payload("assistant", [{"type": "reasoning", "text": "Internal reasoning"}]),
    )
    answer = RuntimeState.create(session_id="s", data=message_payload("assistant", "Visible answer"))
    multi_part = RuntimeState.create(
        session_id="s",
        data=message_payload(
            "assistant",
            [{"type": "text", "text": "Part one"}, {"type": "text", "text": "Part two"}],
        ),
    )

    assert to_chat_completions([control, reasoning, answer, multi_part]) == [
        {"role": "assistant", "content": "Visible answer"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Part one"},
                {"type": "text", "text": "Part two"},
            ],
        },
    ]


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
    assert snapshot["schema_version"] == 7
    assert len(snapshot["nodes"]) == 2
    assert sum(node["data"].get("type") == "message" for node in snapshot["nodes"]) == 1
    assert "runtime" not in snapshot
    queued = store.pending_sync_operations()
    assert queued and queued[-1]["snapshot"]["nodes"]


def test_old_runtime_snapshot_is_rejected(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini-agent"), "device")
    with pytest.raises(ValueError, match="schema_version=4, 5, 6, or 7"):
        store.apply_runtime_node_snapshot({"schema_version": 3}, local_device_id="device")


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


def test_bridge_keeps_multi_tool_batch_on_one_assistant_node() -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=lambda _frame: None)
    bridge.start()
    bridge.handle(
        RuntimeEvent(
            "assistant_message",
            data={
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_messages": [
                        {"name": "glob", "call_id": "call_a", "arguments": {}},
                        {"name": "run_command", "call_id": "call_b", "arguments": {}},
                    ],
                }
            },
        )
    )
    bridge.handle(RuntimeEvent("tool_call", "glob", {"tool": "glob", "call_id": "call_a"}))
    bridge.handle(RuntimeEvent("tool_result", "glob result", {"tool": "glob", "call_id": "call_a"}))
    bridge.handle(RuntimeEvent("tool_call", "run_command", {"tool": "run_command", "call_id": "call_b"}))
    bridge.handle(RuntimeEvent("tool_result", "command result", {"tool": "run_command", "call_id": "call_b"}))

    context = bridge.model_context()
    messages = _chat_messages_from_nodes(context)
    assistants = [message for message in messages if isinstance(message, AssistantMessage) and message.tool_messages]
    assert len(assistants) == 1
    assert [(tool.call_id, tool.status, tool.content) for tool in assistants[0].tool_messages] == [
        ("call_a", "succeeded", "glob result"),
        ("call_b", "succeeded", "command result"),
    ]


def test_hidden_tool_failure_closes_canonical_tool_call() -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=lambda _frame: None)
    bridge.start()
    bridge.handle(
        RuntimeEvent(
            "assistant_message",
            data={
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_messages": [{"name": "run_command", "call_id": "call_a", "arguments": {}}],
                }
            },
        )
    )
    bridge.handle(RuntimeEvent("tool_failed", "command failed", {"tool": "run_command", "call_id": "call_a"}))

    messages = _chat_messages_from_nodes(bridge.model_context())
    assistants = [message for message in messages if isinstance(message, AssistantMessage) and message.tool_messages]
    assert len(assistants) == 1
    assert assistants[0].tool_messages[0].status == "failed"
    assert assistants[0].tool_messages[0].content == "command failed"


def test_user_denial_persists_as_recoverable_canonical_tool_result() -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(store, session_id="s", prompt="hello", emit=lambda _frame: None)
    bridge.start()
    bridge.handle(
        RuntimeEvent(
            "assistant_message",
            data={
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_messages": [{"name": "write_file", "call_id": "call_denied", "arguments": {"path": "a.txt"}}],
                }
            },
        )
    )
    bridge.handle(
        RuntimeEvent(
            "tool_failed",
            "The user denied this write_file tool call.",
            {
                "tool": "write_file",
                "call_id": "call_denied",
                "error": "The user denied this write_file tool call.",
                "failure_code": "user_denied",
            },
        )
    )

    messages = _chat_messages_from_nodes(bridge.model_context())
    assistant = next(message for message in messages if isinstance(message, AssistantMessage) and message.tool_messages)
    denied = assistant.tool_messages[0]
    assert denied.status == "failed"
    assert denied.retryable is False
    assert denied.failure_code == "user_denied"
    assert denied.content == "The user denied this write_file tool call."
    result_blocks = [
        block
        for node in store.load_nodes("s")
        for block in node.data.get("message", {}).get("content", [])
        if block.get("type") == "tool_result"
    ]
    assert result_blocks[-1]["retryable"] is False
    assert result_blocks[-1]["failure_code"] == "user_denied"
    assert bridge.abort_category is None


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


def test_bridge_starts_a_new_agent_turn_after_same_session_plan_handoff() -> None:
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(
        store,
        session_id="s",
        prompt="plan",
        running_mode="plan",
        emit=lambda _frame: None,
    )
    bridge.start()
    bridge.handle(RuntimeEvent("response_delta", "proposal"))
    plan_assistant_id = bridge.assistant.id

    bridge.begin_turn("s", "implement", running_mode="agent")
    assert bridge.running_mode == "agent"
    assert bridge.assistant is not None and bridge.assistant.id != plan_assistant_id
    bridge.handle(RuntimeEvent("response_delta", "implementation"))
    bridge.finish("success", "implementation")

    assistants = [node for node in store.all_nodes("s") if node.role == "assistant"]
    assert [node.content[0]["text"] for node in assistants] == ["proposal", "implementation"]
    assert assistants[0].running_mode == "plan"
    assert assistants[1].running_mode == "agent"


def test_node_writer_orders_parent_and_child_when_clock_values_tie() -> None:
    store = InMemoryNodeStore()
    writer = NodeWriter(store, clock=lambda: "2026-08-13T00:00:00+00:00")

    parent = writer.create(session_id="s", data=message_payload("user", "first"))
    parent = writer.delete(parent.session_id, parent.id)
    child = writer.create(session_id="s", parent=parent, data=message_payload("assistant", "second"))
    writer.delete(child.session_id, child.id)

    assert [node.role for node in store.all_nodes("s")] == ["user", "assistant"]
    assert datetime.fromisoformat(child.timestamp) > datetime.fromisoformat(parent.timestamp)


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
    fork = store.create_session(root_parent=(source_node.session_id, source_node.id))
    target_root = store.get_session_root(fork.session_id)
    assert target_root is not None
    fork_node = writer.create(
        session_id=fork.session_id,
        parent=target_root,
        data=message_payload("user", "fork"),
    )
    writer.delete(fork_node.session_id, fork_node.id)

    loaded = store.load_nodes(fork.session_id)
    assert [(node.session_id, node.id) for node in loaded] == [
        (source_node.session_id, source_node.id),
        (target_root.session_id, target_root.id),
        (fork_node.session_id, fork_node.id),
    ]
