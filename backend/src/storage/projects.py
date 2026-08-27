"""Local-only project and conversation cwd metadata.

Projects use their own small transactional database so the Web UI can list
workspaces without opening every session database.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from backend.domain.state import utc_now

PROJECTS_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cwd TEXT NOT NULL,
    cwd_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    removed_at TEXT
);
CREATE TABLE IF NOT EXISTS project_sessions (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS project_sessions_project_idx
    ON project_sessions(project_id, created_at, session_id);
"""


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    cwd: str
    created_at: str
    updated_at: str
    removed_at: str | None
    conversation_count: int = 0

    @property
    def available(self) -> bool:
        path = Path(self.cwd)
        try:
            # Removal is a lifecycle flag, not a filesystem mutation.  Keep
            # ``available`` about the selected cwd itself so recycle-bin
            # entries can accurately report whether that folder still exists.
            return path.is_dir()
        except (OSError, RuntimeError):
            return False


class ProjectStore:
    """Thread-safe project index for the local Mini-Agent installation."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        if path.name != "projects.db":
            raise ValueError("Project database must be named projects.db.")
        if path.is_symlink():
            raise ValueError("Project database cannot be a symbolic link.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        with self._connection() as connection:
            connection.executescript(PROJECTS_SCHEMA)

    @contextmanager
    def _connection(self):
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def normalize_cwd(value: str | Path) -> tuple[Path, str]:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("项目 cwd 必须是一个目录。")
        # ``normcase`` follows the host filesystem: it folds case on Windows
        # while preserving distinct case-sensitive paths on POSIX systems.
        key = os.path.normcase(str(path))
        return path, key

    @staticmethod
    def _row(row: sqlite3.Row) -> Project:
        return Project(
            project_id=str(row["project_id"]),
            name=str(row["name"]),
            cwd=str(row["cwd"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            removed_at=str(row["removed_at"]) if row["removed_at"] is not None else None,
            conversation_count=int(row["conversation_count"]),
        )

    def create(self, cwd: str | Path, *, name: str | None = None) -> Project:
        path, key = self.normalize_cwd(cwd)
        default_name = path.name or path.anchor or str(path)
        cleaned_name = (name or default_name).strip() or default_name
        now = utc_now()
        project_id = f"project_{uuid4().hex}"
        with self._connection() as connection:
            # Serialize the duplicate check and insert across independent
            # ProjectStore instances in the same process/database.
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT project_id FROM projects WHERE cwd_key=? AND removed_at IS NULL LIMIT 1", (key,)
            ).fetchone()
            if duplicate is not None:
                raise RuntimeError("该文件夹已经有一个活动项目。")
            connection.execute(
                "INSERT INTO projects(project_id,name,cwd,cwd_key,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (project_id, cleaned_name[:120], str(path), key, now, now),
            )
        project = self.get(project_id)
        assert project is not None
        return project

    def get(self, project_id: str, *, include_removed: bool = True) -> Project | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT p.project_id,p.name,p.cwd,p.created_at,p.updated_at,p.removed_at,
                    COUNT(ps.session_id) AS conversation_count
                    FROM projects AS p LEFT JOIN project_sessions AS ps ON ps.project_id=p.project_id
                    WHERE p.project_id=? GROUP BY p.project_id""",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        project = self._row(row)
        return project if include_removed or project.removed_at is None else None

    def list(self, state: str = "active") -> list[Project]:
        if state not in {"active", "removed", "all"}:
            raise ValueError(f"Unknown project state: {state}")
        where = (
            ""
            if state == "all"
            else ("WHERE p.removed_at IS NULL" if state == "active" else "WHERE p.removed_at IS NOT NULL")
        )
        with self._connection() as connection:
            rows = connection.execute(
                f"""SELECT p.project_id,p.name,p.cwd,p.created_at,p.updated_at,p.removed_at,
                    COUNT(ps.session_id) AS conversation_count
                    FROM projects AS p LEFT JOIN project_sessions AS ps ON ps.project_id=p.project_id
                    {where} GROUP BY p.project_id ORDER BY p.updated_at DESC,p.project_id DESC"""
            ).fetchall()
        return [self._row(row) for row in rows]

    def session_project(self, session_id: str, *, include_removed: bool = True) -> Project | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT p.project_id,p.name,p.cwd,p.created_at,p.updated_at,p.removed_at,
                    COUNT(all_ps.session_id) AS conversation_count
                    FROM project_sessions AS ps JOIN projects AS p ON p.project_id=ps.project_id
                    LEFT JOIN project_sessions AS all_ps ON all_ps.project_id=p.project_id
                    WHERE ps.session_id=? GROUP BY p.project_id""",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        project = self._row(row)
        return project if include_removed or project.removed_at is None else None

    def create_session(self, project_id: str, session_id: str) -> Project:
        project = self.get(project_id, include_removed=False)
        if project is None:
            raise ValueError("项目不存在或已移除。")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO project_sessions(project_id,session_id,created_at) VALUES (?,?,?)",
                (project_id, session_id, utc_now()),
            )
            connection.execute("UPDATE projects SET updated_at=? WHERE project_id=?", (utc_now(), project_id))
        return project

    def rename(self, project_id: str, name: str) -> Project:
        project = self.get(project_id, include_removed=False)
        if project is None:
            raise ValueError("项目不存在或已移除。")
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError("项目名称不能为空。")
        if len(cleaned_name) > 120:
            raise ValueError("项目名称不能超过 120 个字符。")
        with self._connection() as connection:
            connection.execute(
                "UPDATE projects SET name=?,updated_at=? WHERE project_id=?",
                (cleaned_name, utc_now(), project_id),
            )
        result = self.get(project_id, include_removed=False)
        assert result is not None
        return result

    def update_cwd(self, project_id: str, cwd: str | Path) -> Project:
        project = self.get(project_id, include_removed=False)
        if project is None:
            raise ValueError("项目不存在或已移除。")
        path, key = self.normalize_cwd(cwd)
        with self._connection() as connection:
            connection.execute(
                "UPDATE projects SET cwd=?,cwd_key=?,updated_at=? WHERE project_id=?",
                (str(path), key, utc_now(), project_id),
            )
        result = self.get(project_id, include_removed=False)
        assert result is not None
        return result

    def discard_session(self, session_id: str) -> None:
        """Remove a binding created by a failed fork/provisioning operation."""

        with self._connection() as connection:
            connection.execute("DELETE FROM project_sessions WHERE session_id=?", (session_id,))

    def discard(self, project_id: str) -> None:
        """Delete a never-exposed project during an atomic creation rollback.

        Normal user removal is deliberately soft (``remove``).  This hard
        delete is only used when provisioning the first session failed before
        the project could be returned to a client.
        """

        with self._connection() as connection:
            connection.execute("DELETE FROM projects WHERE project_id=?", (project_id,))

    def remove(self, project_id: str) -> Project:
        project = self.get(project_id, include_removed=False)
        if project is None:
            raise ValueError("项目不存在或已移除。")
        with self._connection() as connection:
            connection.execute(
                "UPDATE projects SET removed_at=?,updated_at=? WHERE project_id=?", (utc_now(), utc_now(), project_id)
            )
        result = self.get(project_id)
        assert result is not None
        return result

    def restore(self, project_id: str) -> Project:
        project = self.get(project_id)
        if project is None:
            raise ValueError("项目不存在。")
        with self._connection() as connection:
            connection.execute(
                "UPDATE projects SET removed_at=NULL,updated_at=? WHERE project_id=?", (utc_now(), project_id)
            )
        result = self.get(project_id)
        assert result is not None
        return result

    def project_session_ids(self, *, include_removed: bool = True) -> set[str]:
        with self._connection() as connection:
            query = "SELECT ps.session_id FROM project_sessions AS ps JOIN projects AS p ON p.project_id=ps.project_id"
            if not include_removed:
                query += " WHERE p.removed_at IS NULL"
            rows = connection.execute(query).fetchall()
        return {str(row[0]) for row in rows}

    def session_ids(self, project_id: str) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT session_id FROM project_sessions WHERE project_id=? ORDER BY created_at,session_id",
                (project_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]
