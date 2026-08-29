"""Authenticated Memory inspection, automation, and management routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool

from backend.domain.memory import (
    MemoryEvidence,
    MemoryItem,
    MemoryJob,
    MemoryJobStatus,
    MemoryKind,
    MemorySettings,
)
from backend.runtime.memory import MemoryContextSelector
from backend.storage.memory import (
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryStorageError,
    MemoryStore,
)

from .auth.dependencies import require_user
from .auth.types import UserIdentity

router = APIRouter(prefix="/api/internal/memory", tags=["internal-memory"])


class ExtractRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=200)


class ConsolidateRequest(BaseModel):
    project_id: str | None = Field(default=None, max_length=200)


class ItemEnabledRequest(BaseModel):
    enabled: StrictBool


class ClearRequest(BaseModel):
    confirm: str = Field(max_length=100)


@router.get("/items")
def list_memory_items(
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
    project_id: Annotated[str | None, Query(max_length=200)] = None,
    kind: MemoryKind | None = None,
    include_deleted: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, object]:
    _require_project(request, identity.id, project_id)
    store = _store(request, identity.id)
    try:
        if project_id is None:
            accessible_project_ids = [
                project.project_id for project in request.app.state.web.projects(identity.id).list("all")
            ]
            items = store.list_accessible_items(
                accessible_project_ids,
                kinds=(kind,) if kind is not None else (),
                include_deleted=include_deleted,
                limit=limit,
            )
        else:
            items = store.list_items(
                project_id=project_id,
                kinds=(kind,) if kind is not None else (),
                include_deleted=include_deleted,
                limit=limit,
            )
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"items": [_item_payload(item) for item in items], "memory_db_exists": store.exists}


@router.get("/items/{memory_id}/evidence")
def list_memory_evidence(
    memory_id: str,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
    project_id: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, object]:
    _require_project(request, identity.id, project_id)
    store = _store(request, identity.id)
    try:
        item = _scoped_item(request, identity.id, memory_id)
        if project_id is not None and item.project_id is not None and item.project_id != project_id:
            raise HTTPException(status_code=404, detail="Memory item does not exist in this scope.")
        evidence = store.list_evidence(memory_id=memory_id)[:limit]
    except HTTPException:
        raise
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {
        "memory": _item_payload(item),
        "evidence": [_evidence_payload(value) for value in evidence],
    }


@router.get("/retrieval/dry-run")
def memory_retrieval_dry_run(
    query: Annotated[str, Query(min_length=1, max_length=16_384)],
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
    project_id: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, object]:
    """Return exactly what selection would render without changing a model request."""

    _require_project(request, identity.id, project_id)
    settings = MemorySettings.from_mapping(request.app.state.web.settings.memory_config_for_user(identity.id))
    try:
        result = MemoryContextSelector(_store(request, identity.id), settings).select(query, project_id=project_id)
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {
        "dry_run": True,
        "use_memories": settings.use_memories,
        "would_inject": settings.use_memories and bool(result.context),
        "would_inject_if_enabled": bool(result.context),
        "project_id": project_id,
        "context": result.context,
        "context_bytes": result.context_bytes,
        "estimated_tokens": result.estimated_tokens,
        "entries": [
            {
                "memory": _item_payload(entry.item),
                "raw_bm25_rank": entry.raw_bm25_rank,
                "evidence_count": entry.evidence_count,
                "score": entry.scores.to_dict(),
                "selected": entry.selected,
                "reason": entry.reason,
            }
            for entry in result.entries
        ],
    }


@router.get("/retrieval/latest")
def latest_memory_retrieval(
    session_id: Annotated[str, Query(min_length=1, max_length=200)],
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    value = request.app.state.web.memory_diagnostics.latest(identity.id, session_id)
    if value is None:
        raise HTTPException(status_code=404, detail="No Memory retrieval has been recorded for this session.")
    return value


@router.get("/retrieval/history")
def memory_retrieval_history(
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, object]:
    return {"records": request.app.state.web.memory_diagnostics.list_latest(identity.id, limit=limit)}


@router.get("/jobs")
def list_memory_jobs(
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
    status: MemoryJobStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, object]:
    try:
        jobs = _store(request, identity.id).list_jobs(status=status, limit=limit)
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"jobs": [_job_payload(job) for job in jobs]}


@router.post("/extract", status_code=202)
def enqueue_memory_extraction(
    body: ExtractRequest,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    from .session_store import require_session, session_store

    sessions = session_store(request.app.state.web, identity.id)
    require_session(sessions, body.session_id)
    project = request.app.state.web.projects(identity.id).session_project(body.session_id)
    actual_project_id = project.project_id if project is not None and project.removed_at is None else None
    if body.project_id is not None and body.project_id != actual_project_id:
        raise HTTPException(status_code=409, detail="Session is not bound to the requested project.")
    try:
        job = request.app.state.web.memory_automation.enqueue_extract(
            identity.id,
            body.session_id,
            project_id=actual_project_id,
        )
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"job": _job_payload(job)}


@router.post("/consolidate", status_code=202)
def enqueue_memory_consolidation(
    body: ConsolidateRequest,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    _require_project(request, identity.id, body.project_id)
    try:
        job = request.app.state.web.memory_automation.enqueue_consolidate(
            identity.id,
            project_id=body.project_id,
        )
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"job": _job_payload(job)}


@router.post("/jobs/{job_id}/cancel")
def cancel_memory_job(
    job_id: str,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    try:
        job = request.app.state.web.memory_automation.cancel(identity.id, job_id)
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"job": _job_payload(job)}


@router.patch("/items/{memory_id}")
def set_memory_enabled(
    memory_id: str,
    body: ItemEnabledRequest,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    try:
        item = _scoped_item(request, identity.id, memory_id)
        updated = _store(request, identity.id).set_item_enabled(item.memory_id, enabled=body.enabled)
    except HTTPException:
        raise
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"memory": _item_payload(updated)}


@router.delete("/items/{memory_id}")
def delete_memory_item(
    memory_id: str,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    try:
        item = _scoped_item(request, identity.id, memory_id)
        deleted = _store(request, identity.id).delete_item(item.memory_id)
    except HTTPException:
        raise
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"memory": _item_payload(deleted)}


@router.post("/items/{memory_id}/restore")
def restore_memory_item(
    memory_id: str,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    try:
        item = _scoped_item(request, identity.id, memory_id)
        restored = _store(request, identity.id).restore_item(item.memory_id)
    except HTTPException:
        raise
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"memory": _item_payload(restored)}


@router.post("/clear")
def clear_memories(
    body: ClearRequest,
    request: Request,
    identity: Annotated[UserIdentity, Depends(require_user)],
) -> dict[str, object]:
    if body.confirm != "CLEAR ALL MEMORIES":
        raise HTTPException(status_code=422, detail="Clear confirmation text does not match.")
    try:
        request.app.state.web.memory_automation.clear_user(identity.id)
    except (MemoryStorageError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"cleared": True}


def _store(request: Request, user_id: str) -> MemoryStore:
    return MemoryStore(request.app.state.web.user_paths(user_id))


def _require_project(request: Request, user_id: str, project_id: str | None) -> None:
    if project_id is not None and request.app.state.web.projects(user_id).get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project does not exist.")


def _scoped_item(request: Request, user_id: str, memory_id: str) -> MemoryItem:
    item = _store(request, user_id).get_item(memory_id, include_deleted=True)
    if item is None:
        raise HTTPException(status_code=404, detail="Memory item does not exist.")
    if item.project_id is not None and request.app.state.web.projects(user_id).get(item.project_id) is None:
        raise HTTPException(status_code=404, detail="Memory item does not exist in an accessible project.")
    return item


def _item_payload(item: MemoryItem) -> dict[str, object]:
    return {
        "memory_id": item.memory_id,
        "kind": item.kind.value,
        "title": item.title,
        "content": item.content,
        "summary": item.summary,
        "scope": item.scope.value,
        "project_id": item.project_id,
        "confidence": item.confidence,
        "tags": list(item.tags),
        "status": item.status.value,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "last_used_at": item.last_used_at,
        "deleted_at": item.deleted_at,
    }


def _evidence_payload(value: MemoryEvidence) -> dict[str, object]:
    return {
        "evidence_id": value.evidence_id,
        "memory_id": value.memory_id,
        "session_id": value.session_id,
        "turn_id": value.turn_id,
        "excerpt": value.excerpt,
        "source_kind": value.source_kind,
        "content_sha256": value.content_sha256,
        "created_at": value.created_at,
    }


def _job_payload(job: MemoryJob) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "kind": job.kind.value,
        "status": job.status.value,
        "source_id": job.source_id,
        "project_id": job.project_id,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "available_at": job.available_at,
        "lease_expires_at": job.lease_expires_at,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _memory_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, MemoryConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=503, detail="Memory storage is temporarily unavailable.")


__all__ = ["router"]
