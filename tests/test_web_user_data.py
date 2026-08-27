from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.user_data import copy_session_files
from backend.configuration import ClientPaths, ConfigurationError


def test_local_layout_is_canonical_and_rejects_unsafe_session_ids(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / ".mini_agent")
    paths.ensure()

    assert {item.name for item in paths.root.iterdir()} == {"mcp", "plugins", "runtime", "skills", "config.toml"}
    assert paths.state_db.is_file()
    assert paths.projects_db.is_file()
    assert paths.mcp_file.is_file()
    assert paths.mcp_trust_file.is_file()
    with pytest.raises(ConfigurationError):
        paths.session_root("../outside")


def test_each_session_has_an_independent_workspace_and_branch_copy(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / ".mini_agent")
    paths.ensure_session("session_source")
    source = paths.session_workspace("session_source")
    (source / "notes.txt").write_text("source", encoding="utf-8")
    (paths.session_uploads("session_source") / "input.txt").write_text("upload", encoding="utf-8")

    copy_session_files(paths, "session_source", "session_branch")

    branch = paths.session_workspace("session_branch")
    assert (branch / "notes.txt").read_text(encoding="utf-8") == "source"
    assert (paths.session_uploads("session_branch") / "input.txt").read_text(encoding="utf-8") == "upload"
    (branch / "notes.txt").write_text("branch", encoding="utf-8")
    assert (source / "notes.txt").read_text(encoding="utf-8") == "source"


def test_old_sibling_uploads_are_not_migrated(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / ".mini_agent")
    root = paths.session_root("session_migrate")
    root.mkdir(parents=True)
    (root / "workspace").mkdir()
    legacy = root / "uploads"
    legacy.mkdir()
    (legacy / "old.txt").write_text("legacy", encoding="utf-8")

    paths.ensure_session("session_migrate")

    assert not (paths.session_uploads("session_migrate") / "old.txt").exists()
    assert (legacy / "old.txt").read_text(encoding="utf-8") == "legacy"


def test_branch_copy_ignores_old_sibling_uploads(tmp_path: Path) -> None:
    paths = ClientPaths(tmp_path / ".mini_agent")
    source_root = paths.session_root("session_legacy_source")
    source_root.mkdir(parents=True)
    (source_root / "workspace").mkdir()
    legacy = source_root / "uploads"
    legacy.mkdir()
    (legacy / "old.txt").write_text("legacy", encoding="utf-8")
    (source_root / "state.db").touch()

    copy_session_files(paths, "session_legacy_source", "session_legacy_branch")

    assert not (paths.session_uploads("session_legacy_branch") / "old.txt").exists()
    assert (legacy / "old.txt").read_text(encoding="utf-8") == "legacy"
