from __future__ import annotations

from pathlib import Path

from backend.api.auth_types import UserIdentity
from backend.api.user_data import migrate_legacy_for_owner, user_root
from backend.configuration import ClientPaths


def test_legacy_migration_copies_sessions_logs_and_workspace(tmp_path: Path) -> None:
    data_root = tmp_path / "web"
    source_paths = ClientPaths(tmp_path / "legacy")
    source_paths.ensure()
    (source_paths.root / "session_legacy" / "state.db").parent.mkdir()
    (source_paths.root / "session_legacy" / "state.db").write_bytes(b"state")
    (source_paths.logs_dir / "run.jsonl").write_text('{"kind":"run_finished"}\n', encoding="utf-8")
    source_workspace = tmp_path / "legacy-workspace"
    source_workspace.mkdir()
    (source_workspace / "notes.txt").write_text("legacy notes", encoding="utf-8")
    identity = UserIdentity("user-1", "legacy@example.com", legacy_owner=True)
    statuses: list[str] = []

    migrate_legacy_for_owner(
        data_root,
        identity,
        source_paths,
        source_workspace,
        status=None,
        set_status=statuses.append,
    )

    target_root = user_root(data_root, identity.id)
    assert (target_root / "client" / "session_legacy" / "state.db").read_bytes() == b"state"
    assert (target_root / "client" / "logs" / "run.jsonl").read_text(encoding="utf-8")
    assert (target_root / "workspace" / "notes.txt").read_text(encoding="utf-8") == "legacy notes"
    assert statuses == ["pending", "complete"]
