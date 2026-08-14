"""Local project management endpoints."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from backend.storage.projects import Project, ProjectStore

from .auth.dependencies import require_user
from .auth.types import UserIdentity
from .sessions.routes import _mutation_error, _store, _summary_for_user

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


def _project_store(request: Request, identity: UserIdentity) -> ProjectStore:
    return request.app.state.web.projects(identity.id)


def _ensure_project_idle(request: Request, identity: UserIdentity, project_id: str) -> None:
    """Reject project mutations that would invalidate a running cwd."""

    projects = _project_store(request, identity)
    session_store = _store(request.app.state.web, identity.id)
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
    identity: UserIdentity = Depends(require_user),
) -> list[dict[str, object]]:
    try:
        store = _project_store(request, identity)
        return [_project_payload(item, store) for item in store.list(state)]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/projects", response_model=None)
def create_project(request: Request, identity: UserIdentity = Depends(require_user)) -> Response | dict[str, object]:
    try:
        selected = _pick_directory(request)
    except _PickerBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if selected is None:
        return Response(status_code=204)
    store = _project_store(request, identity)
    session_store = _store(request.app.state.web, identity.id)
    session = None
    project = None
    try:
        project = store.create(selected)
        try:
            session = session_store.create_session("新对话", client_id=None, local_only=True)
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
        payload = _summary_for_user(request.app.state.web, identity.id, summary)
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
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    state = request.app.state.web
    projects = _project_store(request, identity)
    session_store = _store(state, identity.id)
    try:
        project = projects.get(project_id)
        if project is None:
            raise ValueError("项目不存在。")
        if project.removed_at is not None:
            raise RuntimeError("项目已移除，请从回收站恢复后重试。")
        if not project.available:
            raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后重试。")
        session = session_store.create_session(body.title, client_id=body.client_id, local_only=True)
        try:
            projects.create_session(project_id, session.session_id)
        except Exception:
            shutil.rmtree(session_store.paths.session_root(session.session_id), ignore_errors=True)
            raise
        summary = session_store.get_session_summary(session.session_id)
        assert summary is not None
        payload = _summary_for_user(state, identity.id, summary)
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
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    projects = _project_store(request, identity)
    project = projects.get(project_id, include_removed=False)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已移除。")
    _ensure_project_idle(request, identity, project_id)
    try:
        return _project_payload(projects.remove(project_id), projects)
    except Exception as exc:
        raise _mutation_error(exc) from exc


@router.patch("/projects/{project_id}")
def rename_project(
    project_id: str,
    body: ProjectRenameRequest,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    try:
        project = _project_store(request, identity).rename(project_id, body.name)
        return _project_payload(project, _project_store(request, identity))
    except ValueError as exc:
        raise _mutation_error(exc) from exc
    except Exception as exc:
        raise _mutation_error(exc) from exc


@router.post("/projects/{project_id}/path", response_model=None)
def change_project_path(
    project_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> Response | dict[str, object]:
    store = _project_store(request, identity)
    project = store.get(project_id, include_removed=False)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在或已移除。")
    _ensure_project_idle(request, identity, project_id)
    try:
        selected = _pick_directory(request)
    except _PickerBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if selected is None:
        return Response(status_code=204)
    _ensure_project_idle(request, identity, project_id)
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
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    try:
        store = _project_store(request, identity)
        return _project_payload(store.restore(project_id), store)
    except Exception as exc:
        raise _mutation_error(exc) from exc
