"""Session management endpoints for the backend service."""

from __future__ import annotations

import shutil
from threading import RLock
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictBool, model_validator

from backend.domain import DEFAULT_TIME_ZONE, TIME_ZONE_OPTIONS
from backend.domain.runtime_state import RuntimeStateValidationError
from backend.providers import ModelConfigurationError
from backend.runtime import build_application as _default_build_application
from backend.storage.auth.crypto import SecretDecryptionError
from backend.storage.codec import normalize_session_title

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
    source_node_session_id: str | None = Field(default=None, max_length=200)


class TimezoneBody(BaseModel):
    timezone: str


class RuntimeConfigPatch(BaseModel):
    node_id: str = Field(min_length=1, max_length=200)
    provider_name: str | None = Field(default=None, min_length=1, max_length=80)
    model: dict[str, object] | None = None
    permission_mode: Literal["approval_for_me", "read_only", "workspace_write", "full_access"] | None = None
    full_access_acknowledged: StrictBool = False
    running_mode: Literal["agent", "plan"] | None = None

    @model_validator(mode="after")
    def require_full_access_acknowledgement(self) -> RuntimeConfigPatch:
        if self.permission_mode == "full_access" and not self.full_access_acknowledged:
            raise ValueError("full_access requires explicit joint file and network confirmation")
        return self

    @property
    def has_update(self) -> bool:
        return any(
            value is not None for value in (self.provider_name, self.model, self.permission_mode, self.running_mode)
        )


def _provider_model_snapshot(
    config, public_record: dict[str, object], *, explicit: dict[str, object] | None = None
) -> dict[str, object]:
    """Build the canonical node model for a provider switch.

    A provider name identifies a complete saved configuration.  The model
    carried by the active node may contain overrides from a different
    provider, so it must never be used as the base when the name changes.
    Only fields explicitly supplied in this PATCH are applied on top of the
    newly loaded provider defaults.
    """

    defaults: dict[str, object] = {
        "reasoning_effort": "medium",
        "current_model": getattr(config, "model", None) or public_record.get("model") or "unknown",
        "context_length": int(getattr(config, "context_size", None) or public_record.get("context_size") or 128000),
        "output_length": int(getattr(config, "max_tokens", None) or public_record.get("max_tokens") or 8192),
        "thinking": "enable",
        "temperature": 1.0,
    }
    if explicit:
        defaults.update(explicit)
    return defaults


def _runtime_config_bridge(state: WebAppState, identity: UserIdentity, session_id: str):
    bridges = getattr(state, "active_runtime_bridges", {})
    owner_key = (identity.id, session_id)
    # Session identifiers are scoped to an authenticated user.  Never fall
    # back to the pre-v0.3 session-only registry key: doing so could let a
    # user mutate a same-named session owned by another account in a shared
    # process.
    return bridges.get(owner_key)


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
        "title_is_custom": summary.title_is_custom,
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
    return summary


def _has_active_execution(state: WebAppState, user_id: str, session_id: str) -> bool:
    bridge = getattr(state, "active_runtime_bridges", {}).get((user_id, session_id))
    if bridge is not None and not bool(getattr(bridge, "closed", False)):
        return True
    registry = getattr(state, "job_registry", None)
    if registry is None:
        return False
    try:
        from backend.jobs import JobState

        return any(
            item.info.state in {JobState.PENDING, JobState.RUNNING}
            for item in registry.list_for_user(user_id, session_id=session_id)
        )
    except (AttributeError, TypeError):
        return False


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


