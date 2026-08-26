from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.auth.service import WebAuthSettings
from backend.api.state import WebAppState
from backend.cloud import CloudUnavailable
from backend.configuration import ClientPaths
from backend.storage.auth.types import UserIdentity


def _state(tmp_path: Path, *, cloud_client=None) -> WebAppState:
    return WebAppState(tmp_path / "web", cloud_client=cloud_client)


def test_default_web_state_is_local_and_does_not_require_postgres(tmp_path: Path) -> None:
    legacy_root = tmp_path / "web"
    legacy_root.mkdir(parents=True)
    (legacy_root / "mcp").mkdir()
    (legacy_root / "skills").mkdir()
    (legacy_root / "runtime").mkdir()
    (legacy_root / "config.toml").write_text("[model]\nmodel = 'legacy'\n", encoding="utf-8")
    (legacy_root / "client.db").write_bytes(b"legacy")

    state = _state(tmp_path)
    assert state.auth.path.name == "client.db"
    assert state.auth.path == tmp_path / ".mini_agent-cache" / "auth" / "client.db"
    assert state.auth.path.is_file()
    assert not list(tmp_path.rglob("auth.sqlite3"))
    assert (legacy_root / "config.toml").exists()
    assert (legacy_root / "client.db").read_bytes() == b"legacy"
    assert (legacy_root / "mcp").is_dir()
    assert (legacy_root / "skills").is_dir()
    assert (legacy_root / "runtime").is_dir()


def test_client_paths_from_home_uses_legacy_cache_outside_web_root(tmp_path: Path) -> None:
    paths = ClientPaths.from_home(tmp_path)

    assert paths.root == tmp_path / ".mini_agent-cache" / "tui"
    assert tmp_path / ".mini_agent" != paths.root


