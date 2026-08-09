from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import psycopg
import pytest
from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.storage.auth import AuthStore
from backend.storage.auth.crypto import SecretDecryptionError
from backend.storage.auth.types import AuthStorageUnavailable
from backend.storage.postgres.auth import PostgresAuthRepository
from backend.storage.postgres.auth_migration import apply_migration, check_migration
from backend.storage.postgres.settings import PostgresSettingsRepository
from fastapi.testclient import TestClient

_SECRET = "postgres-web-test-secret-key-material-123456789"


class FakeMailer:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send_code(self, recipient: str, code: str, purpose: str) -> None:
        self.messages.append((recipient, code, purpose))


def _database_url() -> str:
    return os.environ["TEST_DATABASE_URL"].replace("@localhost:", "@127.0.0.1:")


def _state(root: Path, mailer: FakeMailer) -> WebAppState:
    return WebAppState(root, mailer=mailer, database_url=_database_url(), secret_key=_SECRET)


def _register(client: TestClient, mailer: FakeMailer, email: str, password: str) -> dict:
    assert client.post("/api/auth/register/code", json={"email": email}).status_code == 202
    code = mailer.messages[-1][1]
    response = client.post("/api/auth/register", json={"email": email, "code": code, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["user"]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_postgres_registration_and_login_are_shared_between_server_instances(tmp_path: Path) -> None:
    first_mailer = FakeMailer()
    second_mailer = FakeMailer()
    first_state = _state(tmp_path / "first", first_mailer)
    second_state = _state(tmp_path / "second", second_mailer)

    with TestClient(create_app(first_state)) as first:
        user = _register(first, first_mailer, "shared@example.com", "a" * 12)
        assert user["legacy_owner"] is False
        assert (
            first.put("/api/auth/profile", json={"display_name": "Shared", "agent_preferences": "concise"}).status_code
            == 200
        )

    with TestClient(create_app(second_state)) as second:
        login = second.post("/api/auth/login", json={"email": "shared@example.com", "password": "a" * 12})
        assert login.status_code == 200
        assert login.json()["user"]["id"] == user["id"]
        assert (
            second.post(
                "/api/auth/login", json={"email": "shared@example.com", "password": "wrong-password"}
            ).status_code
            == 400
        )
        assert second.get("/api/auth/profile").json()["display_name"] == "Shared"
        old_cookie = second.cookies.get("mini_agent_session")
        assert second.post("/api/auth/logout", json={}).status_code == 200
        second.cookies.set("mini_agent_session", old_cookie)
        assert second.get("/api/auth/me").status_code == 401

    with psycopg.connect(_database_url()) as connection:
        row = connection.execute("SELECT password_hash FROM users WHERE email=%s", ("shared@example.com",)).fetchone()
        challenge = connection.execute("SELECT code_hash FROM verification_challenges LIMIT 1").fetchone()
    assert row and row[0] != "a" * 12
    assert challenge and first_mailer.messages[-1][1] not in str(challenge[0])


def test_postgres_enforces_unique_users_one_time_codes_and_rate_limits() -> None:
    auth = PostgresAuthRepository(_database_url())
    auth.insert_challenge("unique@example.com", "register", "123456", None)
    user = auth.register_user("unique@example.com", "123456", "u" * 12)
    assert auth.authenticate(user.email, "u" * 12) == user
    assert auth.authenticate(user.email, "incorrect") is None

    auth.insert_challenge(user.email, "register", "654321", None)
    with pytest.raises(ValueError, match="已注册"):
        auth.register_user(user.email, "654321", "v" * 12)

    auth.insert_challenge(user.email, "reset", "111111", None)
    auth.reset_password(user.email, "111111", "n" * 12)
    with pytest.raises(ValueError, match="验证码无效"):
        auth.reset_password(user.email, "111111", "z" * 12)

    assert auth.consume_limit("email:limit@example.com", "login:test", 2, 900) is True
    assert auth.consume_limit("email:limit@example.com", "login:test", 2, 900) is True
    assert auth.consume_limit("email:limit@example.com", "login:test", 2, 900) is False


def test_postgres_settings_are_isolated_and_readable_across_instances() -> None:
    auth = PostgresAuthRepository(_database_url())
    for email, code in (("alice-settings@example.com", "123456"), ("bob-settings@example.com", "654321")):
        auth.insert_challenge(email, "register", code, None)
        auth.register_user(email, code, "p" * 12)
    alice = auth.user_by_email("alice-settings@example.com")
    bob = auth.user_by_email("bob-settings@example.com")
    assert alice and bob

    first = PostgresSettingsRepository(_database_url(), secret_key=_SECRET)
    second = PostgresSettingsRepository(_database_url(), secret_key=_SECRET)
    first.update_profile(alice.id, display_name="Alice", agent_preferences="direct")
    first.update_agent_config(alice.id, {"display_mode": "verbose", "timezone": "UTC"})

    assert second.profile_for_user(alice.id)["display_name"] == "Alice"
    assert second.agent_config_for_user(alice.id)["display_mode"] == "verbose"
    assert second.profile_for_user(bob.id)["display_name"] == ""
    assert second.agent_config_for_user(bob.id)["display_mode"] == "medium"


def test_postgres_readiness_failure_returns_503_without_creating_auth_sqlite(tmp_path: Path) -> None:
    state = _state(tmp_path / "runtime", FakeMailer())

    def unavailable() -> None:
        raise AuthStorageUnavailable("offline")

    state.auth.ping = unavailable  # type: ignore[method-assign]
    with TestClient(create_app(state)) as client:
        response = client.get("/api/ready")
    assert response.status_code == 503
    assert not list(tmp_path.rglob("auth.sqlite3"))


def test_postgres_reset_and_device_authorization_are_cross_instance(tmp_path: Path) -> None:
    mailer = FakeMailer()
    first_state = _state(tmp_path / "first", mailer)
    second_state = _state(tmp_path / "second", mailer)
    with TestClient(create_app(first_state)) as first, TestClient(create_app(second_state)) as second:
        user = _register(first, mailer, "device@example.com", "d" * 12)
        old_cookie = first.cookies.get("mini_agent_session")

        started = second.post("/api/auth/device/start", json={}).json()
        grant = started["verification_url"].split("grant=", 1)[1]
        assert first.post("/api/auth/device/approve", json={"grant": grant, "approved": True}).status_code == 200
        polled = second.post("/api/auth/device/token", json={"poll_secret": started["poll_secret"]})
        assert polled.status_code == 200
        assert (
            second.get("/api/auth/me", headers={"Authorization": f"Bearer {polled.json()['access_token']}"}).json()[
                "id"
            ]
            == user["id"]
        )

        assert first.post("/api/auth/password-reset/code", json={"email": user["email"]}).status_code == 202
        code = mailer.messages[-1][1]
        reset = first.post(
            "/api/auth/password-reset/confirm",
            json={"email": user["email"], "code": code, "password": "n" * 12},
        )
        assert reset.status_code == 200
        with TestClient(create_app(second_state)) as old_browser:
            old_browser.cookies.set("mini_agent_session", old_cookie)
            assert old_browser.get("/api/auth/me").status_code == 401


def test_postgres_provider_keys_use_authenticated_server_encryption() -> None:
    auth = PostgresAuthRepository(_database_url())
    auth.insert_challenge("keys@example.com", "register", "123456", None)
    user = auth.register_user("keys@example.com", "123456", "p" * 12)
    settings = PostgresSettingsRepository(_database_url(), secret_key=_SECRET)
    settings.update_provider_config(
        user.id,
        {
            "provider": "deepseek",
            "protocol": "chat_completions",
            "base_url": "https://api.example.com",
            "model": "model",
            "max_tokens": 1024,
            "context_size": 4096,
            "tokenizer_model": "tokenizer",
            "api_key": "provider-secret",
        },
    )
    with psycopg.connect(_database_url()) as connection:
        ciphertext = connection.execute(
            "SELECT api_key_ciphertext FROM user_provider_settings WHERE user_id=%s", (user.id,)
        ).fetchone()[0]
    assert str(ciphertext).startswith("v2:")
    assert "provider-secret" not in str(ciphertext)
    assert settings.model_config_for_user(user.id).api_key == "provider-secret"

    wrong_key = PostgresSettingsRepository(_database_url(), secret_key="x" * 40)
    with pytest.raises(SecretDecryptionError):
        wrong_key.model_config_for_user(user.id)


def test_sqlite_migration_preserves_accounts_and_clears_sessions_and_provider_key(tmp_path: Path) -> None:
    source = tmp_path / "auth.sqlite3"
    legacy = AuthStore(source)
    legacy.insert_challenge("migrate@example.com", "register", "123456", None)
    user = legacy.register_user("migrate@example.com", "123456", "m" * 12)
    legacy.update_profile(user.id, display_name="Migrated", agent_preferences="brief")
    legacy.update_agent_config(user.id, {"display_mode": "verbose", "timezone": "UTC"})
    legacy.update_provider_config(
        user.id,
        {
            "provider": "deepseek",
            "base_url": "https://api.example.com",
            "model": "model",
            "api_key": "must-not-migrate",
        },
    )
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO user_capability_settings(user_id,settings_json,updated_at) VALUES (?,?,?)",
            (user.id, json.dumps({"web_search": True}), 1.0),
        )
    old_token = legacy.create_session(user.id, "browser")
    legacy.set_metadata(f"legacy_migration:{user.id}", "complete")
    before = _digest(source)

    auth = PostgresAuthRepository(_database_url())
    settings = PostgresSettingsRepository(_database_url(), secret_key=_SECRET)
    assert check_migration(source, auth)["status"] == "ready"
    result = apply_migration(source, auth)
    assert result["status"] == "applied"
    assert apply_migration(source, auth)["status"] == "already_applied"
    assert _digest(source) == before

    migrated = auth.authenticate("migrate@example.com", "m" * 12)
    assert migrated and migrated.id == user.id and migrated.legacy_owner is True
    assert auth.resolve_token(old_token) is None
    assert settings.profile_for_user(user.id)["display_name"] == "Migrated"
    assert settings.agent_config_for_user(user.id)["display_mode"] == "verbose"
    assert settings.provider_config_for_user(user.id)["api_key_configured"] is False
    assert settings.capability_config_for_user(user.id) == {"web_search": True}
    assert auth.metadata(f"legacy_migration:{user.id}") == "complete"


def test_sqlite_migration_conflict_rolls_back_without_record(tmp_path: Path) -> None:
    source = tmp_path / "auth.sqlite3"
    legacy = AuthStore(source)
    legacy.insert_challenge("conflict@example.com", "register", "123456", None)
    legacy.register_user("conflict@example.com", "123456", "c" * 12)

    auth = PostgresAuthRepository(_database_url())
    auth.insert_challenge("conflict@example.com", "register", "654321", None)
    auth.register_user("conflict@example.com", "654321", "d" * 12)
    with pytest.raises(ValueError, match="conflicting user"):
        apply_migration(source, auth)
    with auth.connection() as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM web_auth_data_migrations").fetchone()["count"] == 0
