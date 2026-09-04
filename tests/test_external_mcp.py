from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.mcp import client as mcp_client
from backend.mcp.client.manager import _list_all_tools


def _definition(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=name, inputSchema={"type": "object"})


class _PaginatedSession:
    def __init__(self, pages: dict[str | None, SimpleNamespace]) -> None:
        self.pages = pages
        self.cursors: list[str | None] = []

    async def list_tools(self, *, params=None):
        cursor = params.cursor if params is not None else None
        self.cursors.append(cursor)
        return self.pages[cursor]


def test_external_mcp_collects_all_tool_pages_in_order() -> None:
    session = _PaginatedSession(
        {
            None: SimpleNamespace(tools=[_definition("first")], nextCursor="page-2"),
            "page-2": SimpleNamespace(tools=[_definition("second")], nextCursor="page-3"),
            "page-3": SimpleNamespace(tools=[_definition("third")], nextCursor=None),
        }
    )

    definitions = asyncio.run(_list_all_tools(session))

    assert [definition.name for definition in definitions] == ["first", "second", "third"]
    assert session.cursors == [None, "page-2", "page-3"]


def test_external_mcp_stops_after_a_repeated_cursor_and_keeps_the_current_page() -> None:
    session = _PaginatedSession(
        {
            None: SimpleNamespace(tools=[_definition("first")], nextCursor="repeat"),
            "repeat": SimpleNamespace(tools=[_definition("second")], nextCursor="repeat"),
        }
    )

    definitions = asyncio.run(_list_all_tools(session))

    assert [definition.name for definition in definitions] == ["first", "second"]
    assert session.cursors == [None, "repeat"]


def test_external_mcp_discovers_and_calls_tools_from_all_real_stdio_pages() -> None:
    script = Path(__file__).parent / "support" / "paginated_mcp_server.py"
    resources = mcp_client.start_external_tools(
        (
            mcp_client.McpServerConfig(
                "paginated",
                sys.executable,
                (str(script),),
                cwd=str(script.parent),
            ),
        )
    )

    try:
        assert [tool.name for tool in resources] == ["mcp_paginated_first_page", "mcp_paginated_second_page"]
        assert resources[0].handler() == "called first_page"
        assert resources[1].handler() == "called second_page"
    finally:
        resources.close()


def test_project_mcp_file_is_ignored_and_user_file_is_the_only_source(tmp_path: Path) -> None:
    global_file = tmp_path / "home" / "mcp" / "servers.toml"
    global_file.parent.mkdir(parents=True)
    global_file.write_text(
        """
[servers.global_only]
command = "global-command"
args = ["--global"]

[servers.shared]
command = "old-command"
args = ["--old"]
cwd = "old-cwd"
env_refs = { SECRET = "env://SECRET" }
""".strip(),
        encoding="utf-8",
    )
    project_file = tmp_path / "workspace" / ".mini_agent" / "mcp.toml"
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        """
[servers.shared]
command = "project-command"
args = ["--project"]
""".strip(),
        encoding="utf-8",
    )

    configs = {item.name: item for item in mcp_client.load_server_configs(global_file, project_file)}

    assert set(configs) == {"global_only", "shared"}
    assert configs["shared"].command == "old-command"
    assert configs["shared"].args == ("--old",)
    assert configs["shared"].cwd == "old-cwd"
    assert configs["shared"].env_refs == {"SECRET": "env://SECRET"}


def test_external_mcp_submit_cancels_a_timed_out_future(monkeypatch) -> None:
    class PendingFuture:
        def __init__(self) -> None:
            self.cancelled = False

        def result(self, *, timeout: float | None = None):
            assert timeout == 0.01
            raise TimeoutError

        def cancel(self) -> None:
            self.cancelled = True

    future = PendingFuture()
    monkeypatch.setattr(mcp_client.asyncio, "run_coroutine_threadsafe", lambda _value, _loop: future)
    manager = object.__new__(mcp_client.ExternalMcpManager)
    manager._loop = object()

    with pytest.raises(TimeoutError):
        manager._submit(object(), timeout=0.01)

    assert future.cancelled is True


def test_external_mcp_tools_are_approval_gated_and_not_plan_safe(tmp_path: Path, monkeypatch) -> None:
    global_file = tmp_path / "mcp.toml"
    global_file.write_text('[servers.demo]\ncommand = "demo-server"\n', encoding="utf-8")

    class Manager:
        def __init__(self, configs) -> None:
            assert configs[0].name == "demo"
            self.definitions = {
                "demo": [
                    SimpleNamespace(
                        name="echo",
                        description="Echo input",
                        inputSchema={"type": "object", "properties": {"value": {"type": "string"}}},
                    )
                ]
            }

        def call(self, server_name: str, tool_name: str, arguments: dict[str, object]) -> str:
            return f"{server_name}/{tool_name}:{arguments['value']}"

        def close(self) -> None:
            return None

    monkeypatch.setattr(mcp_client, "ExternalMcpManager", Manager)
    tools = mcp_client.load_external_tools(global_file, tmp_path / "missing.toml")

    assert len(tools) == 1
    assert tools[0].name == "mcp_demo_echo"
    assert tools[0].requires_confirmation is True
    assert tools[0].read_only is False
    assert tools[0].handler(value="ok") == "demo/echo:ok"
    mcp_client.close_external_tools()