def test_guest_login_is_fully_offline_and_reuses_cookie(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with TestClient(create_app(state)) as client:
        first = client.post("/api/auth/guest")
        assert first.status_code == 200, first.text
        user = first.json()["user"]
        assert user["kind"] == "guest"
        assert user["display_name"] == "游客用户"
        assert state.user_paths(user["id"]).user_db.is_file()
        assert client.get("/api/auth/me").json()["id"] == user["id"]
        assert client.get("/api/auth/me").json()["display_name"] == "游客用户"

        second = client.post("/api/auth/guest")
        assert second.status_code == 200
        assert second.json()["user"]["id"] == user["id"]

        assert client.post("/api/auth/logout", json={}).status_code == 200
        assert client.get("/api/auth/me").status_code == 401

        after_logout = client.post("/api/auth/guest")
        assert after_logout.status_code == 200
        assert after_logout.json()["user"]["id"] == user["id"]

    with sqlite3.connect(state.auth.path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(local_sessions)")}
        assert "token_hash" in columns
        assert "token" not in columns
        assert connection.execute("SELECT COUNT(*) FROM local_identities WHERE kind='guest'").fetchone()[0] == 1
        assert connection.execute("SELECT guest_id FROM local_device_state WHERE id=1").fetchone()[0] == user["id"]


def test_guest_identity_is_shared_by_independent_browser_sessions(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with TestClient(create_app(state)) as first, TestClient(create_app(state)) as second:
        first_user = first.post("/api/auth/guest").json()["user"]
        second_user = second.post("/api/auth/guest").json()["user"]

        assert second_user["id"] == first_user["id"]
        assert first.cookies.get("mini_agent_session") != second.cookies.get("mini_agent_session")


def test_guest_provisioning_failure_removes_new_canonical_identity(tmp_path: Path, monkeypatch) -> None:
    state = _state(tmp_path)

    def fail_user_paths(_user_id: str):
        raise OSError("read-only")

    monkeypatch.setattr(state, "user_paths", fail_user_paths)
    with TestClient(create_app(state)) as client:
        response = client.post("/api/auth/guest")
    assert response.status_code == 503
    with sqlite3.connect(state.auth.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_identities WHERE kind='guest'").fetchone()[0] == 0
        assert connection.execute("SELECT guest_id FROM local_device_state WHERE id=1").fetchone()[0] is None


def test_legacy_multiple_guests_choose_the_oldest_canonical_identity(tmp_path: Path) -> None:
    state = _state(tmp_path)
    older = "11111111-1111-4111-8111-111111111111"
    newer = "22222222-2222-4222-8222-222222222222"
    with sqlite3.connect(state.auth.path) as connection:
        connection.executemany(
            "INSERT INTO local_identities(id,email,kind,created_at,updated_at) VALUES (?,?,?, ?, ?)",
            [(older, None, "guest", 1.0, 1.0), (newer, None, "guest", 2.0, 2.0)],
        )

    identity, created = state.auth.get_or_create_guest()

    assert identity.id == older
    assert created is False
    with sqlite3.connect(state.auth.path) as connection:
        assert connection.execute("SELECT guest_id FROM local_device_state WHERE id=1").fetchone()[0] == older


def test_account_profile_defaults_to_email_and_blank_name_is_rejected(tmp_path: Path) -> None:
    state = _state(tmp_path)
    identity = UserIdentity("11111111-1111-4111-8111-111111111111", "user@example.com", "account")
    state.auth.upsert_identity(identity)
    token = state.auth.create_session(identity.id, "browser")
    with TestClient(create_app(state)) as client:
        client.cookies.set("mini_agent_session", token)
        response = client.get("/api/auth/me")
        assert response.status_code == 200
        assert response.json()["display_name"] == "user@example.com"
        blank = client.put("/api/auth/profile", json={"display_name": "  ", "agent_preferences": ""})
        assert blank.status_code == 422


def test_account_operations_report_cloud_unavailable_without_clearing_local_state(tmp_path: Path) -> None:
    class OfflineCloud:
        base_url = "http://127.0.0.1:8100"

        def register_code(self, _email: str) -> None:
            raise CloudUnavailable("cloud offline", retryable=True)

    state = _state(tmp_path, cloud_client=OfflineCloud())
    with TestClient(create_app(state)) as client:
        response = client.post("/api/auth/register/code", json={"email": "user@example.com"})
        assert response.status_code == 503
        assert "offline" in response.json()["detail"]
        guest = client.post("/api/auth/guest")
        assert guest.status_code == 200


def test_account_password_operations_never_fall_back_to_local_storage(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with TestClient(create_app(state)) as client:
        assert (
            client.post(
                "/api/auth/register",
                json={"email": "a@example.com", "code": "123456", "password": "p" * 12},
            ).status_code
            == 503
        )
        assert client.post("/api/auth/login", json={"email": "a@example.com", "password": "p" * 12}).status_code == 503
        assert (
            client.post(
                "/api/auth/password-reset/confirm",
                json={"email": "a@example.com", "code": "123456", "password": "p" * 12},
            ).status_code
            == 503
        )


def test_readiness_only_checks_local_components(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with TestClient(create_app(state)) as client:
        response = client.get("/api/ready")
    assert response.status_code == 200
    assert response.json()["service"] == "mini-agent-backend"


def test_production_cookie_and_cors_are_scoped_to_the_frontend_origin(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.auth_service.settings = WebAuthSettings(
        public_url="https://app.example.com",
        allowed_origins=("https://app.example.com",),
        cookie_secure=True,
    )
    with TestClient(create_app(state), base_url="https://api.example.com") as client:
        preflight = client.options(
            "/api/auth/login",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "https://app.example.com"
        assert preflight.headers["access-control-allow-credentials"] == "true"


def test_web_routes_keep_the_stable_local_contract(tmp_path: Path) -> None:
    state = _state(tmp_path)
    routes = set(create_app(state).openapi()["paths"])
    assert "/api/turns" in routes
    assert "/api/sidebar-threads" in routes
    assert "/api/sync/status" in routes
    assert "/api/ready" in routes
    assert "/api/chat" not in routes
    assert "/api/chat/batch" not in routes
    assert "/api/sessions" not in routes


def test_runtime_config_defaults_validates_and_persists_per_user(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with TestClient(create_app(state)) as client:
        login = client.post("/api/auth/guest")
        assert login.status_code == 200
        settings = client.get("/api/auth/settings").json()
        assert settings["runtime_config"] == {
            "max_tool_calls": 32,
            "terminal_type": "cmd",
        }
        assert {option["value"] for option in settings["terminal_options"]} >= {"cmd"}
        assert settings["terminal_notice"] is None

        saved = client.put("/api/auth/runtime-config", json={"max_tool_calls": 1000})
        assert saved.status_code == 200
        assert saved.json() == {"max_tool_calls": 1000, "terminal_type": "cmd"}
        assert client.get("/api/auth/settings").json()["runtime_config"] == {
            "max_tool_calls": 1000,
            "terminal_type": "cmd",
        }

        for value in (0, 1001, True, "32"):
            response = client.put("/api/auth/runtime-config", json={"max_tool_calls": value})
            assert response.status_code == 422


def test_runtime_config_accepts_each_detected_terminal(tmp_path: Path, monkeypatch) -> None:
    detected = {
        "cmd": "cmd.exe",
        "git_bash": "bash.exe",
        "powershell": "powershell.exe",
        "pwsh": "pwsh.exe",
        "wsl": "wsl.exe",
    }
    monkeypatch.setattr("backend.api.auth.routes.available_terminal_executables", lambda **_kwargs: detected)
    state = _state(tmp_path)
    with TestClient(create_app(state)) as client:
        assert client.post("/api/auth/guest").status_code == 200
        for terminal_type in detected:
            response = client.put("/api/auth/runtime-config", json={"terminal_type": terminal_type})
            assert response.status_code == 200, response.text
            assert response.json()["terminal_type"] == terminal_type


def test_runtime_config_rejects_unavailable_terminal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.api.auth.routes.available_terminal_executables",
        lambda **_kwargs: {"cmd": "cmd.exe"},
    )
    state = _state(tmp_path)
    with TestClient(create_app(state)) as client:
        assert client.post("/api/auth/guest").status_code == 200
        response = client.put("/api/auth/runtime-config", json={"terminal_type": "pwsh"})
        assert response.status_code == 422
        assert "not available" in response.json()["detail"]
