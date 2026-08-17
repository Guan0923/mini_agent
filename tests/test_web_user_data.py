from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.user_data import copy_session_files, user_paths, user_root, user_workspace
from backend.configuration import ClientPaths, ConfigurationError

USER_ID = "123e4567-e89b-12d3-a456-426614174000"


def test_user_layout_is_canonical_and_rejects_non_uuid_ids(tmp_path: Path) -> None:
    paths = user_paths(tmp_path, USER_ID)
    assert paths.root == tmp_path / USER_ID
    assert {item.relative_to(paths.root).as_posix() for item in paths.root.iterdir() if item.is_dir()} == {
        "skills",
        "rag",
        "plugins",
        "mcp",
        "runtime",
        "sync",
    }
    assert paths.mcp_resources_dir.is_dir()
    assert paths.sync_staging_dir.is_dir()
    assert paths.sync_recovery_dir.is_dir()
    assert paths.config_file.is_file()
    assert paths.user_db.is_file()
    assert paths.mcp_file.is_file()
    assert paths.mcp_trust_file.is_file()
    assert not (paths.runtime_dir / "web").exists()
    assert not (paths.runtime_dir / "tui").exists()

    with pytest.raises(ConfigurationError):
        user_root(tmp_path, "user@example.com")
    with pytest.raises(ConfigurationError):
        user_root(tmp_path, "../outside")


def test_each_session_has_an_independent_workspace_and_branch_copy(tmp_path: Path) -> None:
    source = user_workspace(tmp_path, USER_ID, "session_source")
    (source / "notes.txt").write_text("source", encoding="utf-8")
    source_paths = ClientPaths(user_root(tmp_path, USER_ID))
    (source_paths.session_uploads("session_source") / "input.txt").write_text("upload", encoding="utf-8")

    copy_session_files(tmp_path, USER_ID, "session_source", "session_branch")
    branch = user_workspace(tmp_path, USER_ID, "session_branch")
    assert (branch / "notes.txt").read_text(encoding="utf-8") == "source"
    assert (source_paths.session_uploads("session_branch") / "input.txt").read_text(encoding="utf-8") == "upload"

    (branch / "notes.txt").write_text("branch", encoding="utf-8")
    assert (source / "notes.txt").read_text(encoding="utf-8") == "source"


def test_legacy_uploads_migrate_into_workspace(tmp_path: Path) -> None:
    paths = ClientPaths(user_root(tmp_path, USER_ID))
    root = paths.session_root("session_migrate")
    root.mkdir(parents=True)
    (root / "workspace").mkdir()
    legacy = root / "uploads"
    legacy.mkdir()
    (legacy / "old.txt").write_text("legacy", encoding="utf-8")
    paths.ensure_session("session_migrate")
    assert (paths.session_uploads("session_migrate") / "old.txt").read_text(encoding="utf-8") == "legacy"
    assert not (root / "uploads").exists()


def test_branch_copy_migrates_legacy_uploads_first(tmp_path: Path) -> None:
    paths = ClientPaths(user_root(tmp_path, USER_ID))
    source_root = paths.session_root("session_legacy_source")
    source_root.mkdir(parents=True)
    (source_root / "workspace").mkdir()
    legacy = source_root / "uploads"
    legacy.mkdir()
    (legacy / "old.txt").write_text("legacy", encoding="utf-8")
    (source_root / "state.db").touch()

    copy_session_files(tmp_path, USER_ID, "session_legacy_source", "session_legacy_branch")
    assert (
        paths.session_uploads("session_legacy_branch") / "old.txt"
    ).read_text(encoding="utf-8") == "legacy"