@router.patch("/sessions/{session_id}/runtime-config")
def patch_runtime_config(
    session_id: str,
    body: RuntimeConfigPatch,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict:
    """Update the active dynamic leaf configuration for the current user."""

    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    summary = _require_active(store, session_id)
    if not _has_active_execution(state, identity.id, session_id):
        if summary.last_run_status != "running":
            raise HTTPException(status_code=409, detail="当前会话没有正在运行的任务")
    if not body.has_update:
        raise HTTPException(status_code=422, detail="至少需要一个运行配置字段")
    node = store.get_node(session_id, body.node_id)
    if node is None:
        raise HTTPException(status_code=409, detail="node_id 不属于当前会话")
    if node.session_id != session_id:
        raise HTTPException(status_code=409, detail="node_id 不属于当前会话")
    if getattr(node, "data_type", None) == "root":
        raise HTTPException(status_code=409, detail="root 节点不可修改运行配置")
    children = store.list_children(node.session_id, node.id)
    if children:
        raise HTTPException(status_code=409, detail="node_id 不是活动叶节点")
    # The stream owns the dynamic sidecar; publish a config event to its input
    # channel where the bridge applies it and emits the full node.update frame.
    registry = getattr(state, "active_runtime_configs", None)
    if registry is None:
        registry = state.active_runtime_configs = {}
    owner_key = (identity.id, session_id)
    lock_registry = getattr(state, "active_runtime_config_locks", {})
    lock = lock_registry.setdefault(owner_key, RLock())
    with lock:
        previous_registry = dict(registry.get(owner_key, {}))
        current = dict(previous_registry)
        patch_values = body.model_dump(exclude_none=True, exclude={"full_access_acknowledged"})
        current.update(patch_values)
        bridge = _runtime_config_bridge(state, identity, session_id)
        if bridge is None:
            raise HTTPException(status_code=409, detail="当前运行尚未注册动态节点")
        # Only the mutable assistant sidecar is addressable while a run is
        # active.  ``last_node`` is often a sealed user/tool node between
        # workflow boundaries and must never be treated as a PATCH target.
        active = getattr(bridge, "assistant", None)
        if active is None or str(getattr(active, "id", "")) != body.node_id:
            raise HTTPException(status_code=409, detail="node_id 与活动动态节点不匹配")
        try:
            live = bridge.writer.current(active.session_id, active.id)
        except (AttributeError, KeyError) as exc:
            raise HTTPException(status_code=409, detail="活动动态节点已结束") from exc
        # The registry only contains fields waiting for the next boundary and
        # is normally empty after each immediate update.  Seed a new partial
        # patch from the live dynamic node so changing one field (for example
        # permission or reasoning) never resets the other model fields to
        # provider defaults.  A real provider switch below deliberately
        # discards this model snapshot and loads the selected provider's
        # complete defaults instead.
        if not previous_registry:
            current = {
                "provider_name": live.provider_name,
                "model": dict(live.model),
                "permission_mode": live.permission_mode,
                "running_mode": live.running_mode,
                **patch_values,
            }
        if body.provider_name:
            providers_reader = getattr(state.settings, "provider_configs_for_user", None)
            providers = providers_reader(identity.id) if callable(providers_reader) else []
            match = next(
                (
                    item
                    for item in providers
                    if str(item.get("provider_name") or item.get("provider") or "").casefold()
                    == body.provider_name.casefold()
                ),
                None,
            )
            if match is None:
                raise HTTPException(status_code=404, detail="provider_name 不存在")
            try:
                resolved = state.model_config_for_provider_name(identity.id, body.provider_name)
            except SecretDecryptionError as exc:
                raise HTTPException(status_code=409, detail="提供商密钥无法解密，请重新配置") from exc
            except ModelConfigurationError as exc:
                raise HTTPException(status_code=422, detail=f"提供商配置无效：{exc}") from exc
            current["provider_name"] = str(
                getattr(resolved, "provider_name", None) or match.get("provider_name") or body.provider_name
            )
            old_provider = str(getattr(bridge, "provider_name", "") or "")
            provider_changed = old_provider.casefold() != body.provider_name.casefold()
            explicit_model = body.model if isinstance(body.model, dict) else None
            if provider_changed or not isinstance(current.get("model"), dict):
                # Do not merge a prior registry model into a newly selected
                # provider.  The only permitted override is this request's
                # explicit ``model`` object.
                current["model"] = _provider_model_snapshot(resolved, match, explicit=explicit_model)
            elif explicit_model:
                current["model"] = {**current["model"], **explicit_model}
        registry[owner_key] = current
        # Apply immediately so the stream emits a complete node.update frame;
        # request boundaries still decide when the provider consumes the change.
        try:
            updated = bridge.apply_runtime_config(current)
        except (RuntimeStateValidationError, ValueError) as exc:
            # A rejected candidate must be atomic.  Preserve a previously
            # queued valid patch so a transient bad request cannot erase a
            # user's earlier update waiting for the next execution boundary.
            if previous_registry:
                registry[owner_key] = previous_registry
            else:
                registry.pop(owner_key, None)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        registry.pop(owner_key, None)
    return {
        "session_id": session_id,
        "node_id": body.node_id,
        **(updated.to_dict() if updated is not None else current),
    }


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
            segments: dict[str, dict] = {}
            for event in events:
                event_data = dict(event.data)
                if event.kind == "run_segment":
                    segment_id = str(event_data.get("segment_id") or "")
                    if segment_id:
                        segments[segment_id] = event_data
                    continue
                if event.kind in {"thinking", "thinking_start", "thinking_delta"}:
                    if not any(item.get("kind") == "thinking" for item in payload["events"]):
                        payload["events"].append({"kind": "thinking", "message": event.message, "data": event_data})
                elif event.kind in {"tool_call", "tool_result", "tool_failed"}:
                    if event.kind in {"tool_failed"}:
                        continue
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
            if segments:
                payload["segments"] = sorted(segments.values(), key=lambda item: int(item.get("sequence") or 0))
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
        segments: dict[str, dict] = {}
        if record["role"] == "assistant":
            for event in events:
                if event.kind == "run_segment":
                    segment_id = str(event.data.get("segment_id") or "")
                    if segment_id:
                        segments[segment_id] = dict(event.data)
                    continue
                if event.kind in {
                    "thinking",
                    "thinking_start",
                    "thinking_delta",
                    "thinking_end",
                    "tool_call",
                    "tool_result",
                    "tool_failed",
                }:
                    if event.kind in {"tool_failed"}:
                        continue
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
            if segments:
                payload["segments"] = sorted(segments.values(), key=lambda item: int(item.get("sequence") or 0))
            if any(event.kind == "run_started" for event in events) and not any(
                event.kind == "run_finished" for event in events
            ):
                payload["running"] = True
        result.append(payload)
    return result


def _branch_session(store, source, body: BranchRequest, *, rewind: bool):
    title = body.title or (source.title if rewind else f"{source.title}（分支）")
    client_id = body.client_id
    # A fork title is always a locked custom title.  A rewind inherits the
    # source's provenance unless the caller supplied a genuinely new title;
    # the Web client echoes the source title, so only a different value (or an
    # explicit None) keeps automatic naming alive on the branch.
    title_is_custom = (
        None
        if not rewind
        else (source.title_is_custom if not body.title or normalize_session_title(body.title) == source.title else True)
    )
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
                    (
                        node
                        for node in candidates
                        if str(getattr(node, "session_id", ""))
                        == (body.source_node_session_id or source.session_id)
                    ),
                    None if body.source_node_session_id else (candidates[0] if candidates else None),
                )
            if source_node is None:
                source_node = store.get_node(body.source_node_session_id or source.session_id, body.source_node_id)
            if source_node is None:
                raise ValueError("指定的 source_node_id 不属于当前会话。")
            target = store.create_session(
                title,
                client_id=client_id,
                local_only=source.local_only,
                root_parent=(source_node.session_id, source_node.id),
                title_is_custom=title_is_custom,
            )
        elif body.run_id:
            records = store.load_conversation_records(source.session_id)
            if not any(str(record["run_id"]) == body.run_id for record in records):
                raise ValueError("指定的 run 不属于当前会话。")
            target = store.fork_run(body.run_id)
            target = store.rename_session(target.session_id, title, title_is_custom=title_is_custom)
            if client_id:
                target = store.set_client_id(target.session_id, client_id)
        else:
            target = store.import_conversation(
                title,
                [message.model_dump() for message in body.fallback_messages],
                client_id=client_id,
                force_new=rewind,
                local_only=source.local_only,
                title_is_custom=title_is_custom,
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
    if _has_active_execution(state, identity.id, session_id):
        raise HTTPException(status_code=409, detail="会话已有正在运行的任务，请先停止。")
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
    """Resolve a same-session rewind parent without mutating the session."""
    state: WebAppState = request.app.state.web
    store = _store(state, identity.id)
    source = _require_branchable(store, session_id)
    if _has_active_execution(state, identity.id, session_id):
        raise HTTPException(status_code=409, detail="会话已有正在运行的任务，请先停止。")
    _require_session_workspace(state, identity.id, source.session_id)
    if not body.source_node_id:
        raise HTTPException(status_code=422, detail="rewind 必须提供 source_node_id")
    target_session_id = body.source_node_session_id or source.session_id
    target = store.get_node(target_session_id, body.source_node_id)
    if target is None:
        raise HTTPException(status_code=409, detail="source_node_id 不属于当前会话祖先树")
    loaded = getattr(store, "load_nodes", lambda _sid: [])(source.session_id)
    if target.key not in {getattr(item, "key", None) for item in loaded}:
        raise HTTPException(status_code=409, detail="source_node_id 不属于当前会话祖先树")
    # The protocol names the selected message as the rewind target. Resolve its
    # parent with the complete cross-session key before returning the branch
    # anchor. Older clients sent the parent directly; accepting a root here is
    # harmless and keeps those clients on the same-session branch path.
    if target.data_type == "root":
        # A legacy client may already have sent the desired root parent.
        parent = target
    elif target.parent_id or target.parent_session_id:
        if not target.parent_id or not target.parent_session_id:
            raise HTTPException(status_code=409, detail="目标节点的父引用不完整")
        parent = store.get_node(target.parent_session_id, target.parent_id)
        if parent is None:
            raise HTTPException(status_code=409, detail="目标节点的父节点不存在")
    else:
        parent = target
    payload = _summary_for_user(state, identity.id, store.get_session_summary(source.session_id))
    payload["rewind_source_node_id"] = parent.id
    payload["rewind_source_session_id"] = parent.session_id
    payload["branch"] = True
    return payload


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
