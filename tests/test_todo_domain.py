from __future__ import annotations

import pytest

from backend.domain import TodoItem, TodoSnapshot, TodoStateError, apply_todo_operations


def _id(value: int) -> str:
    return f"todo_{value:032x}"


def test_atomic_operations_preserve_order_and_allow_duplicate_content_and_multiple_active_items() -> None:
    snapshot = TodoSnapshot(
        4,
        (
            TodoItem(_id(1), "remove", "completed"),
            TodoItem(_id(2), "same", "pending"),
            TodoItem(_id(3), "same", "completed"),
        ),
    )

    updated, applied = apply_todo_operations(
        snapshot,
        [
            {"op": "update", "id": _id(2), "content": "same", "status": "in_progress"},
            {"op": "remove", "id": _id(1)},
            {"op": "update", "id": _id(3), "status": "in_progress"},
            {"op": "add", "content": "same", "status": "in_progress"},
        ],
        generated_ids=[_id(4)],
    )

    assert updated.revision == 5
    assert [(todo.id, todo.content, todo.status) for todo in updated.todos] == [
        (_id(2), "same", "in_progress"),
        (_id(3), "same", "in_progress"),
        (_id(4), "same", "in_progress"),
    ]
    assert [operation["op"] for operation in applied] == ["update", "remove", "update", "add"]


@pytest.mark.parametrize("source", ["pending", "in_progress", "completed"])
@pytest.mark.parametrize("target", ["pending", "in_progress", "completed"])
def test_every_status_transition_is_allowed_when_it_changes(source: str, target: str) -> None:
    snapshot = TodoSnapshot(1, (TodoItem(_id(1), "work", source),))  # type: ignore[arg-type]
    operation = {"op": "update", "id": _id(1), "status": target}

    if source == target:
        with pytest.raises(TodoStateError, match="does not change"):
            apply_todo_operations(snapshot, [operation], generated_ids=[])
        return

    updated, _applied = apply_todo_operations(snapshot, [operation], generated_ids=[])
    assert updated.todos[0].status == target


def test_invalid_late_operation_rolls_back_the_whole_batch() -> None:
    snapshot = TodoSnapshot(2, (TodoItem(_id(1), "keep", "pending"),))

    with pytest.raises(TodoStateError, match="does not exist"):
        apply_todo_operations(
            snapshot,
            [
                {"op": "update", "id": _id(1), "status": "completed"},
                {"op": "remove", "id": _id(2)},
            ],
            generated_ids=[],
        )

    assert snapshot == TodoSnapshot(2, (TodoItem(_id(1), "keep", "pending"),))


def test_capacity_and_operation_count_are_enforced() -> None:
    full = TodoSnapshot(7, tuple(TodoItem(_id(index), str(index), "pending") for index in range(100)))

    with pytest.raises(TodoStateError, match="more than 100"):
        apply_todo_operations(
            full,
            [{"op": "add", "content": "overflow", "status": "pending"}],
            generated_ids=[_id(100)],
        )
    with pytest.raises(TodoStateError, match="1 to 100"):
        apply_todo_operations(TodoSnapshot(), [], generated_ids=[])
    with pytest.raises(TodoStateError, match="1 to 100"):
        apply_todo_operations(
            TodoSnapshot(),
            [{"op": "add", "content": str(index), "status": "pending"} for index in range(101)],
            generated_ids=[_id(index) for index in range(101)],
        )


@pytest.mark.parametrize(
    "operations",
    [
        [{"op": "add", "content": "work", "status": "pending", "extra": True}],
        [{"op": "update", "id": "bad", "status": "completed"}],
        [{"op": "remove", "id": _id(1), "extra": True}],
        [
            {"op": "update", "id": _id(1), "status": "completed"},
            {"op": "remove", "id": _id(1)},
        ],
    ],
)
def test_operation_shapes_ids_and_duplicate_targets_are_rejected(operations: list[dict[str, object]]) -> None:
    snapshot = TodoSnapshot(1, (TodoItem(_id(1), "work", "pending"),))

    with pytest.raises(TodoStateError):
        apply_todo_operations(snapshot, operations, generated_ids=[])
