from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.state import WebAppState


def test_local_apis_need_no_session_credentials_and_removed_routes_are_absent(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".mini_agent")
    with TestClient(create_app(state)) as client:
        headers = {"Cookie": "session=obsolete", "Authorization": "Bearer obsolete"}
        assert client.get("/api/settings", headers=headers).status_code == 200
        assert client.get("/api/projects").status_code == 200

        sidebar = client.post("/api/sidebar-threads", json={})
        assert sidebar.status_code == 201
        session_id = sidebar.json()["session_id"]
        assert client.get("/api/turns", params={"session_id": session_id}).status_code == 200

        assert client.post("/api/auth/guest").status_code == 404
        assert client.post("/api/auth/login", json={}).status_code == 404
        assert client.post("/api/sync/push", json={}).status_code == 404


def test_browser_writes_require_an_allowed_loopback_origin_but_cli_writes_do_not(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".mini_agent")
    with TestClient(create_app(state)) as client:
        assert (
            client.post(
                "/api/sidebar-threads",
                json={},
                headers={"Origin": "https://outside.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/sidebar-threads",
                json={},
                headers={"Origin": "http://127.0.0.1:5173"},
            ).status_code
            == 201
        )
        assert client.post("/api/sidebar-threads", json={}).status_code == 201

        preflight = client.options(
            "/api/sidebar-threads",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert "access-control-allow-credentials" not in preflight.headers


def test_configured_browser_origins_must_remain_loopback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINI_AGENT_ALLOWED_ORIGINS", "https://outside.example")
    state = WebAppState(tmp_path / ".mini_agent")

    try:
        with pytest.raises(ValueError, match="loopback origins"):
            create_app(state)
    finally:
        state.close()


def test_provider_api_never_echoes_plaintext_and_reopens_the_encrypted_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINI_AGENT_LOCAL_DEK_FALLBACK", "test-local-key-material-that-is-at-least-32-bytes")
    root = tmp_path / ".mini_agent"
    state = WebAppState(root)
    payload = {
        "provider_name": "local-test",
        "protocol": "responses",
        "base_url": "https://example.test/v1",
        "model": "demo",
        "api_key": "provider-secret",
    }
    with TestClient(create_app(state)) as client:
        response = client.put("/api/settings/providers", json=payload)
        assert response.status_code == 200
        assert "provider-secret" not in response.text
        assert "api_key" not in response.json()

    with sqlite3.connect(root / "runtime" / "state.db") as connection:
        raw = str(connection.execute("SELECT provider_configs_json FROM provider_settings").fetchone()[0])
    assert "provider-secret" not in raw
    assert "v4:" in raw

    reopened = WebAppState(root)
    try:
        assert reopened.model_config("local-test").api_key == "provider-secret"
        assert "provider-secret" not in str(reopened.settings_payload())
    finally:
        reopened.close()
