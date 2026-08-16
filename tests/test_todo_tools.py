from pathlib import Path

import pytest

from backend.planning.prompts import compose_system_prompt
from backend.tools import ToolError, build_tool_registry


def _todos(*items: tuple[str, str]) -> dict[str, object]:
    return {"todos": [{"content": content, "status": status} for content, status in items]}


def test_todo_write_echoes_per_status_counts(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    output = registry.invoke(
        "todo_write",
        _todos(
            ("explore the repository", "completed"),
            ("implement the tool", "in_progress"),
            ("add tests", "pending"),
        ),
    )

    assert output == ("Todo list updated: 3 items — pending: 1, in_progress: 1, completed: 1")


def test_todo_write_replaces_the_list_and_allows_clearing(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    registry.invoke("todo_write", _todos(("first", "pending"), ("second", "in_progress")))
    output = registry.invoke("todo_write", _todos(("third", "completed")))

    assert output == "Todo list updated: 1 items — pending: 0, in_progress: 0, completed: 1"
    assert registry.invoke("todo_write", {"todos": []}) == (
        "Todo list updated: 0 items — pending: 0, in_progress: 0, completed: 0"
    )


def test_todo_write_rejects_duplicate_content(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    with pytest.raises(ToolError, match="Duplicate todo content"):
        registry.invoke("todo_write", _todos(("same", "pending"), ("same", "completed")))


def test_todo_write_rejects_blank_content(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    with pytest.raises(ToolError, match="non-blank"):
        registry.invoke("todo_write", {"todos": [{"content": "   ", "status": "pending"}]})


def test_todo_write_ignores_extra_item_fields(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    output = registry.invoke(
        "todo_write",
        {"todos": [{"content": "work", "status": "in_progress", "priority": 1, "id": "x"}]},
    )

    assert output == "Todo list updated: 1 items — pending: 0, in_progress: 1, completed: 0"


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({}, "todos"),
        ({"todos": "not-an-array"}, "todos"),
        ({"todos": [{"content": "work"}]}, "status"),
        ({"todos": [{"status": "pending"}]}, "content"),
        ({"todos": [{"content": "work", "status": "blocked"}]}, "status"),
        ({"todos": [{"content": "", "status": "pending"}]}, "content"),
        ({"todos": ["not-an-object"]}, "todos"),
    ],
)
def test_todo_write_schema_rejects_invalid_arguments(
    tmp_path: Path, arguments: dict[str, object], message: str
) -> None:
    registry = build_tool_registry(tmp_path)

    with pytest.raises(ToolError, match=f"Invalid arguments.*{message}"):
        registry.invoke("todo_write", arguments)


def test_todo_write_rejects_more_than_100_items(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)
    items = [{"content": f"item {index}", "status": "pending"} for index in range(101)]

    with pytest.raises(ToolError, match="Invalid arguments"):
        registry.invoke("todo_write", {"todos": items})


def test_todo_write_is_registered_but_not_read_only(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    assert "todo_write" in registry.names()
    assert registry.is_read_only("todo_write") is False
    assert "todo_write" not in registry.read_only_names()


def test_agent_prompt_requires_todo_write_tool() -> None:
    assert "`todo_write`" in compose_system_prompt("agent")
