from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import REPO_ROOT, create_app
from backend.api.state import WebAppState
from backend.domain import MessageQueueUnavailable
from backend.storage.message_queue import MemoryMessageQueue


class UnavailableMessageQueue(MemoryMessageQueue):
    def ping(self) -> None:
        raise MessageQueueUnavailable("message_queue_unavailable")

    def list(self, thread_id: str):
        del thread_id
        raise MessageQueueUnavailable("message_queue_unavailable")


def test_backend_repo_root_targets_the_current_checkout() -> None:
    assert REPO_ROOT == Path(__file__).resolve().parents[1]
    assert (REPO_ROOT / "frontend").is_dir()


def test_production_frontend_does_not_capture_unknown_api_routes(tmp_path: Path, monkeypatch) -> None:
    frontend_dist = tmp_path / "dist"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text("<!doctype html><title>Mini-Agent</title>", encoding="utf-8")
    monkeypatch.setenv("MINI_AGENT_FRONTEND_DIST", str(frontend_dist))

    state = WebAppState(tmp_path / ".mini_agent")
    with TestClient(create_app(state)) as client:
        assert client.get("/").status_code == 200
        assert client.post("/api/auth/guest").status_code == 404


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


def test_health_stays_live_while_ready_and_queue_mutations_fail_when_redis_is_unavailable(tmp_path: Path) -> None:
    state = WebAppState(tmp_path / ".mini_agent", message_queue=UnavailableMessageQueue())
    with TestClient(create_app(state)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/ready").status_code == 503
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        sidebar_list = client.get("/api/sidebar-threads")
        assert sidebar_list.status_code == 200
        assert sidebar_list.json()[0]["message_count"] == 0
        assert sidebar_list.json()[0]["conversation_updated_at"] == sidebar["created_at"]
        assert client.get(f"/api/sidebar-threads/{sidebar['thread_id']}/queued-messages").status_code == 503
        create_turn = client.post(
            "/api/turns",
            json={
                "id": "turn-no-redis",
                "session_id": sidebar["session_id"],
                "thread_id": sidebar["thread_id"],
                "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            },
        )
        assert create_turn.status_code == 503


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
        "max_tokens": 2048,
        "context_size": 65536,
        "temperature": 0.7,
        "api_key": "provider-secret",
    }
    with TestClient(create_app(state)) as client:
        response = client.put("/api/settings/providers", json=payload)
        assert response.status_code == 200
        assert "provider-secret" not in response.text
        assert "api_key" not in response.json()
        assert response.json()["max_tokens"] == 2048
        assert response.json()["context_size"] == 65536
        assert response.json()["temperature"] == 0.7

        patched = client.patch(
            f"/api/settings/providers/{response.json()['id']}",
            json={"max_tokens": 4096, "context_size": 131072, "temperature": 1.2},
        )
        assert patched.status_code == 200
        assert patched.json()["max_tokens"] == 4096
        assert patched.json()["context_size"] == 131072
        assert patched.json()["temperature"] == 1.2
        assert client.get("/api/settings").json()["provider_config"]["temperature"] == 1.2

        assert (
            client.patch(
                f"/api/settings/providers/{response.json()['id']}",
                json={"temperature": True},
            ).status_code
            == 422
        )
        assert (
            client.patch(
                f"/api/settings/providers/{response.json()['id']}",
                json={"max_tokens": 4096, "context_size": 4096},
            ).status_code
            == 422
        )

    with sqlite3.connect(root / "runtime" / "state.db") as connection:
        raw = str(connection.execute("SELECT provider_configs_json FROM provider_settings").fetchone()[0])
    assert "provider-secret" not in raw
    assert "v4:" in raw

    reopened = WebAppState(root)
    try:
        model_config = reopened.model_config("local-test")
        assert model_config.api_key == "provider-secret"
        assert model_config.max_tokens == 4096
        assert model_config.context_size == 131072
        assert model_config.temperature == 1.2
        assert "provider-secret" not in str(reopened.settings_payload())
    finally:
        reopened.close()
