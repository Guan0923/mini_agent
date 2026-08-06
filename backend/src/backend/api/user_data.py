"""Per-user runtime roots and the non-destructive legacy Web migration."""

from __future__ import annotations

import shutil
from pathlib import Path

from backend.configuration import ClientPaths

from .auth_types import UserIdentity


def user_root(data_root: Path, user_id: str) -> Path:
    return data_root / user_id


def user_paths(data_root: Path, user_id: str, source_config: Path | None = None) -> ClientPaths:
    del source_config
    paths = ClientPaths(user_root(data_root, user_id))
    paths.ensure()
    return paths


def user_workspace(data_root: Path, user_id: str) -> Path:
    path = user_root(data_root, user_id) / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_benchmark_root(data_root: Path, user_id: str) -> Path:
    path = user_root(data_root, user_id) / "benchmark"
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy_for_owner(
    data_root: Path,
    identity: UserIdentity,
    source_paths: ClientPaths,
    source_workspace: Path,
    *,
    status: str | None,
    set_status,
) -> None:
    """Copy old single-user Web data once, preserving every original file."""
    if not identity.legacy_owner or status == "complete":
        return
    target_paths = user_paths(data_root, identity.id)
    # Keep the legacy client/ mirror for old tooling while the runtime reads the
    # canonical user root. Both copies are intentionally idempotent.
    legacy_root = target_paths.root / "client"
    legacy_root.mkdir(parents=True, exist_ok=True)
    legacy_logs = legacy_root / "logs"
    set_status("pending")
    try:
        for source in source_paths.root.glob("session_*"):
            if source.is_dir() and (source / "state.db").exists():
                shutil.copytree(source, target_paths.root / source.name, dirs_exist_ok=True)
                shutil.copytree(source, legacy_root / source.name, dirs_exist_ok=True)
        if source_paths.logs_dir.exists():
            shutil.copytree(source_paths.logs_dir, target_paths.logs_dir, dirs_exist_ok=True)
            shutil.copytree(source_paths.logs_dir, legacy_logs, dirs_exist_ok=True)
        if source_workspace.exists():
            shutil.copytree(source_workspace, user_workspace(data_root, identity.id), dirs_exist_ok=True)
    except OSError:
        set_status("failed")
        raise
    set_status("complete")