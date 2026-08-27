"""Safe copy helpers for local session workspaces and uploads."""

from __future__ import annotations

import shutil
from pathlib import Path

from backend.configuration import ClientPaths


def copy_session_files(paths: ClientPaths, source_session_id: str, target_session_id: str) -> None:
    """Copy the source session workspace into a new independent session."""

    if source_session_id == target_session_id:
        raise ValueError("Source and target sessions must be different.")
    source_root = paths.session_root(source_session_id)
    target_root = paths.session_root(target_session_id)
    if source_root.is_symlink() or target_root.is_symlink():
        raise ValueError("Session data cannot be a symbolic link.")
    if not source_root.is_dir() or not paths.session_db(source_session_id).is_file():
        raise ValueError("Source session does not exist.")
    target_existed = target_root.exists()
    paths.ensure_session(target_session_id)
    try:
        _copy_tree_without_symlinks(
            paths.session_workspace(source_session_id), paths.session_workspace(target_session_id)
        )
    except Exception:
        if not target_existed:
            shutil.rmtree(target_root, ignore_errors=True)
        raise


def copy_session_uploads(paths: ClientPaths, source_session_id: str, target_session_id: str) -> None:
    """Copy durable uploads when two sessions share an external project cwd."""

    if source_session_id == target_session_id:
        raise ValueError("Source and target sessions must be different.")
    source_root = paths.session_root(source_session_id)
    target_root = paths.session_root(target_session_id)
    if source_root.is_symlink() or target_root.is_symlink():
        raise ValueError("Session data cannot be a symbolic link.")
    if not source_root.is_dir() or not paths.session_db(source_session_id).is_file():
        raise ValueError("Source session does not exist.")
    target_existed = target_root.exists()
    paths.ensure_session(target_session_id)
    try:
        _copy_tree_without_symlinks(paths.session_uploads(source_session_id), paths.session_uploads(target_session_id))
    except Exception:
        if not target_existed:
            shutil.rmtree(target_root, ignore_errors=True)
        raise


def _copy_tree_without_symlinks(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise ValueError(f"Refusing to copy symbolic-link directory: {source}")
    if not source.is_dir():
        raise ValueError(f"Refusing to copy a missing directory: {source}")
    if target.is_symlink():
        raise ValueError(f"Refusing to copy into symbolic-link directory: {target}")
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if item.is_symlink():
            raise ValueError(f"Refusing to copy symbolic link: {item}")
        destination = target / relative
        if destination.is_symlink():
            raise ValueError(f"Refusing to copy into symbolic link: {destination}")
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
        else:
            raise ValueError(f"Refusing to copy special file: {item}")


__all__ = ["copy_session_files", "copy_session_uploads"]
