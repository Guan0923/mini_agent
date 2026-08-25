"""Knowledge-base capability and search endpoints."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from backend.jobs import AdmissionPolicy, JobLane, JobScopeKind, ThreadJob
from backend.rag import (
    EmbeddingProfile,
    KnowledgeBaseService,
    PdfExtractor,
    RagBusyError,
    RagNotFoundError,
)

from .auth.dependencies import require_user
from .auth.types import UserIdentity
from .session_files.store import MAX_FILE_BYTES, SessionFileError, SessionFileStore
from .session_store import require_active_session, session_store
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


def _profile(request: Request, identity: UserIdentity) -> EmbeddingProfile:
    settings = request.app.state.web.settings.rag_config_for_user(identity.id)
    return EmbeddingProfile.create(
        base_url=str(settings["embedding_base_url"]),
        model=str(settings["embedding_model"]),
    )


def _submit_index_job(
    request: Request,
    identity: UserIdentity,
    service: KnowledgeBaseService,
    document_id: str,
    profile: EmbeddingProfile,
    *,
    session_id: str | None = None,
) -> str | None:
    state = request.app.state.web
    registry = getattr(state, "job_registry", None)
    if registry is None:
        return None
    paths = state.user_paths(identity.id)
    parent_scope = getattr(state, "system_job_scope", registry.root_scope())
    job_scope = parent_scope.child(JobScopeKind.USER, user_id=identity.id, session_id=session_id)
    job = ThreadJob(
        registry.new_job_id(),
        service.index_document,
        kwargs={
            "document_id": document_id,
            "profile": profile,
            "extractor": PdfExtractor(paths.root),
        },
    )
    registry.submit(job, scope=job_scope, lane=JobLane.BACKGROUND, admission=AdmissionPolicy())
    return job.info().id


@router.get("/capabilities")
def capabilities(request: Request, identity: UserIdentity = Depends(require_user)) -> dict[str, object]:
    return _service(request, identity).capabilities(_profile(request, identity))


@router.post("/documents/import")
def import_document(
    request: Request, body: RagImportRequest, identity: UserIdentity = Depends(require_user)
) -> dict[str, object]:
    state = request.app.state.web
    store = session_store(state, identity.id)
    require_active_session(store, body.session_id)
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
    profile = _profile(request, identity)
    service = _service(request, identity)
    section = _section_for_session(request, identity, body.session_id)
    document, ingestion, duplicate = service.import_document(
        source_path, user_id=identity.id, section_id=section.section_id, profile=profile, source=body.source
    )
    job_id = _submit_index_job(
        request,
        identity,
        service,
        document.document_id,
        profile,
        session_id=body.session_id,
    )
    return {
        "document": document.__dict__,
        "ingestion": ingestion.__dict__,
        "duplicate": duplicate,
        "section": section.__dict__,
        "job_id": job_id,
    }


@router.post("/documents/upload", status_code=202)
async def upload_document(
    request: Request,
    section_id: str = Form(min_length=1, max_length=128),
    file: UploadFile = File(...),
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    """Upload one bounded PDF directly into an existing RAG section."""

    service = _service(request, identity)
    try:
        section = service.get_section(section_id, user_id=identity.id)
    except RagNotFoundError as exc:
        raise HTTPException(status_code=404, detail="知识库分区不存在。") from exc
    filename = SessionFileStore.sanitize_name(file.filename or "document.pdf")
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=422, detail="知识库只支持 PDF 文件")
    paths = request.app.state.web.user_paths(identity.id)
    paths.rag_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".rag-upload-", dir=paths.rag_dir) as temporary:
            source_path = Path(temporary) / filename
            total = 0
            with source_path.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"单文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB 限制。",
                        )
                    handle.write(chunk)
            if total == 0:
                raise HTTPException(status_code=422, detail="PDF 文件不能为空。")
            profile = _profile(request, identity)
            document, ingestion, duplicate = service.import_document(
                source_path,
                user_id=identity.id,
                section_id=section.section_id,
                profile=profile,
                source="knowledge_base",
            )
    finally:
        await file.close()
    job_id = _submit_index_job(
        request,
        identity,
        service,
        document.document_id,
        profile,
        session_id=section.session_id,
    )
    return {
        "document": document.__dict__,
        "ingestion": ingestion.__dict__,
        "duplicate": duplicate,
        "section": section.__dict__,
        "job_id": job_id,
    }


@router.post("/documents/{document_id}/reindex", status_code=202)
def reindex_document(
    document_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    service = _service(request, identity)
    profile = _profile(request, identity)
    try:
        document, ingestion = service.queue_document(
            document_id,
            user_id=identity.id,
            profile=profile,
        )
        section = service.get_section(document.section_id, user_id=identity.id)
    except RagNotFoundError as exc:
        raise HTTPException(status_code=404, detail="知识库文件不存在。") from exc
    except RagBusyError as exc:
        raise HTTPException(status_code=409, detail="文件正在索引，不能重复提交。") from exc
    job_id = _submit_index_job(
        request,
        identity,
        service,
        document.document_id,
        profile,
        session_id=section.session_id,
    )
    return {
        "document": document.__dict__,
        "ingestion": ingestion.__dict__,
        "job_id": job_id,
    }


@router.delete("/documents/{document_id}")
def delete_document(
    document_id: str,
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> dict[str, object]:
    service = _service(request, identity)
    try:
        document, warning = service.delete_document(document_id, user_id=identity.id)
    except RagNotFoundError as exc:
        raise HTTPException(status_code=404, detail="知识库文件不存在。") from exc
    except RagBusyError as exc:
        raise HTTPException(status_code=409, detail="文件正在索引，不能删除。") from exc
    return {"deleted": document.document_id, "warning": warning}


@router.get("/tree")
def knowledge_base_tree(
    request: Request,
    identity: UserIdentity = Depends(require_user),
) -> list[dict[str, object]]:
    """Return the authenticated user's knowledge-base sections and files."""

    service = _service(request, identity)
    profile = _profile(request, identity)
    return [
        {
            "section": section.__dict__,
            "documents": service.list_documents(
                user_id=identity.id,
                section_id=section.section_id,
                profile=profile,
            ),
        }
        for section in service.list_sections(user_id=identity.id)
    ]


@router.get("/sections/{section_id}/documents")
def list_documents(
    section_id: str, request: Request, identity: UserIdentity = Depends(require_user)
) -> list[dict[str, object]]:
    service = _service(request, identity)
    profile = _profile(request, identity)
    return service.list_documents(user_id=identity.id, section_id=section_id, profile=profile)
