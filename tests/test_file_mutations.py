"""Tests for run_command as the primary file-operation and system tool."""

from pathlib import Path

import pytest

from mini_agent.tools import ToolError, ToolRegistry


def test_run_command_is_read_only_and_does_not_require_confirmation(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    assert tools.is_read_only("run_command") is True
    assert tools.requires_confirmation("run_command") is False


def test_run_command_is_not_retryable(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    assert tools.is_retryable("run_command") is False


def test_run_command_lists_and_reads_files(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "note.txt").write_text("hello world", encoding="utf-8")

    result = tools.invoke("run_command", {"command": "Get-Content note.txt"}, confirmed=True)
    assert "hello world" in result


def test_run_command_writes_files(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    tools.invoke(
        "run_command",
        {"command": "[System.IO.File]::WriteAllText('output.txt', 'written content')"},
        confirmed=True,
    )
    assert (tmp_path / "output.txt").read_text(encoding="utf-8").strip() == "written content"


def test_run_command_validates_command_type(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match="non-empty string"):
        tools.invoke("run_command", {"command": ""}, confirmed=True)


def test_run_command_workspace_directory_is_cwd(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    result = tools.invoke("run_command", {"command": "Get-Location"}, confirmed=True)
    assert str(tmp_path.resolve()) in result


def test_tool_registry_has_three_tools(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)

    names = tools.names()
    assert set(names) == {"web_search", "web_fetch", "run_command"}
    assert tools.read_only_names() == ["web_search", "web_fetch", "run_command"]
