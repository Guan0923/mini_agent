from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from backend.mcp.settings import KEYRING_SERVICE, McpSettingsStore
from backend.runtime import build_application
from backend.runtime.application import factory
from backend.tools import ToolError


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_set = False

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        if self.fail_set:
            raise RuntimeError("keyring unavailable")
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


def _write_skill(root: Path, directory: str, *, name: str = "demo") -> Path:
    skill = root / directory
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Demo settings skill.\n"
        "metadata:\n"
        "  owner: local\n"
        "allowed-tools:\n"
        "  - read_file\n"
        "---\n"
        "Use the demo workflow.\n",
        encoding="utf-8",
    )
    return skill


def test_skill_settings_persist_and_apply_to_the_next_runner(
    tmp_path: Path,
    local_sandbox_runtime: None,
) -> None:
    root = tmp_path / "user-data"
    state = WebAppState(root)
    skill = _write_skill(state.paths.skills_dir, "folder-id", name="folder-id")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    current = build_application(workspace, planner_name="rule", paths=state.paths)
    try:
        with TestClient(create_app(state)) as client:
            payload = client.get("/api/settings/skills").json()
            assert payload["enabled"] is True
            assert payload["skills"] == [
                {
                    "directory": "folder-id",
                    "name": "folder-id",
                    "description": "Demo settings skill.",
                    "metadata": {"owner": "local"},
                    "allowed_tools": ["read_file"],
                    "root": skill.resolve().as_posix(),
                    "enabled": True,
                }
            ]
            assert client.put("/api/settings/skills/folder-id/enabled", json={"enabled": "false"}).status_code == 422
            assert client.put("/api/settings/skills/folder-id/enabled", json={"enabled": False}).status_code == 200
            assert client.put("/api/settings/skills/enabled", json={"enabled": "false"}).status_code == 422

        manifest = skill / "SKILL.md"
        assert current.runner.skill_catalog.names() == ("folder-id",)
        assert "Use the demo workflow." in current.runner.tools.invoke("read_file", {"path": str(manifest)})

        next_runner = build_application(workspace, planner_name="rule", paths=ClientPaths(root))
        try:
            assert next_runner.runner.skill_catalog.names() == ()
            with pytest.raises(ToolError, match="approved workspace"):
                next_runner.runner.tools.invoke("read_file", {"path": str(manifest)})
        finally:
            next_runner.close()
    finally:
        current.close()

    assert state.settings.skill_config() == {"disabled": ["folder-id"]}


def test_skill_import_cancel_validation_copy_conflict_and_delete(tmp_path: Path) -> None:
    source = _write_skill(tmp_path / "imports", "demo")
    nested = source / "references" / "guide.md"
    nested.parent.mkdir()
    nested.write_text("guide", encoding="utf-8")
    root = tmp_path / "user-data"
    state = WebAppState(root, project_picker=lambda: None)
    with TestClient(create_app(state)) as client:
        assert client.post("/api/settings/skills/import").status_code == 204

    state = WebAppState(root, project_picker=lambda: source)
    with TestClient(create_app(state)) as client:
        imported = client.post("/api/settings/skills/import")
        assert imported.status_code == 201
        assert imported.json() == {"directory": "demo"}
        assert (state.paths.skills_dir / "demo" / "references" / "guide.md").read_text(encoding="utf-8") == "guide"
        assert client.post("/api/settings/skills/import").status_code == 409
        assert client.delete("/api/settings/skills/%2E%2E").status_code == 404
        assert client.delete("/api/settings/skills/demo").status_code == 204
        assert not (state.paths.skills_dir / "demo").exists()

    invalid = tmp_path / "invalid-skill"
    invalid.mkdir()
    state = WebAppState(tmp_path / "invalid-user-data", project_picker=lambda: invalid)
    with TestClient(create_app(state)) as client:
        assert client.post("/api/settings/skills/import").status_code == 422


def _server_payload(command: str = "demo-command") -> dict[str, object]:
    return {
        "name": "demo",
        "command": command,
        "args": ["--flag"],
        "cwd": None,
        "env": {"MODE": "test"},
        "secrets": {"API_TOKEN": "first-secret"},
        "remove_secrets": [],
        "enabled": True,
    }


def _server_update_payload(command: str = "demo-command") -> dict[str, object]:
    payload = _server_payload(command)
    del payload["name"]
    return payload


