from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

import backend.api.rag as rag_routes
from backend.api.app import create_app
from backend.api.state import WebAppState
from backend.rag import EmbeddingProfile, KnowledgeBaseService
from backend.rag.models import PdfPage
from backend.storage.auth import LocalAuthStore


def _client(tmp_path: Path):
    state = WebAppState(tmp_path / "web", auth_repository=LocalAuthStore(tmp_path / "client.db"))
    client = TestClient(create_app(state))
    identity = client.post("/api/auth/guest").json()["user"]
    return state, client, identity


def _section(state: WebAppState, user_id: str, session_id: str = "session"):
    paths = state.user_paths(user_id)
    return KnowledgeBaseService(paths.root).ensure_section(user_id, session_id=session_id)


def test_rag_tree_and_direct_upload(monkeypatch, tmp_path: Path) -> None:
    state, client, identity = _client(tmp_path)
    section = _section(state, identity["id"])
    monkeypatch.setattr(rag_routes, "_submit_index_job", lambda *args, **kwargs: "job-upload")

    response = client.post(
        "/api/rag/documents/upload",
        data={"section_id": section.section_id},
        files={"file": ("guide.pdf", b"pdf-content", "application/pdf")},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["document"]["filename"] == "guide.pdf"
    assert payload["document"]["source"] == "knowledge_base"
    assert payload["job_id"] == "job-upload"
    tree = client.get("/api/rag/tree")
    assert tree.status_code == 200
    assert tree.json()[0]["section"]["section_id"] == section.section_id
    assert tree.json()[0]["documents"][0]["filename"] == "guide.pdf"


def test_direct_upload_validates_type_size_and_section_owner(monkeypatch, tmp_path: Path) -> None:
    state, client, identity = _client(tmp_path)
    section = _section(state, identity["id"])
    other_section = KnowledgeBaseService(state.user_paths(identity["id"]).root).ensure_section(
        str(uuid4()),
        session_id="other-session",
    )
    monkeypatch.setattr(rag_routes, "MAX_FILE_BYTES", 4)

    wrong_type = client.post(
        "/api/rag/documents/upload",
        data={"section_id": section.section_id},
        files={"file": ("notes.txt", b"text", "text/plain")},
    )
    oversized = client.post(
        "/api/rag/documents/upload",
        data={"section_id": section.section_id},
        files={"file": ("large.pdf", b"12345", "application/pdf")},
    )
    forbidden = client.post(
        "/api/rag/documents/upload",
        data={"section_id": other_section.section_id},
        files={"file": ("guide.pdf", b"pdf", "application/pdf")},
    )

    assert wrong_type.status_code == 422
    assert oversized.status_code == 413
    assert forbidden.status_code == 404


def test_delete_and_reindex_are_scoped_and_enforce_busy_state(monkeypatch, tmp_path: Path) -> None:
    state, client, identity = _client(tmp_path)
    section = _section(state, identity["id"])
    service = KnowledgeBaseService(state.user_paths(identity["id"]).root)
    source = tmp_path / "managed.pdf"
    source.write_bytes(b"pdf-content")
    profile = EmbeddingProfile.create()
    document, _, _ = service.import_document(
        source,
        user_id=identity["id"],
        section_id=section.section_id,
        profile=profile,
        source="knowledge_base",
    )
    service.index_extraction(document.document_id, profile, [PdfPage(1, "text", "ready")], embed=False)
    monkeypatch.setattr(rag_routes, "_submit_index_job", lambda *args, **kwargs: "job-reindex")
    monkeypatch.setattr(KnowledgeBaseService, "_delete_document_vectors", lambda *args, **kwargs: None)

    reindex = client.post(f"/api/rag/documents/{document.document_id}/reindex")
    busy_delete = client.delete(f"/api/rag/documents/{document.document_id}")

    assert reindex.status_code == 202
    assert reindex.json()["job_id"] == "job-reindex"
    assert busy_delete.status_code == 409

    service.index_extraction(document.document_id, profile, [PdfPage(1, "text", "ready again")], embed=False)
    deleted = client.delete(f"/api/rag/documents/{document.document_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": document.document_id, "warning": None}
    assert source.is_file()

    missing = client.post(f"/api/rag/documents/{document.document_id}/reindex")
    assert missing.status_code == 404
