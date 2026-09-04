"""Dependency-free Todo state machine and storage port."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

TodoStatus = Literal["pending", "in_progress", "completed"]
TODO_STATUSES: tuple[TodoStatus, ...] = ("pending", "in_progress", "completed")
MAX_TODOS = 100
_TODO_ID_PATTERN = re.compile(r"^todo_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class TodoItem:
    id: str
    content: str
    status: TodoStatus

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "content": self.content, "status": self.status}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TodoItem:
        return cls(id=str(data["id"]), content=str(data["content"]), status=data["status"])


@dataclass(frozen=True, slots=True)
class TodoSnapshot:
    revision: int = 0
    todos: tuple[TodoItem, ...] = ()

    @property
    def unfinished(self) -> tuple[TodoItem, ...]:
        return tuple(todo for todo in self.todos if todo.status != "completed")

    def to_dict(self) -> dict[str, object]:
        counts = Counter(todo.status for todo in self.todos)
        return {
            "revision": self.revision,
            "todos": [todo.to_dict() for todo in self.todos],
            "counts": {status: counts[status] for status in TODO_STATUSES},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TodoSnapshot:
        raw_todos = data.get("todos", [])
        return cls(
            revision=int(data.get("revision", 0)),
            todos=tuple(TodoItem.from_dict(todo) for todo in raw_todos if isinstance(todo, Mapping)),
        )


@dataclass(frozen=True, slots=True)
class TodoUpdateResult:
    turn_id: str
    snapshot: TodoSnapshot
    applied_operations: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "revision": self.snapshot.revision,
            "applied_operations": [dict(operation) for operation in self.applied_operations],
            "counts": self.snapshot.to_dict()["counts"],
            "todos": [todo.to_dict() for todo in self.snapshot.todos],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TodoUpdateResult:
        snapshot = TodoSnapshot.from_dict(data)
        operations = data.get("applied_operations", [])
        return cls(
            turn_id=str(data["turn_id"]),
            snapshot=snapshot,
            applied_operations=tuple(
                {str(key): str(value) for key, value in operation.items()}
                for operation in operations
                if isinstance(operation, Mapping)
            ),
        )


class TodoStateError(ValueError):
    """A rejected Todo mutation that leaves authoritative state unchanged."""

    def __init__(self, code: str, message: str, *, snapshot: TodoSnapshot | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.snapshot = snapshot

    def to_dict(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": str(self)}
        if self.snapshot is not None:
            error["current_revision"] = self.snapshot.revision
            error["current_snapshot"] = self.snapshot.to_dict()
        return {"error": error}


class TodoListStore(Protocol):
    def update(
        self,
        *,
        session_id: str,
        turn_id: str,
        call_id: str,
        expected_revision: int,
        operations: Sequence[Mapping[str, Any]],
    ) -> TodoUpdateResult: ...

    def snapshot(self, session_id: str, turn_id: str) -> TodoSnapshot: ...

    def receipt(self, session_id: str, turn_id: str, call_id: str) -> TodoUpdateResult | None: ...

    def claim_finalization(self, session_id: str, turn_id: str) -> bool: ...

    def finalization_claimed(self, session_id: str, turn_id: str) -> bool: ...

    def persist_turn(self, session_id: str, turn_id: str) -> None: ...

    def expire_turn(self, session_id: str, turn_id: str) -> None: ...


def apply_todo_operations(
    snapshot: TodoSnapshot,
    operations: Sequence[Mapping[str, Any]],
    *,
    generated_ids: Sequence[str],
) -> tuple[TodoSnapshot, tuple[dict[str, str], ...]]:
    """Validate and apply one all-or-nothing operation batch in memory."""

    if not 1 <= len(operations) <= MAX_TODOS:
        raise TodoStateError("invalid_operations", f"Operations must contain 1 to {MAX_TODOS} items.")
    todos = list(snapshot.todos)
    by_id = {todo.id: index for index, todo in enumerate(todos)}
    targeted: set[str] = set()
    generated = iter(generated_ids)
    applied: list[dict[str, str]] = []

    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            raise TodoStateError("invalid_operation", f"Operation {index} must be an object.")
        op = operation.get("op")
        if op == "add":
            _validate_fields(operation, index, required={"op", "content", "status"})
            content = operation.get("content")
            status = operation.get("status")
            _validate_content(content, index)
            _validate_status(status, index)
            try:
                todo_id = next(generated)
            except StopIteration as exc:
                raise TodoStateError("invalid_operation", "Missing generated ID for add operation.") from exc
            _validate_todo_id(todo_id, index)
            if todo_id in by_id:
                raise TodoStateError("duplicate_id", f"Generated Todo ID {todo_id!r} already exists.")
            todo = TodoItem(todo_id, content, status)
            by_id[todo_id] = len(todos)
            todos.append(todo)
            applied.append(todo.to_dict() | {"op": "add"})
            continue

        todo_id = operation.get("id")
        _validate_todo_id(todo_id, index)
        if todo_id in targeted:
            raise TodoStateError("duplicate_target", f"Todo {todo_id!r} is targeted more than once.")
        targeted.add(todo_id)
        todo_index = by_id.get(todo_id)
        if todo_index is None:
            raise TodoStateError("todo_not_found", f"Todo {todo_id!r} does not exist.")

        if op == "remove":
            _validate_fields(operation, index, required={"op", "id"})
            todos.pop(todo_index)
            by_id = {todo.id: current for current, todo in enumerate(todos)}
            applied.append({"op": "remove", "id": todo_id})
            continue
        if op != "update":
            raise TodoStateError("invalid_operation", f"Unsupported operation {op!r} at index {index}.")

        _validate_fields(
            operation,
            index,
            required={"op", "id"},
            optional={"content", "status"},
        )
        current = todos[todo_index]
        has_content = "content" in operation
        has_status = "status" in operation
        if not has_content and not has_status:
            raise TodoStateError("invalid_operation", f"Update operation {index} must change content or status.")
        content = operation.get("content", current.content)
        status = operation.get("status", current.status)
        _validate_content(content, index)
        _validate_status(status, index)
        if content == current.content and status == current.status:
            raise TodoStateError("no_change", f"Update for Todo {todo_id!r} does not change its state.")
        todos[todo_index] = TodoItem(todo_id, content, status)
        normalized = {"op": "update", "id": todo_id}
        if has_content:
            normalized["content"] = content
        if has_status:
            normalized["status"] = status
        applied.append(normalized)

    if len(todos) > MAX_TODOS:
        raise TodoStateError("todo_limit", f"Todo list cannot contain more than {MAX_TODOS} items.")
    return TodoSnapshot(snapshot.revision + 1, tuple(todos)), tuple(applied)


def _validate_fields(
    operation: Mapping[str, Any],
    index: int,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    fields = set(operation)
    allowed = required | (optional or set())
    if not required <= fields or not fields <= allowed:
        raise TodoStateError(
            "invalid_operation",
            f"Operation {index} fields must match its operation type.",
        )


def _validate_todo_id(value: object, index: int) -> None:
    if not isinstance(value, str) or _TODO_ID_PATTERN.fullmatch(value) is None:
        raise TodoStateError("invalid_operation", f"Operation {index} requires a valid Todo ID.")


def _validate_content(value: object, index: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TodoStateError("invalid_content", f"Operation {index} content must be a non-blank string.")
    if len(value) > 500:
        raise TodoStateError("invalid_content", f"Operation {index} content cannot exceed 500 characters.")


def _validate_status(value: object, index: int) -> None:
    if value not in TODO_STATUSES:
        raise TodoStateError(
            "invalid_status",
            f"Operation {index} status must be one of {', '.join(TODO_STATUSES)}.",
        )


__all__ = [
    "MAX_TODOS",
    "TODO_STATUSES",
    "TodoItem",
    "TodoListStore",
    "TodoSnapshot",
    "TodoStateError",
    "TodoStatus",
    "TodoUpdateResult",
    "apply_todo_operations",
]
