"""Unified per-user runtime roots shared by Web and TUI."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

from backend.configuration import ClientPaths, UserConfigStore, validate_identity_id


class UserDataUnavailable(RuntimeError):
    """The canonical local user-data root cannot be prepared or used."""


def ensure_data_root_access(data_root: Path) -> None:
    """Validate and probe the canonical data root before serving requests."""

    base = Path(data_root)
    if base.is_symlink():
        raise UserDataUnavailable("用户数据根目录不能是符号链接。")
    try:
        if base.exists() and not base.is_dir():
            raise UserDataUnavailable("用户数据根目录必须是目录。")
        base.mkdir(parents=True, exist_ok=True)
        probe = base / f".write-probe-{uuid4().hex}"
        descriptor = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        probe.unlink()
    except UserDataUnavailable:
        raise
    except OSError as exc:
        raise UserDataUnavailable("用户数据根目录不可写，请检查服务进程权限。") from exc


def remove_user_root(data_root: Path, user_id: str) -> None:
    """Remove one validated user root after a failed first-time provision."""

    root = user_root(data_root, user_id)
    if root.is_symlink():
        raise UserDataUnavailable("用户数据路径不能是符号链接。")
    if root.exists():
        if not root.is_dir():
            raise UserDataUnavailable("用户数据路径必须是目录。")
        shutil.rmtree(root)


def user_root(data_root: Path, user_id: str, *, require_uuid: bool = True) -> Path:
    validate_identity_id(user_id, require_uuid=require_uuid)
    base = Path(data_root)
    if base.is_symlink():
        raise ValueError("User data root cannot be a symbolic link.")
    root = base.resolve()
    candidate = root / user_id
    if candidate.is_symlink():
        raise ValueError("User data path cannot be a symbolic link.")
    candidate = candidate.resolve()
    if candidate.parent != root:
        raise ValueError("User data path must remain inside the data root.")
    return candidate


def user_paths(data_root: Path, user_id: str, source_config: Path | None = None) -> ClientPaths:
    del source_config
    try:
        paths = ClientPaths(user_root(data_root, user_id))
        paths.ensure()
        UserConfigStore(paths.config_file).ensure_defaults(
            {
                "profile": {"display_name": "", "agent_preferences": ""},
                "agent": {
                    "tone": "balanced",
                    "verbosity": "balanced",
                    "initiative": "balanced",
                    "custom_instructions": "",
                    "display_mode": "medium",
                    "timezone": "Asia/Shanghai",
                    "location_enabled": False,
                },
                "runtime": {"log_full_messages": True},
                "capabilities": {"skills": True, "rag": False, "plugins": False, "mcp": False},
                "providers": {"active_id": ""},
                "sync": {
                    "auto_save_enabled": False,
                    "auto_save_rule": "idle_5m",
                    "device_id": f"web_{user_id}",
                },
            }
        )
        # ``user.db`` is part of the identity's contract even before the first
        # provider or sync mutation.  Constructing the store creates its schema
        # without duplicating settings in the TOML file.
        from backend.storage.user_settings import UserSettingsStore

        UserSettingsStore(paths.user_db)
        return paths
    except UserDataUnavailable:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise UserDataUnavailable("本地用户数据目录暂不可用。") from exc


def user_workspace(data_root: Path, user_id: str, session_id: str) -> Path:
    paths = user_paths(data_root, user_id)
    paths.ensure_session(session_id)
    return paths.session_workspace(session_id)


def copy_session_files(data_root: Path, user_id: str, source_session_id: str, target_session_id: str) -> None:
    """Copy the source session's current files into a new independent session."""

    if source_session_id == target_session_id:
        raise ValueError("Source and target sessions must be different.")
    paths = user_paths(data_root, user_id)
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
        _copy_tree_without_symlinks(paths.session_uploads(source_session_id), paths.session_uploads(target_session_id))
    except Exception:
        # Branch callers create a fresh target session before copying.  If a
        # payload contains a link/special file or the disk fills up, remove
        # that newly-created shell so no half-copied session is discoverable.
        # An explicitly pre-existing target is left intact for callers that
        # intentionally use this helper as an overwrite operation.
        if not target_existed:
            shutil.rmtree(target_root, ignore_errors=True)
        raise


def copy_session_uploads(data_root: Path, user_id: str, source_session_id: str, target_session_id: str) -> None:
    """Copy only durable uploads when sessions share an external project cwd."""

    if source_session_id == target_session_id:
        raise ValueError("Source and target sessions must be different.")
    paths = user_paths(data_root, user_id)
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


