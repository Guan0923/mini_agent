from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.configuration import ClientPaths
from fastapi.testclient import TestClient


class FakeMailer:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send_code(self, recipient: str, code: str, purpose: str) -> None:
        self.messages.append((recipient, code, purpose))


def _state(tmp_path: Path) -> tuple[WebAppState, FakeMailer]:
    mailer = FakeMailer()
    state = WebAppState(tmp_path / "web", mailer=mailer)
    state.paths = ClientPaths(tmp_path / "legacy-client")
    state.paths.ensure()
    state.config_path = state.paths.config_file
    return state, mailer


def _register(client: TestClient, mailer: FakeMailer, email: str, password: str) -> dict:
    assert client.post("/api/auth/register/code", json={"email": email}).status_code == 202
    code = mailer.messages[-1][1]
    response = client.post("/api/auth/register", json={"email": email, "code": code, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["user"]


def test_auth_cookie_protects_api_and_logout_revokes_it(tmp_path: Path) -> None:
    state, mailer = _state(tmp_path)
    with TestClient(create_app(state)) as client:
        assert client.get("/api/tools").status_code == 401
        assert client.post("/api/auth/register/code", json={"email": "Alice@example.com"}).status_code == 202
        code = mailer.messages[-1][1]
        registration = client.post(
            "/api/auth/register",
            json={"email": "Alice@example.com", "code": code, "password": "a" * 12},
        )
        assert registration.status_code == 200
        cookie_header = registration.headers["set-cookie"].lower()
        assert "httponly" in cookie_header
        assert "samesite=lax" in cookie_header
        assert "max-age=2592000" in cookie_header
        user = registration.json()["user"]
        cookie = client.cookies.get("mini_agent_session")
        assert cookie
        assert client.get("/api/auth/me").json()["id"] == user["id"]
        assert client.get("/api/tools").status_code == 200
        assert client.get("/api/skills").status_code == 200
        assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {cookie}"}).status_code == 401
        assert (
            client.post(
                "/api/chat", json={"prompt": "cross-site"}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert client.post("/api/auth/logout", json={}).status_code == 200
        assert client.get("/api/auth/me").status_code == 401


def test_password_reset_revokes_old_browser_session(tmp_path: Path) -> None:
    state, mailer = _state(tmp_path)
    app = create_app(state)
    with TestClient(app) as client:
        _register(client, mailer, "reset@example.com", "r" * 12)
        old_cookie = client.cookies.get("mini_agent_session")
        assert client.post("/api/auth/password-reset/code", json={"email": "reset@example.com"}).status_code == 202
        reset_code = mailer.messages[-1][1]
        response = client.post(
            "/api/auth/password-reset/confirm",
            json={"email": "reset@example.com", "code": reset_code, "password": "n" * 12},
        )
        assert response.status_code == 200
        assert old_cookie != client.cookies.get("mini_agent_session")
        assert client.post("/api/auth/logout", json={}).status_code == 200
        login = client.post("/api/auth/login", json={"email": "reset@example.com", "password": "n" * 12})
        assert login.status_code == 200


def test_verification_code_is_hashed_and_device_authorization_returns_bearer(tmp_path: Path) -> None:
    state, mailer = _state(tmp_path)
    app = create_app(state)
    with TestClient(app) as browser:
        user = _register(browser, mailer, "device@example.com", "d" * 12)
        code = mailer.messages[-1][1]
        with sqlite3.connect(state.auth.path) as connection:
            stored = connection.execute("SELECT code_hash FROM verification_challenges").fetchone()[0]
        assert code not in stored

        started = browser.post("/api/auth/device/start")
        assert started.status_code == 200
        payload = started.json()
        grant = payload["verification_url"].split("grant=", 1)[1]
        assert browser.post("/api/auth/device/approve", json={"grant": grant, "approved": True}).status_code == 200

        poll = browser.post("/api/auth/device/token", json={"poll_secret": payload["poll_secret"]})
        assert poll.status_code == 200
        token = poll.json()["access_token"]
        with TestClient(app) as cli:
            response = cli.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["id"] == user["id"]
        assert browser.post("/api/auth/device/token", json={"poll_secret": payload["poll_secret"]}).status_code == 400


def test_users_get_separate_storage_roots(tmp_path: Path) -> None:
    state, mailer = _state(tmp_path)
    app = create_app(state)
    with TestClient(app) as first, TestClient(app) as second:
        alice = _register(first, mailer, "alice@example.com", "a" * 12)
        first_cookie = first.cookies.get("mini_agent_session")
        bob = _register(second, mailer, "bob@example.com", "b" * 12)
        assert alice["id"] != bob["id"]
        assert state.user_paths(alice["id"]).root != state.user_paths(bob["id"]).root
        assert first_cookie
        assert second.get("/api/sessions").json() == []

def test_user_profile_is_persisted_and_isolated_between_accounts(tmp_path: Path) -> None:
    state, mailer = _state(tmp_path)
    app = create_app(state)
    with TestClient(app) as alice_client, TestClient(app) as bob_client:
        alice = _register(alice_client, mailer, "profile-alice@example.com", "a" * 12)
        bob = _register(bob_client, mailer, "profile-bob@example.com", "b" * 12)

        assert alice["display_name"] == ""
        assert alice_client.get("/api/auth/profile").json() == {
            "display_name": "",
            "agent_preferences": "",
        }
        response = alice_client.put(
            "/api/auth/profile",
            json={"display_name": " Alice ", "agent_preferences": "  concise answers  "},
        )
        assert response.status_code == 200
        assert response.json() == {"display_name": "Alice", "agent_preferences": "concise answers"}
        assert alice_client.get("/api/auth/me").json()["display_name"] == "Alice"
        assert bob_client.get("/api/auth/profile").json() == {
            "display_name": "",
            "agent_preferences": "",
        }
        assert alice["id"] != bob["id"]


def test_user_profile_rejects_oversized_fields_and_unauthenticated_access(tmp_path: Path) -> None:
    state, mailer = _state(tmp_path)
    with TestClient(create_app(state)) as client:
        assert client.get("/api/auth/profile").status_code == 401
        _register(client, mailer, "profile-validation@example.com", "v" * 12)
        assert client.put(
            "/api/auth/profile",
            json={"display_name": "Cross-site"},
            headers={"Origin": "https://evil.example"},
        ).status_code == 403
        assert client.put("/api/auth/profile", json={"display_name": "x" * 81}).status_code == 422
        assert client.put("/api/auth/profile", json={"agent_preferences": "x" * 4001}).status_code == 422


def test_agent_config_preserves_new_fields_for_partial_legacy_updates(tmp_path: Path) -> None:
    state, mailer = _state(tmp_path)
    with TestClient(create_app(state)) as client:
        _register(client, mailer, "agent-config@example.com", "c" * 12)
        configured = client.put(
            "/api/auth/agent-config",
            json={
                "tone": "direct",
                "verbosity": "concise",
                "initiative": "proactive",
                "custom_instructions": "Use concise bullets",
                "display_mode": "verbose",
                "timezone": "UTC",
                "location_enabled": True,
            },
        )
        assert configured.status_code == 200, configured.text

        legacy_update = client.put("/api/auth/agent-config", json={"tone": "balanced"})
        assert legacy_update.status_code == 200, legacy_update.text
        assert legacy_update.json() == {
            "tone": "balanced",
            "verbosity": "concise",
            "initiative": "proactive",
            "custom_instructions": "Use concise bullets",
            "display_mode": "verbose",
            "timezone": "UTC",
            "location_enabled": True,
        }
