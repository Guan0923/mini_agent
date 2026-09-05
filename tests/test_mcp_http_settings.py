from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.mcp import settings as mcp_settings
from backend.mcp.config import read_server_configs


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


def test_http_settings_credentials_roundtrip_and_type_switch(tmp_path, monkeypatch):
    keyring = MemoryKeyring()
    monkeypatch.setattr(mcp_settings, "_keyring_module", lambda: keyring)
    state = WebAppState(tmp_path / "state")
    body = {
        "name": "remote",
        "transport": "streamable_http",
        "url": "https://example.test/mcp",
        "headers": {"X-Region": "local"},
        "header_secrets": {"Authorization": "test-only-private-value"},
    }
    with TestClient(create_app(state)) as client:
        created = client.post("/api/settings/mcp/servers", json=body)
        assert created.status_code == 201, created.text
        assert created.json()["secret_headers"] == [{"name": "authorization", "configured": True}]
        assert "test-only-private-value" not in created.text
        raw = state.paths.mcp_file.read_text(encoding="utf-8")
        assert "test-only-private-value" not in raw and "keyring://" in raw
        assert read_server_configs(state.paths.mcp_file)[0].transport == "streamable_http"
        assert client.put("/api/settings/mcp/servers/remote/enabled", json={"enabled": False}).status_code == 200
        assert read_server_configs(state.paths.mcp_file)[0].url == body["url"]
        edit = {"transport": "streamable_http", "url": body["url"], "header_secrets": {"authorization": ""}}
        assert client.put("/api/settings/mcp/servers/remote", json=edit).status_code == 200
        assert list(keyring.values.values()) == ["test-only-private-value"]
        edit["header_secrets"] = {"authorization": "test-only-replacement"}
        assert client.put("/api/settings/mcp/servers/remote", json=edit).status_code == 200
        assert list(keyring.values.values()) == ["test-only-replacement"]
        edit["header_secrets"] = {}
        edit["remove_header_secrets"] = ["AUTHORIZATION"]
        cleared = client.put("/api/settings/mcp/servers/remote", json=edit)
        assert cleared.status_code == 200 and not cleared.json()["secret_headers"]
        assert not keyring.values
        edit.pop("remove_header_secrets")
        edit["header_secrets"] = {"authorization": "test-only-value"}
        client.put("/api/settings/mcp/servers/remote", json=edit)
        switched = client.put("/api/settings/mcp/servers/remote", json={"command": "python"})
        assert switched.status_code == 200 and switched.json()["transport"] == "stdio"
        assert not keyring.values
        assert client.delete("/api/settings/mcp/servers/remote").status_code == 204


@pytest.mark.parametrize(
    "extra",
    [
        {"command": "python"},
        {"env": {"A": "B"}},
        {"args": ["server.py"]},
        {"headers": {"Authorization": "test-only-private-value"}},
        {"headers": {"Host": "untrusted"}},
        {"headers": {"MCP-Protocol-Version": "wrong"}},
        {"headers": {"X-A": "one", "x-a": "two"}},
        {"header_secrets": {"Authorization": "test-only-private-value\r\ninjected"}},
        {"url": "https://user:test-only-private-value@example.test/mcp"},
        {"url": "https://example.test/mcp?token=test-only-private-value"},
    ],
)
def test_http_invalid_input_never_echoes_secret(tmp_path, extra):
    with TestClient(create_app(WebAppState(tmp_path / "state"))) as client:
        response = client.post(
            "/api/settings/mcp/servers",
            json={
                "name": "invalid",
                "transport": "streamable_http",
                "url": "http://127.0.0.1:1234/mcp",
                **extra,
            },
        )
        assert response.status_code == 422
        assert "test-only-private-value" not in response.text
        assert "input" not in json.dumps(response.json())


def test_stdio_rejects_http_fields_and_old_config_is_preserved(tmp_path):
    with TestClient(create_app(WebAppState(tmp_path / "state"))) as client:
        bad = client.post(
            "/api/settings/mcp/servers", json={"name": "bad", "command": "python", "url": "https://example.test/mcp"}
        )
        assert bad.status_code == 422
        old = client.post("/api/settings/mcp/servers", json={"name": "old", "command": "python", "args": ["server.py"]})
        assert old.status_code == 201 and old.json()["transport"] == "stdio"
