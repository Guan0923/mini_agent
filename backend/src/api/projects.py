"""Local project management endpoints."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from backend.configuration import LocalConfigStore
from backend.skills.trust import ProjectSkillTrustStore
from backend.storage.projects import Project, ProjectStore

from .session_store import mutation_error as _mutation_error
from .session_store import session_store as _store
from .session_store import summary_payload as _summary

router = APIRouter(prefix="/api")
_picker_lock = threading.Lock()


class _PickerBusyError(RuntimeError):
    """Raised when another local folder picker is already open."""


class ProjectSessionRequest(BaseModel):
    title: str | None = Field(default="新对话", max_length=120)
    client_id: str | None = Field(default=None, max_length=200)


class ProjectRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("项目名称不能为空。")
        return value


def _project_payload(project: Project, store: ProjectStore | None = None) -> dict[str, object]:
    session_ids = store.session_ids(project.project_id) if store is not None else []
    return {
        "id": project.project_id,
        "project_id": project.project_id,
        "name": project.name,
        "cwd": project.cwd,
        "available": project.available,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "removed_at": project.removed_at,
        "conversation_count": len(session_ids) if store is not None else project.conversation_count,
        "session_ids": session_ids,
    }


def _pick_directory(request: Request) -> Path | None:
    if not _picker_lock.acquire(blocking=False):
        raise _PickerBusyError("已有一个文件夹选择窗口正在打开。")
    try:
        injected = getattr(request.app.state.web, "project_picker", None)
        if injected is not None:
            try:
                selected = injected()
                if selected is None or not str(selected):
                    return None
                return Path(selected)
            except OSError:
                raise
            except Exception as exc:
                raise OSError("当前环境无法打开系统文件夹选择器。") from exc
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            try:
                root.withdraw()
                root.attributes("-topmost", True)
                selected = filedialog.askdirectory(title="选择 Mini-Agent 项目文件夹", mustexist=True)
            finally:
                root.destroy()
        except Exception as exc:
            raise OSError("当前环境无法打开系统文件夹选择器。") from exc
        return Path(selected) if selected else None
    finally:
        _picker_lock.release()


def _project_store(request: Request) -> ProjectStore:
    return request.app.state.web.projects


def _ensure_project_idle(request: Request, project_id: str) -> None:
    """Reject project mutations that would invalidate a running cwd."""

    projects = _project_store(request)
    session_store = _store(request.app.state.web)
    for summary in session_store.list_sessions(state="all"):
        bound = projects.session_project(summary.session_id, include_removed=False)
        if bound is None or bound.project_id != project_id:
            continue
        runtime = None
        try:
            runtime = session_store.load_runtime(summary.session_id)
        except Exception:
            # Durable summaries remain authoritative when an optional runtime
            # projection cannot be loaded.
            pass
        runtime_running = bool(
            runtime is not None
            and (
                getattr(runtime, "status", None) == "running"
                or getattr(getattr(runtime, "current_run", None), "status", None) == "running"
            )
        )
        if summary.last_run_status == "running" or runtime_running:
            raise HTTPException(status_code=409, detail="项目中有正在运行的任务，请先停止。")


@router.get("/projects")
def list_projects(
    request: Request,
    state: Literal["active", "removed", "all"] = "active",
) -> list[dict[str, object]]:
    try:
        store = _project_store(request)
        return [_project_payload(item, store) for item in store.list(state)]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/projects", response_model=None)
def create_project(request: Request) -> Response | dict[str, object]:
    try:
        selected = _pick_directory(request)
    except _PickerBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if selected is None:
        return Response(status_code=204)
    store = _project_store(request)
    session_store = _store(request.app.state.web)
    session = None
    project = None
    try:
        project = store.create(selected)
        try:
            session = session_store.create_session("新对话", client_id=None)
            session_store.create_sidebar_thread(
                session_id=session.session_id,
                thread_id=session.session_id,
                title=session.title,
                title_is_custom=session.title_is_custom,
            )
            store.create_session(project.project_id, session.session_id)
        except Exception:
            if session is not None:
                shutil.rmtree(session_store.paths.session_root(session.session_id), ignore_errors=True)
            # A project without its first conversation is not useful to the
            # Web client and must not leave an active duplicate path behind.
            store.discard(project.project_id)
            raise
        summary = session_store.get_session_summary(session.session_id)
        assert summary is not None
        payload = _summary(request.app.state.web, summary)
        payload["project_id"] = project.project_id
        return {"project": _project_payload(project, store), "session": payload}
    except RuntimeError as exc:
        raise _mutation_error(exc) from exc
    except (OSError, ValueError) as exc:
        raise _mutation_error(exc) from exc
    except Exception as exc:
        raise _mutation_error(exc) from exc


@router.post("/projects/{project_id}/sessions")
def create_project_session(
    project_id: str,
    body: ProjectSessionRequest,
    request: Request,
) -> dict[str, object]:
    state = request.app.state.web
    projects = _project_store(request)
    session_store = _store(state)
    try:
        project = projects.get(project_id)
        if project is None:
            raise ValueError("项目不存在。")
        if project.removed_at is not None:
            raise RuntimeError("项目已移除，请从回收站恢复后重试。")
        if not project.available:
            raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
        session = session_store.create_session(body.title, client_id=body.client_id)
        session_store.create_sidebar_thread(
            session_id=session.session_id,
            thread_id=session.session_id,
            title=session.title,
            title_is_custom=session.title_is_custom,
        )
        try:
            projects.create_session(project_id, session.session_id)
        except Exception:
            shutil.rmtree(session_store.paths.session_root(session.session_id), ignore_errors=True)
            raise
        summary = session_store.get_session_summary(session.session_id)
        assert summary is not None
        payload = _summary(state, summary)
        payload["project_id"] = project.project_id
        return {"project": _project_payload(project, projects), "session": payload}
    except RuntimeError as exc:
        raise _mutation_error(exc) from exc
    except (OSError, ValueError) as exc:
        raise _mutation_error(exc) from exc
    except Exception as exc:
        raise _mutation_error(exc) from exc


@router.post("/projects/{project_id}/remove")
def remove_project(
    project_id: str,
    request: Request,
) -> dict[str, object]:
    projects = _project_store(request)
    project = projects.get(project_id, include_removed=False)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已移除。")
    _ensure_project_idle(request, project_id)
    try:
        return _project_payload(projects.remove(project_id), projects)
    except Exception as exc:
        raise _mutation_error(exc) from exc


@router.patch("/projects/{project_id}")
def rename_project(
    project_id: str,
    body: ProjectRenameRequest,
    request: Request,
) -> dict[str, object]:
    try:
        project = _project_store(request).rename(project_id, body.name)
        return _project_payload(project, _project_store(request))
    except ValueError as exc:
        raise _mutation_error(exc) from exc
    except Exception as exc:
        raise _mutation_error(exc) from exc


@router.post("/projects/{project_id}/path", response_model=None)
def change_project_path(
    project_id: str,
    request: Request,
) -> Response | dict[str, object]:
    store = _project_store(request)
    project = store.get(project_id, include_removed=False)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已移除。")
    _ensure_project_idle(request, project_id)
    try:
        selected = _pick_directory(request)
    except _PickerBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if selected is None:
        return Response(status_code=204)
    _ensure_project_idle(request, project_id)
    try:
        updated = store.update_cwd(project_id, selected)
        return _project_payload(updated, store)
    except ValueError as exc:
        raise _mutation_error(exc) from exc
    except Exception as exc:
        raise _mutation_error(exc) from exc


@router.post("/projects/{project_id}/restore")
def restore_project(
    project_id: str,
    request: Request,
) -> dict[str, object]:
    try:
        store = _project_store(request)
        return _project_payload(store.restore(project_id), store)
    except Exception as exc:
        raise _mutation_error(exc) from exc


def _workspace_sha256(cwd: str) -> str:
    normalized = os.path.normcase(str(Path(cwd).resolve())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _trust_store(request: Request) -> ProjectSkillTrustStore:
    state = request.app.state.web
    return ProjectSkillTrustStore(LocalConfigStore(state.paths.config_file))


@router.get("/projects/{project_id}/skill-trust")
def get_project_skill_trust(
    project_id: str,
    request: Request,
) -> dict[str, object]:
    projects = _project_store(request)
    project = projects.get(project_id, include_removed=False)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已移除。")
    trusted = _trust_store(request).trusted_skills(project_id, _workspace_sha256(project.cwd))
    return {
        "project_id": project_id,
        "workspace_sha256": _workspace_sha256(project.cwd),
        "trusted_skills": {name: {"tree_sha256": tree} for name, tree in trusted.items()},
    }


@router.delete("/projects/{project_id}/skill-trust")
def revoke_project_skill_trust(
    project_id: str,
    request: Request,
) -> dict[str, object]:
    projects = _project_store(request)
    project = projects.get(project_id, include_removed=False)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已移除。")
    store = _trust_store(request)
    store.revoke_project(project_id)
    return {"project_id": project_id, "trusted_skills": {}}