def import_guest_sessions(data_root: Path, source_user_id: str, target_user_id: str) -> dict[str, object]:
    """Copy idle guest sessions into an account without importing settings."""

    source_paths = user_paths(data_root, source_user_id)
    target_paths = user_paths(data_root, target_user_id)
    from backend.storage.projects import ProjectStore

    source_projects = ProjectStore(source_paths.projects_db)
    target_projects = ProjectStore(target_paths.projects_db)
    source_sessions: list[Path] = []
    for item in source_paths.runtime_dir.iterdir():
        if item.is_symlink() or not item.is_dir():
            raise RuntimeError(f"游客 runtime 包含不支持的条目: {item.name}")
        state_db = item / "state.db"
        if state_db.is_symlink() or not state_db.is_file():
            raise RuntimeError(f"游客会话缺少有效 state.db: {item.name}")
        source_sessions.append(item)
    for session in source_sessions:
        try:
            source_paths.session_root(session.name)
        except ValueError as exc:
            raise RuntimeError(f"游客会话目录名不安全: {session.name}") from exc
    for session in source_sessions:
        connection = None
        try:
            connection = sqlite3.connect(session / "state.db")
            if _session_has_active_run(connection):
                raise RuntimeError("游客会话仍有正在运行的 Agent 任务。")
        except sqlite3.DatabaseError:
            # A malformed guest session is not copied into an account.
            raise RuntimeError(f"游客会话状态数据库无法读取: {session.name}")
        finally:
            if connection is not None:
                connection.close()

    imported: list[str] = []
    skipped: list[str] = []
    for source in source_sessions:
        if any(item.is_symlink() for item in source.rglob("*")):
            raise RuntimeError(f"游客会话包含不支持的符号链接: {source.name}")
        target = target_paths.session_root(source.name)
        if target.exists():
            if not target.is_dir():
                raise RuntimeError(f"正式账户会话目标不是目录: {source.name}")
            skipped.append(source.name)
            continue
        _copy_session_payload(source, target)
        imported.append(source.name)

    # Projects are device-local metadata, but guest upgrade must preserve the
    # same local grouping and cwd binding.  Copy only bindings for sessions
    # that exist in the target; malformed/stale index rows are ignored rather
    # than making an otherwise valid session import fail.
    imported_projects: list[str] = []
    project_session_ids: set[str] = set()
    for project in source_projects.list("all"):
        session_ids = [
            session_id
            for session_id in source_projects.session_ids(project.project_id)
            if target_paths.session_db(session_id).is_file()
        ]
        if not session_ids:
            continue
        project_session_ids.update(session_ids)
        target_projects.import_project(project, session_ids)
        imported_projects.append(project.project_id)
    return {
        "imported": imported,
        "skipped": skipped,
        "count": len(imported),
        "sync_count": sum(1 for session_id in imported if session_id not in project_session_ids),
        "projects_imported": imported_projects,
    }


def _copy_session_payload(source: Path, target: Path) -> None:
    """Copy only the durable session contract using SQLite's online backup."""

    target.mkdir(parents=True, exist_ok=False)
    try:
        source_db = source / "state.db"
        if source_db.is_symlink():
            raise ValueError("Session state database cannot be a symbolic link.")
        target_db = target / "state.db"
        source_connection = sqlite3.connect(source_db)
        target_connection = sqlite3.connect(target_db)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        for name in ("workspace", "uploads"):
            source_item = source / name
            target_item = target / name
            if source_item.exists():
                _copy_tree_without_symlinks(source_item, target_item)
            else:
                target_item.mkdir(parents=True, exist_ok=True)
    except (OSError, sqlite3.DatabaseError, ValueError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"游客会话复制失败: {source.name}") from exc


def _copy_tree_without_symlinks(source: Path, target: Path) -> None:
    """Copy a directory without following links or special files."""

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


def _session_has_active_run(connection: sqlite3.Connection) -> bool:
    """Check known run tables while tolerating a minimal SQLite state DB."""

    for table in ("session_runs", "runs"):
        try:
            if connection.execute(f"SELECT 1 FROM {table} WHERE status='running' LIMIT 1").fetchone():
                return True
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
    return False


def user_benchmark_root(data_root: Path, user_id: str) -> Path:
    validate_identity_id(user_id, require_uuid=True)
    base = Path(data_root)
    if base.is_symlink():
        raise ValueError("User data root cannot be a symbolic link.")
    path = base.resolve().parent / ".mini_agent-cache" / "benchmark" / user_id
    if path.is_symlink():
        raise ValueError("Benchmark path cannot be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True)
    return path
