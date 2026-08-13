"""Session management endpoints for the backend service."""

from __future__ import annotations

import shutil
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.domain import DEFAULT_TIME_ZONE, TIME_ZONE_OPTIONS
from backend.domain.runtime_state import NodeWriter
from backend.runtime import build_application as _default_build_application

from ..auth.dependencies import require_user
from ..auth.types import UserIdentity
from ..shared.runtime import build_user_application
from ..state import WebAppState
from .projection import project_node_transcript

router = APIRouter(prefix="/api")
build_application = _default_build_application


def _build_user_application(state: WebAppState, user_id: str, *, session_id: str):
    """Resolve the historical module-level builder patch point."""

    import sys

    package = sys.modules.get("backend.api.sessions")
    builder = getattr(package, "build_application", build_application)
    return build_user_application(state, user_id, session_id=session_id, builder=builder)


class SessionMessageInput(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=100_000)


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    client_id: str | None = Field(default=None, max_length=200)
    messages: list[SessionMessageInput] = Field(default_factory=list, max_length=500)
    project_id: str | None = Field(default=None, max_length=200)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class BranchRequest(BaseModel):
    run_id: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=120)
    client_id: str | None = Field(default=None, max_length=200)
    fallback_messages: list[SessionMessageInput] = Field(default_factory=list, max_length=500)
    source_node_id: str | None = Field(default=None, max_length=200)


class TimezoneBody(BaseModel):
    timezone: str


def _store(state: WebAppState, user_id: str):
    from backend.storage.sqlite import SQLiteSessionStore

    paths = state.user_paths(user_id)
    store = SQLiteSessionStore(paths, f"web_{user_id}")
    if state.snapshot_manager is not None:
        store.set_sync_listener(lambda: state.mark_sync_dirty(user_id))
    return store


def _summary_payload(summary, *, project_id: str | None = None, project_available: bool | None = None) -> dict:
    return {
        "session_id": summary.session_id,
        "title": summary.title,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "message_count": summary.message_count,
        "last_node_id": summary.last_node_id,
        "last_run_id": summary.last_run_id,
        "last_run_status": summary.last_run_status,
        "client_id": summary.client_id,
        "archived_at": summary.archived_at,
        "deleted_at": summary.deleted_at,
        "local_only": summary.local_only,
        "project_id": project_id,
        "project_available": project_available,
    }


def _summary_for_user(state: WebAppState, user_id: str, summary) -> dict:
    project = state.projects(user_id).session_project(summary.session_id)
    return _summary_payload(
        summary,
        project_id=project.project_id if project is not None else None,
        project_available=project.available if project is not None else None,
    )


def _node_payload(node) -> dict:
    return node.to_dict() if hasattr(node, "to_dict") else dict(node)


def _require_summary(store, session_id: str):
    try:
        summary = store.get_session_summary(session_id)
    except ValueError as exc:
        # Session identifiers are part of the filesystem boundary.  Invalid
        # values must become a client error rather than an internal traceback
        # (and must never be normalized into a path by the route layer).
        raise HTTPException(status_code=400, detail="会话 ID 无效。") from exc
    if summary is None:
        raise HTTPException(status_code=404, detail=f"未知会话：{session_id}")
    return summary


def _require_active(store, session_id: str):
    summary = _require_summary(store, session_id)
    if summary.deleted_at is not None:
        raise HTTPException(status_code=409, detail="会话已删除，无法继续操作。")
    if summary.archived_at is not None:
        raise HTTPException(status_code=409, detail="会话已归档，请先恢复。")
    return summary


def _require_branchable(store, session_id: str):
    summary = _require_active(store, session_id)
    if summary.last_run_status == "running":
        raise HTTPException(status_code=409, detail="会话已有正在运行的任务，请先停止。")
    return summary


