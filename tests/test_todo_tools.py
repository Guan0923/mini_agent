import json
from pathlib import Path

import pytest

from backend.storage.todo_list import MemoryTodoListStore
from backend.tools import ToolError, ToolInvocationContext, build_tool_registry


def _context(store: MemoryTodoListStore, call_id: str) -> ToolInvocationContext:
    return ToolInvocationContext(
        session_id="session-todo",
        turn_id="turn-todo",
        call_id=call_id,
        todo_store=store,
    )


def _invoke(registry, store: MemoryTodoListStore, call_id: str, revision: int, operations: list[dict]):
    output = registry.invoke_with_context(
        "update_todo_list",
        {"expected_revision": revision, "operations": operations},
        _context(store, call_id),
    )
    return json.loads(output)


def test_update_todo_list_adds_duplicate_content_and_returns_authoritative_snapshot(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)
    store = MemoryTodoListStore()

    result = _invoke(
        registry,
        store,
        "call-add",
        0,
        [
            {"op": "add", "content": "same", "status": "in_progress"},
            {"op": "add", "content": "same", "status": "in_progress"},
        ],
    )

    assert result["turn_id"] == "turn-todo"
    assert result["revision"] == 1
    assert result["counts"] == {"pending": 0, "in_progress": 2, "completed": 0}
    assert [todo["content"] for todo in result["todos"]] == ["same", "same"]
    assert len({todo["id"] for todo in result["todos"]}) == 2
    assert all(todo["id"].startswith("todo_") and len(todo["id"]) == 37 for todo in result["todos"])


def test_update_todo_list_updates_content_and_status_then_removes_by_id(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)
    store = MemoryTodoListStore()
    added = _invoke(
        registry,
        store,
        "call-add",
        0,
        [{"op": "add", "content": "draft", "status": "pending"}],
    )
    todo_id = added["todos"][0]["id"]

    updated = _invoke(
        registry,
        store,
        "call-update",
        1,
        [{"op": "update", "id": todo_id, "content": "final", "status": "completed"}],
    )
    removed = _invoke(
        registry,
        store,
        "call-remove",
        2,
        [{"op": "remove", "id": todo_id}],
    )

    assert updated["todos"] == [{"id": todo_id, "content": "final", "status": "completed"}]
    assert removed["revision"] == 3
    assert removed["todos"] == []


def test_update_todo_list_replays_same_call_id_without_incrementing_revision(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)
    store = MemoryTodoListStore()
    operations = [{"op": "add", "content": "work", "status": "pending"}]

    first = _invoke(registry, store, "call-once", 0, operations)
    replay = _invoke(registry, store, "call-once", 0, operations)

    assert replay == first
    assert store.snapshot("session-todo", "turn-todo").revision == 1


def test_update_todo_list_returns_current_snapshot_on_revision_conflict(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)
    store = MemoryTodoListStore()
    _invoke(
        registry,
        store,
        "call-add",
        0,
        [{"op": "add", "content": "work", "status": "pending"}],
    )

    with pytest.raises(ToolError) as raised:
        _invoke(
            registry,
            store,
            "call-stale",
            0,
            [{"op": "add", "content": "stale", "status": "pending"}],
        )

    error = json.loads(str(raised.value))["error"]
    assert error["code"] == "revision_conflict"
    assert error["current_revision"] == 1
    assert error["current_snapshot"]["todos"][0]["content"] == "work"


def test_update_todo_list_rejects_whole_invalid_batch_without_state_change(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)
    store = MemoryTodoListStore()
    added = _invoke(
        registry,
        store,
        "call-add",
        0,
        [{"op": "add", "content": "keep", "status": "pending"}],
    )
    todo_id = added["todos"][0]["id"]

    with pytest.raises(ToolError, match="duplicate_target"):
        _invoke(
            registry,
            store,
            "call-invalid",
            1,
            [
                {"op": "update", "id": todo_id, "status": "completed"},
                {"op": "remove", "id": todo_id},
            ],
        )

    snapshot = store.snapshot("session-todo", "turn-todo")
    assert snapshot.revision == 1
    assert snapshot.todos[0].status == "pending"


def test_update_todo_list_rejects_noop_and_call_id_conflict(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)
    store = MemoryTodoListStore()
    added = _invoke(
        registry,
        store,
        "call-shared",
        0,
        [{"op": "add", "content": "keep", "status": "pending"}],
    )
    todo_id = added["todos"][0]["id"]

    with pytest.raises(ToolError, match="call_id_conflict"):
        _invoke(
            registry,
            store,
            "call-shared",
            1,
            [{"op": "update", "id": todo_id, "status": "completed"}],
        )
    with pytest.raises(ToolError, match="no_change"):
        _invoke(
            registry,
            store,
            "call-noop",
            1,
            [{"op": "update", "id": todo_id, "content": "keep"}],
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"expected_revision": -1, "operations": [{"op": "add", "content": "x", "status": "pending"}]},
        {"expected_revision": 0, "operations": []},
        {"expected_revision": 0, "operations": [{"op": "add", "content": "x"}]},
        {"expected_revision": 0, "operations": [{"op": "add", "content": "", "status": "pending"}]},
        {"expected_revision": 0, "operations": [{"op": "add", "content": "x", "status": "blocked"}]},
        {"expected_revision": 0, "operations": [{"op": "update", "id": "bad", "status": "pending"}]},
        {"expected_revision": 0, "operations": [{"op": "update", "id": "todo_" + "a" * 32}]},
        {
            "expected_revision": 0,
            "operations": [{"op": "remove", "id": "todo_" + "a" * 32, "extra": True}],
        },
    ],
)
def test_update_todo_list_schema_rejects_invalid_arguments(tmp_path: Path, arguments: dict[str, object]) -> None:
    registry = build_tool_registry(tmp_path)

    with pytest.raises(ToolError, match="Invalid arguments"):
        registry.invoke_with_context("update_todo_list", arguments, ToolInvocationContext())


def test_update_todo_list_requires_runtime_context(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    with pytest.raises(ToolError, match="active Turn"):
        registry.invoke(
            "update_todo_list",
            {
                "expected_revision": 0,
                "operations": [{"op": "add", "content": "work", "status": "pending"}],
            },
        )


def test_update_todo_list_is_registered_without_legacy_alias(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    assert "update_todo_list" in registry.names()
    assert "todo_write" not in registry.names()
    assert registry.is_read_only("update_todo_list") is False