def test_mcp_crud_persists_redacts_and_manages_keyring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.mcp import settings as mcp_settings

    fake = FakeKeyring()
    monkeypatch.setattr(mcp_settings, "_keyring_module", lambda: fake)
    state = WebAppState(tmp_path / "user-data")
    state.settings.config_store.replace_section(
        "capabilities",
        {"skills": True, "mcp": False, "plugins": True},
    )
    with TestClient(create_app(state)) as client:
        initial = client.get("/api/settings/mcp").json()
        assert initial == {"enabled": False, "servers": []}
        assert client.put("/api/settings/mcp/enabled", json={"enabled": "true"}).status_code == 422
        assert client.put("/api/settings/mcp/enabled", json={"enabled": True}).json()["enabled"] is True
        assert state.settings.config_store.read()["capabilities"]["plugins"] is True

        overlapping = _server_payload()
        overlapping["env"] = {"API_TOKEN": "plain"}
        assert client.post("/api/settings/mcp/servers", json=overlapping).status_code == 422

        created = client.post("/api/settings/mcp/servers", json=_server_payload())
        assert created.status_code == 201
        assert created.json()["secret_env"] == [{"name": "API_TOKEN", "configured": True}]
        assert "first-secret" not in created.text
        assert fake.values[(KEYRING_SERVICE, "demo.API_TOKEN")] == "first-secret"
        raw = state.paths.mcp_file.read_text(encoding="utf-8")
        assert "first-secret" not in raw
        assert 'API_TOKEN = "keyring://mini-agent-mcp/demo.API_TOKEN"' in raw

        cannot_rename = {**_server_payload(), "name": "renamed", "secrets": {}}
        assert client.put("/api/settings/mcp/servers/demo", json=cannot_rename).status_code == 422

        keep = {**_server_update_payload("updated-command"), "secrets": {"API_TOKEN": ""}}
        kept = client.put("/api/settings/mcp/servers/demo", json=keep)
        assert kept.status_code == 200, kept.text
        assert fake.values[(KEYRING_SERVICE, "demo.API_TOKEN")] == "first-secret"

        replaced = client.put(
            "/api/settings/mcp/servers/demo",
            json={**_server_update_payload("updated-command"), "secrets": {"API_TOKEN": "second-secret"}},
        )
        assert replaced.status_code == 200
        assert "second-secret" not in replaced.text
        assert fake.values[(KEYRING_SERVICE, "demo.API_TOKEN")] == "second-secret"

        disabled = client.put("/api/settings/mcp/servers/demo/enabled", json={"enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        cleared = client.put(
            "/api/settings/mcp/servers/demo",
            json={
                **_server_update_payload("updated-command"),
                "secrets": {},
                "remove_secrets": ["API_TOKEN"],
                "enabled": False,
            },
        )
        assert cleared.status_code == 200
        assert cleared.json()["secret_env"] == []
        assert (KEYRING_SERVICE, "demo.API_TOKEN") not in fake.values

        assert client.delete("/api/settings/mcp/servers/demo").status_code == 204
        assert client.get("/api/settings/mcp").json()["servers"] == []


def test_mcp_keyring_or_toml_failure_rolls_back_without_secret_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.mcp import settings as mcp_settings

    paths = ClientPaths(tmp_path / "user-data")
    paths.ensure()
    fake = FakeKeyring()
    monkeypatch.setattr(mcp_settings, "_keyring_module", lambda: fake)
    store = McpSettingsStore(paths)
    store.create(
        name="demo",
        command="command",
        args=(),
        cwd=None,
        env={},
        secrets={"API_TOKEN": "original-secret"},
        enabled=True,
    )
    original_toml = paths.mcp_file.read_text(encoding="utf-8")

    def fail_write(_path: Path, _content: str) -> None:
        raise OSError("write failed with hidden data")

    monkeypatch.setattr(mcp_settings, "atomic_write_text", fail_write)
    with pytest.raises(ValueError) as error:
        store.update(
            "demo",
            command="changed",
            args=(),
            cwd=None,
            env={},
            secrets={"API_TOKEN": "replacement-secret"},
            remove_secrets=set(),
            enabled=True,
        )
    assert "replacement-secret" not in str(error.value)
    assert fake.values[(KEYRING_SERVICE, "demo.API_TOKEN")] == "original-secret"
    assert paths.mcp_file.read_text(encoding="utf-8") == original_toml

    fake.fail_set = True
    with pytest.raises(ValueError) as keyring_error:
        store.update(
            "demo",
            command="changed",
            args=(),
            cwd=None,
            env={},
            secrets={"API_TOKEN": "never-persisted"},
            remove_secrets=set(),
            enabled=True,
        )
    assert "never-persisted" not in str(keyring_error.value)
    assert paths.mcp_file.read_text(encoding="utf-8") == original_toml


def test_mcp_master_switch_skips_parsing_and_real_connection_test_closes(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / "user-data")
    paths.ensure()
    paths.mcp_file.write_text("not valid toml", encoding="utf-8")

    assert len(factory._external_resources(paths, {})) == 0
    with pytest.raises(ToolError, match="Invalid MCP configuration"):
        factory._external_resources(paths, {"capabilities": {"mcp": True}})

    state = WebAppState(tmp_path / "api-user-data")
    script = Path(__file__).parent / "support" / "trace_mcp_server.py"
    payload = {
        "name": "trace",
        "command": sys.executable,
        "args": [str(script)],
        "cwd": str(script.parent),
        "env": {},
        "secrets": {},
        "remove_secrets": [],
        "enabled": False,
    }
    with TestClient(create_app(state)) as client:
        assert client.post("/api/settings/mcp/servers", json=payload).status_code == 201
        tested = client.post("/api/settings/mcp/servers/trace/test")
        assert tested.status_code == 200, tested.text
        assert tested.json() == {"tools": ["mcp_trace_inspect_trace"], "count": 1}