def _mutation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/sessions")
def list_sessions(
    request: Request,
    state: Literal["active", "archived", "deleted", "all"] = "active",
    identity: UserIdentity = Depends(require_user),
) -> list[dict]:
    app_state: WebAppState = request.app.state.web
    store = _store(app_state, identity.id)
    try:
        summaries = store.list_sessions(state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="会话目录包含无效的会话 ID。") from exc
    projects = app_state.projects(identity.id)
    result: list[dict] = []
    for summary in summaries:
        project = projects.session_project(summary.session_id)
        # Project history has its own sidebar/recycle-bin projection.  Never
        # let it leak into the ordinary archive/deleted lists; removed project
        # sessions are hidden from every ordinary list while retaining direct
        # history access by session id.
        if project is not None and (project.removed_at is not None or state in {"archived", "deleted"}):
            continue
        result.append(
            _summary_payload(
                summary,
                project_id=project.project_id if project is not None else None,
                project_available=project.available if project is not None else None,
            )
        )
    return result


@router.post("/sessions")
def create_session(
    body: CreateSessionRequest,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    try:
        if body.project_id is not None:
            raise ValueError("项目会话必须通过项目接口创建。")
        if body.client_id:
            existing = store.find_session_by_client_id(body.client_id)
            if existing is not None:
                # A stale browser cache must never re-open or overwrite a
                # project conversation through the ordinary session endpoint.
                # Project sessions are created only through their scoped API;
                # callers that submit this client id must refresh metadata.
                if state.projects(identity.id).session_project(existing.session_id) is not None:
                    raise ValueError("项目会话必须通过项目接口打开。")
                summary = store.get_session_summary(existing.session_id)
                assert summary is not None
                return _summary_for_user(state, identity.id, summary)
        if body.messages:
            session = store.import_conversation(
                body.title,
                [message.model_dump() for message in body.messages],
                client_id=body.client_id,
            )
        else:
            session = store.create_session(body.title, client_id=body.client_id)
    except Exception as exc:
        raise _mutation_error(exc) from exc
    summary = store.get_session_summary(session.session_id)
    assert summary is not None
    state.user_paths(identity.id).ensure_session(session.session_id)
    return _summary_for_user(state, identity.id, summary)


def _require_session(state: WebAppState, user_id: str, session_id: str):
    store = _store(state, user_id)
    return store, _require_summary(store, session_id)


def _require_session_workspace(state: WebAppState, user_id: str, session_id: str) -> None:
    """Reject operations that would run against a removed/unavailable cwd."""

    try:
        state.session_workspace(user_id, session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/sessions/{session_id}")
def get_session(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    return _summary_for_user(state, identity.id, _require_summary(store, session_id))


@router.patch("/sessions/{session_id}")
def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    try:
        session = store.rename_session(session_id, body.title)
    except Exception as exc:
        raise _mutation_error(exc) from exc
    summary = store.get_session_summary(session.session_id)
    assert summary is not None
    return _summary_for_user(state, identity.id, summary)


@router.post("/sessions/{session_id}/archive")
def archive_session(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    _require_summary(store, session_id)
    try:
        session = store.archive_session(session_id)
    except Exception as exc:
        raise _mutation_error(exc) from exc
    summary = store.get_session_summary(session.session_id)
    assert summary is not None
    return _summary_for_user(state, identity.id, summary)


@router.post("/sessions/{session_id}/restore")
def restore_session(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    _require_summary(store, session_id)
    try:
        session = store.restore_session(session_id)
    except Exception as exc:
        raise _mutation_error(exc) from exc
    summary = store.get_session_summary(session.session_id)
    assert summary is not None
    return _summary_for_user(state, identity.id, summary)


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    _require_summary(store, session_id)
    try:
        session = store.delete_session(session_id)
    except Exception as exc:
        raise _mutation_error(exc) from exc
    summary = store.get_session_summary(session.session_id)
    assert summary is not None
    return _summary_for_user(state, identity.id, summary)


@router.delete("/sessions/{session_id}/purge", status_code=204)
def purge_session(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> None:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    _require_summary(store, session_id)
    try:
        store.purge_session(session_id)
    except Exception as exc:
        raise _mutation_error(exc) from exc


@router.get("/sessions/{session_id}/messages")
def get_session_messages(
    session_id: str, request: Request, identity: UserIdentity = Depends(require_user)
) -> list[dict]:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    _require_summary(store, session_id)
    return store.load_conversation(session_id)


@router.get("/sessions/{session_id}/nodes")
def get_session_nodes(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> list[dict]:
    """Return canonical static nodes; dynamic update frames are stream-only."""

    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    _require_summary(store, session_id)
    return [_node_payload(node) for node in store.load_nodes(session_id)]


@router.get("/sessions/{session_id}/leaves")
def get_session_leaves(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> list[dict]:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    _require_summary(store, session_id)
    nodes = store.load_nodes(session_id)
    parent_keys = {(node.parent_session_id, node.parent_id) for node in nodes if node.parent_id}
    return [
        _node_payload(node)
        for node in nodes
        if node.session_id == session_id and (node.session_id, node.id) not in parent_keys
    ]


@router.get("/sessions/{session_id}/transcript")
def get_session_transcript(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> list[dict]:
    """Return the Web projection while keeping the legacy messages endpoint stable."""

    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    _require_summary(store, session_id)
    nodes = list(getattr(store, "load_nodes", lambda _session_id: [])(session_id))
    projected = project_node_transcript(nodes)
    if projected:
        for payload in projected:
            run_id = payload.get("run_id")
            if payload["role"] != "assistant" or not run_id:
                continue
            events = store.load_runtime_messages(session_id, run_id=str(run_id))
            for event in events:
                event_data = dict(event.data)
                if event.kind in {"thinking", "thinking_start", "thinking_delta"}:
                    if not any(item.get("kind") == "thinking" for item in payload["events"]):
                        payload["events"].append({"kind": "thinking", "message": event.message, "data": event_data})
                elif event.kind in {"tool_call", "tool_result", "tool_failed"}:
                    call_id = str(event_data.get("call_id") or "")
                    already_present = any(
                        item.get("kind") == event.kind
                        and (call_id == "" or str((item.get("data") or {}).get("call_id") or "") == call_id)
                        for item in payload["events"]
                    )
                    if not already_present:
                        payload["events"].append({"kind": event.kind, "message": event.message, "data": event_data})
                elif event.kind == "error":
                    payload.setdefault("error", event.message)
                elif event.kind == "run_finished":
                    payload["status"] = str(event.data.get("status") or event.message)
                    payload["metrics"] = {
                        key: event.data.get(key)
                        for key in ("duration_ms", "model_calls", "tool_calls", "active_skills")
                        if event.data.get(key) is not None
                    }
                elif event.kind == "cancelled" and "status" not in payload:
                    payload["status"] = str(event.data.get("status") or event.message or "cancelled")
            if any(event.kind == "run_started" for event in events) and not any(
                event.kind == "run_finished" for event in events
            ):
                payload["running"] = True
        return projected
    records = store.load_conversation_records(session_id)

    result: list[dict] = []
    for record in records:
        run_id = str(record["run_id"]) if record.get("run_id") else None
        payload = {
            "id": f"{session_id}:{record['id']}",
            "run_id": run_id,
            "role": record["role"],
            "content": record["content"],
            "events": [],
        }
        # Query by run rather than relying on event payloads to carry an ID.
        # Older runtime records may predate the enriched event envelope.
        events = store.load_runtime_messages(session_id, run_id=run_id) if run_id else []
        if record["role"] == "assistant":
            for event in events:
                if event.kind in {
                    "thinking",
                    "thinking_start",
                    "thinking_delta",
                    "thinking_end",
                    "tool_call",
                    "tool_result",
                    "tool_failed",
                }:
                    payload["events"].append({"kind": event.kind, "message": event.message, "data": dict(event.data)})
                elif event.kind == "error":
                    payload.setdefault("error", event.message)
                elif event.kind == "run_finished":
                    payload["status"] = str(event.data.get("status") or event.message)
                    payload["metrics"] = {
                        key: event.data.get(key)
                        for key in ("duration_ms", "model_calls", "tool_calls", "active_skills")
                        if event.data.get(key) is not None
                    }
                elif event.kind == "cancelled" and "status" not in payload:
                    payload["status"] = str(event.data.get("status") or event.message or "cancelled")
            if any(event.kind == "run_started" for event in events) and not any(
                event.kind == "run_finished" for event in events
            ):
                payload["running"] = True
        result.append(payload)
    return result


def _branch_session(store, source, body: BranchRequest, *, rewind: bool):
    title = body.title or (source.title if rewind else f"{source.title}（分支）")
    client_id = body.client_id
    target = None
    try:
        if body.source_node_id:
            # ``load_nodes`` includes the cross-session ancestors of a fork,
            # while ``get_node(session_id, id)`` only searches one database.
            # Resolve the message's own source node from that bounded tree so
            # historical branches work without accepting an arbitrary node
            # from another conversation.
            source_node = None
            loader = getattr(store, "load_nodes", None)
            if callable(loader):
                candidates = [
                    node for node in loader(source.session_id) if str(getattr(node, "id", "")) == body.source_node_id
                ]
                source_node = next(
                    (node for node in candidates if str(getattr(node, "session_id", "")) == source.session_id),
                    candidates[0] if candidates else None,
                )
            if source_node is None:
                source_node = store.get_node(source.session_id, body.source_node_id)
            if source_node is None:
                raise ValueError("指定的 source_node_id 不属于当前会话。")
            target = store.create_session(title, client_id=client_id, local_only=source.local_only)
            writer = NodeWriter(store)
            root = writer.create(
                session_id=target.session_id,
                parent=(source_node.session_id, source_node.id),
                provider=source_node.provider,
                user=source_node.user,
                cwd=source_node.cwd,
                first_kept_entry_id=source_node.firstKeptEntryId,
                compaction_idx=source_node.compactionIdx,
            )
            writer.delete(root.session_id, root.id)
        elif body.run_id:
            records = store.load_conversation_records(source.session_id)
            if not any(str(record["run_id"]) == body.run_id for record in records):
                raise ValueError("指定的 run 不属于当前会话。")
            target = store.fork_run(body.run_id)
            target = store.rename_session(target.session_id, title)
            if client_id:
                target = store.set_client_id(target.session_id, client_id)
        else:
            target = store.import_conversation(
                title,
                [message.model_dump() for message in body.fallback_messages],
                client_id=client_id,
                force_new=rewind,
                local_only=source.local_only,
            )
    except Exception:
        if target is not None:
            # ``target`` is a fresh session created by this branch operation.
            # Remove its durable shell when building the copied state fails;
            # otherwise a failed request would leave an empty/partial session
            # visible to the next list request.
            try:
                shutil.rmtree(store.paths.session_root(target.session_id), ignore_errors=True)
            except Exception:
                pass
        raise
    summary = store.get_session_summary(target.session_id)
    assert summary is not None
    return summary


def _copy_or_bind_project(state: WebAppState, user_id: str, source_session_id: str, target_session_id: str) -> None:
    project = state.projects(user_id).session_project(source_session_id)
    if project is not None:
        if project.removed_at is not None:
            raise RuntimeError("项目已移除，请从回收站恢复后再创建分支。")
        if not project.available:
            raise RuntimeError("项目 cwd 不可访问，请恢复文件夹后再创建分支。")
        state.projects(user_id).create_session(project.project_id, target_session_id)
        state.copy_session_uploads(user_id, source_session_id, target_session_id)
        return
    state.copy_session_files(user_id, source_session_id, target_session_id)


@router.post("/sessions/{session_id}/fork")
def fork_session(
    session_id: str,
    body: BranchRequest,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    source = _require_branchable(store, session_id)
    _require_session_workspace(state, identity.id, source.session_id)
    target_id: str | None = None
    try:
        summary = _branch_session(store, source, body, rewind=False)
        target_id = summary.session_id
        _copy_or_bind_project(state, identity.id, source.session_id, summary.session_id)
    except Exception as exc:
        if target_id is not None:
            try:
                state.projects(identity.id).discard_session(target_id)
                shutil.rmtree(store.paths.session_root(target_id), ignore_errors=True)
            except Exception:
                pass
        raise _mutation_error(exc) from exc
    return _summary_for_user(state, identity.id, summary)


@router.post("/sessions/{session_id}/rewind")
def rewind_session(
    session_id: str,
    body: BranchRequest,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    source = _require_branchable(store, session_id)
    _require_session_workspace(state, identity.id, source.session_id)
    target_id: str | None = None
    copied = False
    try:
        summary = _branch_session(store, source, body, rewind=True)
        target_id = summary.session_id
        _copy_or_bind_project(state, identity.id, source.session_id, summary.session_id)
        copied = True
        store.delete_session(source.session_id)
    except Exception as exc:
        if target_id is not None:
            try:
                if copied:
                    # Rewind is a compound operation.  If deleting the
                    # source fails after the new session was copied, keep the
                    # source authoritative and move the speculative target to
                    # the soft-deleted state instead of leaving two active
                    # conversations.
                    store.delete_session(target_id)
                else:
                    # A failed file copy must not leave a discoverable,
                    # half-initialized session behind.
                    shutil.rmtree(store.paths.session_root(target_id), ignore_errors=True)
                state.projects(identity.id).discard_session(target_id)
            except Exception:
                pass
        raise _mutation_error(exc) from exc
    return _summary_for_user(state, identity.id, summary)


@router.get("/sessions/{session_id}/timezone")
def get_timezone(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    _store_instance, _summary_value = _require_session(state, identity.id, session_id)
    runtime = _store_instance.load_runtime(session_id)
    selected = runtime.state.timezone if runtime is not None else DEFAULT_TIME_ZONE
    return {
        "timezone": selected,
        "options": [{"identifier": option.identifier, "label": option.label} for option in TIME_ZONE_OPTIONS],
    }


@router.put("/sessions/{session_id}/timezone")
def set_timezone(
    session_id: str,
    body: TimezoneBody,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    _require_session(state, identity.id, session_id)
    _require_session_workspace(state, identity.id, session_id)
    application = None
    try:
        application = _build_user_application(state, identity.id, session_id=session_id)
        conversation = application.open_conversation(session_id)
        selected = conversation.set_timezone(body.timezone)
        return {"timezone": selected}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        if application is not None:
            application.close()


@router.post("/sessions/{session_id}/compact")
def compact_session(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    _require_session(state, identity.id, session_id)
    _require_session_workspace(state, identity.id, session_id)
    application = None
    try:
        application = _build_user_application(state, identity.id, session_id=session_id)
        conversation = application.open_conversation(session_id)
        result = conversation.compact_context()
        return {
            "compacted": result.compacted,
            "previous_messages": result.previous_messages,
            "remaining_messages": result.remaining_messages,
            "summary": result.summary,
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        if application is not None:
            application.close()


@router.get("/sessions/{session_id}/trace")
def get_trace(
    session_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store, summary = _require_session(state, identity.id, session_id)
    runtime = store.load_runtime(session_id)
    current_run = runtime.state.current_run if runtime is not None else None
    return {
        "session_id": session_id,
        "title": summary.title,
        "run": current_run.to_dict() if current_run is not None else None,
    }


@router.get("/forkable-runs")
def list_forkable_runs(
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> list[dict[str, str]]:
    state: WebAppState = request.app.state.web
    return _store(state, identity.id).list_forkable_runs()


@router.post("/runs/{run_id}/fork")
def fork_run(
    run_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    target_id: str | None = None
    source = store.find_run_session(run_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    _require_session_workspace(state, identity.id, source.session_id)
    try:
        session = store.fork_run(run_id)
        target_id = session.session_id
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # The direct run-fork endpoint must carry the source session's current
    # workspace and uploads just like the branch/rewind endpoints.
    try:
        inherited_source_session_id = getattr(session, "source_session_id", None)
        if not inherited_source_session_id:
            runtime = store.load_runtime(session.session_id)
            provenance = runtime.state.current_run.provenance if runtime and runtime.state.current_run else None
            inherited_source_session_id = getattr(provenance, "source_session_id", None)
        if inherited_source_session_id:
            _copy_or_bind_project(state, identity.id, str(inherited_source_session_id), session.session_id)
    except Exception as exc:
        if target_id is not None:
            try:
                state.projects(identity.id).discard_session(target_id)
                shutil.rmtree(store.paths.session_root(target_id), ignore_errors=True)
            except Exception:
                pass
        raise _mutation_error(exc) from exc
    summary = store.get_session_summary(session.session_id)
    assert summary is not None
    return _summary_for_user(state, identity.id, summary)
