from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from backend.mcp import client as mcp_client


def test_project_mcp_server_fully_overrides_global_definition(tmp_path: Path) -> None:
    global_file = tmp_path / "home" / "mcp.toml"
    global_file.parent.mkdir()
    global_file.write_text(
        """
[servers.global_only]
command = "global-command"
args = ["--global"]

[servers.shared]
command = "old-command"
args = ["--old"]
cwd = "old-cwd"
env = { SECRET = "old-secret" }
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
    assert configs["shared"].command == "project-command"
    assert configs["shared"].args == ("--project",)
    assert configs["shared"].cwd is None
    assert configs["shared"].env is None


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
