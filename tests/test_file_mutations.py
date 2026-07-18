"""Tests for registered file tools and command permissions."""

from pathlib import Path

import pytest

from mini_agent.tools import ConfirmationRequired, ToolError, ToolRegistry


def test_registry_exposes_specialized_tools_with_separate_permissions(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    assert tools.names() == [
        "read_file",
        "glob",
        "grep",
        "web_search",
        "web_fetch",
        "write_file",
        "edit_file",
        "run_command",
    ]
    assert tools.read_only_names() == ["read_file", "glob", "grep", "web_search", "web_fetch"]
    for name in ("write_file", "edit_file", "run_command"):
        assert tools.is_read_only(name) is False
        assert tools.requires_confirmation(name) is True
        assert tools.is_retryable(name) is False


def test_read_only_file_tools_do_not_require_confirmation(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "note.txt").write_text("hello world", encoding="utf-8")

    assert "hello world" in tools.invoke("read_file", {"path": "note.txt"})
    assert tools.invoke("glob", {"pattern": "*.txt"}) == "note.txt"
    assert tools.invoke("grep", {"pattern": "world"}) == "note.txt:1:hello world"


def test_mutating_tools_require_confirmation_before_invocation(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "note.txt").write_text("old", encoding="utf-8")

    calls = [
        ("write_file", {"path": "new.txt", "content": "new"}),
        ("edit_file", {"path": "note.txt", "old_text": "old", "new_text": "new"}),
        ("run_command", {"command": "Get-Location"}),
    ]
    for name, arguments in calls:
        with pytest.raises(ConfirmationRequired):
            tools.invoke(name, arguments)

    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "old"


def test_confirmed_run_command_uses_workspace_and_validates_arguments(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    result = tools.invoke("run_command", {"command": "Get-Location"}, confirmed=True)
    assert str(tmp_path.resolve()) in result
    with pytest.raises(ToolError, match="non-empty string"):
        tools.invoke("run_command", {"command": ""}, confirmed=True)
