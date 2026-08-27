from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.configuration import ClientPaths, initialize_config, load_config
from backend.runtime import RuntimeState
from backend.storage.sqlite import SQLiteSessionStore


def test_fresh_home_uses_exact_five_item_layout_and_ignores_legacy_env(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = workspace / ".env"
    legacy.write_text("API_KEY=secret\nBASE_URL=https://model.test\nMODEL=demo\n", encoding="utf-8")
    home = tmp_path / "home"
    paths = ClientPaths.from_home(home)

    initialize_config(paths, workspace)

    assert legacy.exists()
    assert {item.name for item in paths.root.iterdir()} == {"mcp", "plugins", "runtime", "skills", "config.toml"}
    assert {item.name for item in paths.runtime_dir.iterdir()} == {"state.db", "projects.db"}
    assert "model" not in load_config(paths.config_file)
    assert not (paths.root / "sync").exists()
    assert not (paths.root / "user.db").exists()
    assert not (paths.root / "projects.db").exists()
    assert not (home / ".mini_agent-cache").exists()


def test_sqlite_persists_empty_runtime_and_time_zone_locally(tmp_path: Path) -> None:
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini_agent"))
    session = store.create_session("empty")
    state = RuntimeState(session_id=session.session_id, timezone="UTC")

    store.save_runtime(state)

    assert store.load_runtime(session.session_id).timezone == "UTC"


def test_sqlite_closes_connection_when_schema_initialization_fails(tmp_path: Path, monkeypatch) -> None:
    class BrokenConnection:
        def __init__(self) -> None:
            self.row_factory = None
            self.rolled_back = False
            self.closed = False

        def execute(self, _statement: str) -> None:
            return None

        def executescript(self, _script: str) -> None:
            raise sqlite3.DatabaseError("broken schema")

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(sqlite3, "connect", lambda _path: connection)
    store = SQLiteSessionStore(ClientPaths(tmp_path / "mini_agent"))

    with pytest.raises(sqlite3.DatabaseError, match="broken schema"):
        with store._connection("session_broken"):
            pass

    assert connection.rolled_back is True
    assert connection.closed is True
