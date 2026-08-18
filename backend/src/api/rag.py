"""Knowledge-base capability and search endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.jobs import AdmissionPolicy, JobLane, JobScopeKind, ThreadJob
from backend.rag import EmbeddingProfile, KnowledgeBaseService, PdfExtractor

from .auth.dependencies import require_user
from .auth.types import UserIdentity
from .session_files.store import SessionFileError, SessionFileStore
from .sessions.routes import _require_active, _store
from .user_data import user_paths

router = APIRouter(prefix="/api/rag", tags=["rag"])


class RagImportRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    source: Literal["project", "upload"]
    path: str = Field(min_length=1, max_length=2000)


def _section_for_session(request: Request, identity: UserIdentity, session_id: str):
    project = request.app.state.web.projects(identity.id).session_project(session_id, include_removed=False)
    service = _service(request, identity)
    if project is not None:
        return service.ensure_section(identity.id, project_id=project.project_id, display_name=project.name)
    return service.ensure_section(identity.id, session_id=session_id)


def _service(request: Request, identity: UserIdentity) -> KnowledgeBaseService:
    paths = user_paths(request.app.state.web.data_root, identity.id)
    return KnowledgeBaseService(paths.root)


@router.get("/capabilities")
def capabilities(request: Request, identity: UserIdentity = Depends(require_user)) -> dict[str, object]:
    settings = request.app.state.web.settings.rag_config_for_user(identity.id)
    profile = EmbeddingProfile.create(
        base_url=str(settings["embedding_base_url"]), model=str(settings["embedding_model"])
    )
    return _service(request, identity).capabilities(profile)


@router.post("/documents/import")
def import_document(
    request: Request, body: RagImportRequest, identity: UserIdentity = Depends(require_user)
) -> dict[str, object]:
    state = request.app.state.web
    store = _store(state, identity.id)
    _require_active(store, body.session_id)
    paths = state.user_paths(identity.id)
    paths.ensure_session(body.session_id)
    project_root = state.session_workspace(identity.id, body.session_id)
    file_store = SessionFileStore(paths, body.session_id, project_root=project_root)
    try:
        source_path = file_store.resolve(body.source, body.path)
    except SessionFileError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if source_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=422, detail="知识库只支持 PDF 文件")
    settings = state.settings.rag_config_for_user(identity.id)
    profile = EmbeddingProfile.create(
        base_url=str(settings["embedding_base_url"]), model=str(settings["embedding_model"])
    )
    service = _service(request, identity)
    section = _section_for_session(request, identity, body.session_id)
    document, ingestion, duplicate = service.import_document(
        source_path, user_id=identity.id, section_id=section.section_id, profile=profile, source=body.source
    )
    job_id: str | None = None
    job_registry = getattr(state, "job_registry", None)
    if job_registry is not None:
        parent_scope = getattr(state, "system_job_scope", job_registry.root_scope())
        job_scope = parent_scope.child(JobScopeKind.USER, user_id=identity.id, session_id=body.session_id)
        job = ThreadJob(
            job_registry.new_job_id(),
            service.index_document,
            kwargs={
                "document_id": document.document_id,
                "profile": profile,
                "extractor": PdfExtractor(paths.root),
            },
        )
        job_registry.submit(job, scope=job_scope, lane=JobLane.BACKGROUND, admission=AdmissionPolicy())
        job_id = job.info().id
    return {
        "document": document.__dict__,
        "ingestion": ingestion.__dict__,
        "duplicate": duplicate,
        "section": section.__dict__,
        "job_id": job_id,
    }


@router.get("/sections/{section_id}/documents")
def list_documents(
    section_id: str, request: Request, identity: UserIdentity = Depends(require_user)
) -> list[dict[str, object]]:
    service = _service(request, identity)
    settings = request.app.state.web.settings.rag_config_for_user(identity.id)
    profile = EmbeddingProfile.create(
        base_url=str(settings["embedding_base_url"]), model=str(settings["embedding_model"])
    )
    return service.list_documents(user_id=identity.id, section_id=section_id, profile=profile)
