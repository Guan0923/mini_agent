from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from backend.mcp.client import start_external_tools
from backend.mcp.client.adapters import render_mcp_value
from backend.mcp.client.subscriptions import MAX_RESOURCE_UPDATES, ResourceSubscriptions
from backend.mcp.config import McpServerConfig
from backend.tools import ToolError, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/support/mcp_capabilities_server.py"
LEGACY_PYTHON = Path(os.environ.get("MINI_AGENT_MCP_V1_PYTHON", str(ROOT / ".tmp-mcp-v1/Scripts/python.exe")))


@contextmanager
def peer(era="modern", transport="stdio", *, token=None, only=None):
    python = sys.executable if era == "modern" else str(LEGACY_PYTHON)
    if era == "legacy" and not Path(python).exists():
        pytest.skip("Install the isolated MCP v1 test environment or set MINI_AGENT_MCP_V1_PYTHON")
    env = {}
    if only:
        env["MCP_TEST_ONLY"] = only
    if token:
        env["MCP_TEST_TOKEN"] = token
    if transport == "stdio":
        yield McpServerConfig("fixture", python, (str(SCRIPT),), env=env)
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [python, str(SCRIPT), "--port", str(port)],
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(trust_env=False) as http:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError(process.stderr.read().decode(errors="replace"))
                try:
                    if http.get(url + "/health", timeout=0.2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("MCP HTTP fixture did not start")
                time.sleep(0.02)
        path = "/mcp/" if era == "legacy" else "/mcp"
        yield McpServerConfig("fixture", transport="streamable_http", url=url + path)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


def tool_map(resources):
    return {tool.name: tool.handler for tool in resources}


def wait_updates(tools, *, status=None):
    deadline = time.monotonic() + 3
    while True:
        snapshot = json.loads(tools["get_mcp_resource_updates"](server="fixture"))
        if (
            snapshot["changed_uris"]
            if status is None
            else any(s["status"] == status for s in snapshot["subscriptions"])
        ):
            return snapshot
        if time.monotonic() >= deadline:
            pytest.fail(f"MCP change did not arrive: {snapshot}")
        time.sleep(0.01)


@pytest.mark.parametrize("era", ["modern", "legacy"])
@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_real_protocol_matrix(era, transport):
    with peer(era, transport) as config:
        resources = start_external_tools((config,))
        try:
            manager = resources.manager
            expected = "2026-07-28" if era == "modern" else "2025-11-25"
            assert manager.protocol_versions["fixture"] == expected
            tools = tool_map(resources)
            assert all(tool.requires_confirmation and not tool.read_only for tool in resources)
            first = json.loads(tools["list_mcp_resources"](server="fixture"))
            second = json.loads(tools["list_mcp_resources"](server="fixture", cursor=first["next_cursor"]))
            assert first["resources"][0]["uri"] == "notes://one"
            assert second["resources"][0]["uri"] == "notes://two"
            assert "notes://{name}" in tools["list_mcp_resource_templates"](server="fixture")
            assert "resource revision 0" in tools["read_mcp_resource"](server="fixture", uri="notes://one")
            binary = tools["read_mcp_resource"](server="fixture", uri="notes://binary")
            assert "YWJj" not in binary and '"content_omitted": true' in binary
            prompts = json.loads(tools["list_mcp_prompts"](server="fixture"))
            assert prompts["prompts"][0]["arguments"][0]["required"]
            assert "review-more" in tools["list_mcp_prompts"](server="fixture", cursor=prompts["next_cursor"])
            result = json.loads(
                tools["get_mcp_prompt"](server="fixture", name="review", arguments={"language": "Chinese"})
            )
            assert [message["role"] for message in result["messages"]] == ["user", "assistant"]
            with pytest.raises(ToolError):
                tools["get_mcp_prompt"](server="fixture", name="review")
            with pytest.raises(ToolError):
                tools["read_mcp_resource"](server="not-configured", uri="notes://one")
            assert "active" in tools["subscribe_mcp_resource"](server="fixture", uri="notes://one")
            tools["mcp_fixture_change"]()
            assert wait_updates(tools)["changed_uris"] == ["notes://one"]
            tools["mcp_fixture_change"]()
            assert wait_updates(tools)["changed_uris"] == ["notes://one"]
            assert "resource revision 2" in tools["read_mcp_resource"](server="fixture", uri="notes://one")
            assert json.loads(tools["get_mcp_resource_updates"](server="fixture"))["changed_uris"] == []
            tools["unsubscribe_mcp_resource"](server="fixture", uri="notes://one")
            assert json.loads(tools["get_mcp_resource_updates"](server="fixture"))["subscriptions"] == []
            if era == "modern":
                assert "rejected" in tools["subscribe_mcp_resource"](server="fixture", uri="notes://reject")
                tools["subscribe_mcp_resource"](server="fixture", uri="notes://one")
                tools["mcp_fixture_end"]()
                assert wait_updates(tools, status="ended")["resync_required"]
            else:
                with pytest.raises(ToolError):
                    tools["subscribe_mcp_resource"](server="fixture", uri="notes://reject")
            assert manager.describe("fixture")["counts"] == {
                "tools": 3,
                "resources": 2,
                "resource_templates": 1,
                "prompts": 2,
            }
        finally:
            resources.close()
        assert not manager._thread.is_alive()
        assert not manager._sessions


@pytest.mark.parametrize("only", ["resources", "prompts"])
def test_real_server_without_tools(only):
    with peer(only=only) as config:
        resources = start_external_tools((config,))
        try:
            names = tool_map(resources)
            assert not any(name.startswith("mcp_fixture_") for name in names)
            assert f"list_mcp_{only}" in names
        finally:
            resources.close()


def test_new_tools_use_registry_approval_and_argument_validation():
    with peer(only="prompts") as config:
        resources = start_external_tools((config,))
        try:
            registry = ToolRegistry()
            for tool in resources:
                registry.register(tool)
            with pytest.raises(ToolError):
                registry.invoke(
                    "get_mcp_prompt", {"server": "fixture", "name": "review", "arguments": {"language": "en"}}
                )
            with pytest.raises(ToolError):
                registry.invoke(
                    "get_mcp_prompt",
                    {"server": "fixture", "name": "review", "arguments": {"language": 7}},
                    confirmed=True,
                )
        finally:
            resources.close()


def test_updates_are_bounded_and_loss_is_reported():
    subscriptions = ResourceSubscriptions()
    subscriptions.states["notes://one"] = "active"
    for index in range(MAX_RESOURCE_UPDATES + 1):
        subscriptions.record(f"notes://{index}")
    snapshot = subscriptions.snapshot()
    assert len(snapshot["changed_uris"]) == MAX_RESOURCE_UPDATES
    assert snapshot["overflow"] and snapshot["resync_required"]
    subscriptions.lost()
    assert subscriptions.snapshot()["subscriptions"][0]["status"] == "lost"
    asyncio.run(subscriptions.close())
    assert not subscriptions.tasks and not subscriptions.changed


@pytest.mark.parametrize("era", ["modern", "legacy"])
def test_disconnected_peer_does_not_report_active_subscription(era):
    with peer(era) as config:
        resources = start_external_tools((config,))
        try:
            tools = tool_map(resources)
            tools["subscribe_mcp_resource"](server="fixture", uri="notes://one")
            with pytest.raises(ToolError):
                tools["mcp_fixture_echo"](value="disconnect-test-peer")
            assert wait_updates(tools, status="lost")["resync_required"]
        finally:
            resources.close()


@pytest.mark.parametrize("era", ["modern", "legacy"])
def test_connection_rebuild_preserves_subscription_gap(era):
    with peer(era) as config:
        resources = start_external_tools((config,))
        try:
            tools = tool_map(resources)
            tools["subscribe_mcp_resource"](server="fixture", uri="notes://one")
            manager = resources.manager
            manager._submit(manager._close_server("fixture"), timeout=5)
            manager._submit(manager._open_server(config), timeout=15)
            updates = json.loads(tools["get_mcp_resource_updates"](server="fixture"))
            assert updates["resync_required"]
            assert updates["subscriptions"] == [{"uri": "notes://one", "status": "lost"}]
        finally:
            resources.close()


@pytest.mark.parametrize("era", ["modern", "legacy"])
def test_real_http_auth_and_no_redirects(era, monkeypatch):
    with peer(era, "http", token="test-only-credential") as config:
        with pytest.raises(ToolError):
            start_external_tools((config,))
        monkeypatch.setenv("MCP_TEST_AUTH", "test-only-credential")
        authenticated = replace(config, header_refs={"Authorization": "env://MCP_TEST_AUTH"})
        resources = start_external_tools((authenticated,))
        try:
            assert resources.manager.describe("fixture")["counts"]["resources"] == 2
        finally:
            resources.close()
        redirect = replace(authenticated, url=config.url.split("/mcp")[0] + "/redirect")
        with pytest.raises(ToolError) as failure:
            start_external_tools((redirect,))
        assert "test-only-credential" not in str(failure.value)


def test_prompt_rendering_is_bounded_and_preserves_binary_metadata():
    value = {"messages": [{"role": "user", "content": {"type": "image", "data": "YWJj", "mime_type": "image/png"}}]}
    rendered = render_mcp_value(value)
    assert "YWJj" not in rendered and '"role": "user"' in rendered
    rendered = render_mcp_value({"contents": [{"text": '"' * 30000}]})
    assert len(rendered) <= 20000 and json.loads(rendered)["truncated"]
