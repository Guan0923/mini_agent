from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp import types

from backend.mcp import McpToolAdapter, create_server
from backend.mcp.cli import parse_args
from backend.tools import ToolRegistry, build_tool_registry


def test_mcp_definitions_match_the_safe_registry_specs(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)
    adapter = McpToolAdapter(registry)

    definitions = adapter.definitions()

    assert [tool.name for tool in definitions] == ["read_file", "glob", "grep"]
    safe_specs = {
        spec.name: spec for spec in registry.read_only_specs() if not registry.requires_confirmation(spec.name)
    }
    assert {tool.name: tool.inputSchema for tool in definitions} == {
        name: spec.parameters for name, spec in safe_specs.items()
    }
    assert {tool.name: tool.description for tool in definitions} == {
        name: spec.description for name, spec in safe_specs.items()
    }


def test_mcp_server_registers_the_safe_tool_definitions(tmp_path: Path) -> None:
    server = create_server(tmp_path)

    result = asyncio.run(server.request_handlers[types.ListToolsRequest](types.ListToolsRequest()))

    assert [tool.name for tool in result.root.tools] == ["read_file", "glob", "grep"]


def test_mcp_invokes_a_safe_tool_inside_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello from Mini-Agent\n", encoding="utf-8")
    adapter = McpToolAdapter(build_tool_registry(tmp_path))

    result = adapter.invoke("read_file", {"path": "note.txt"})

    assert result.isError is False
    assert result.content[0].text == "note.txt: lines 1-1 of 1\nhello from Mini-Agent\n"


def test_mcp_preserves_workspace_and_argument_validation(tmp_path: Path) -> None:
    adapter = McpToolAdapter(build_tool_registry(tmp_path))

    traversal = adapter.invoke("read_file", {"path": "../outside.txt"})
    invalid = adapter.invoke("read_file", {})

    assert traversal.isError is True
    assert "workspace" in traversal.content[0].text.lower()
    assert invalid.isError is True
    assert "Invalid arguments" in invalid.content[0].text


@pytest.mark.parametrize("name", ["write_file", "edit_file", "run_command", "web_search", "web_fetch"])
def test_mcp_does_not_expose_tools_that_need_confirmation(tmp_path: Path, name: str) -> None:
    adapter = McpToolAdapter(build_tool_registry(tmp_path))

    result = adapter.invoke(name, {})

    assert result.isError is True
    assert "not available" in result.content[0].text


def test_mcp_rejects_unknown_tools(tmp_path: Path) -> None:
    result = McpToolAdapter(ToolRegistry()).invoke("unknown", {})

    assert result.isError is True
    assert "not available" in result.content[0].text


def test_mcp_cli_accepts_an_existing_workspace_and_rejects_missing_one(tmp_path: Path) -> None:
    assert parse_args(["--workspace", str(tmp_path)]).workspace == tmp_path.resolve()

    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--workspace", str(tmp_path / "missing")])

    assert exc_info.value.code == 2
