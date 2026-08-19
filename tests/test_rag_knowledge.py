from __future__ import annotations

from pathlib import Path

import pytest

from backend.rag import EmbeddingProfile, KnowledgeBaseService, chunk_pdf_pages
from backend.rag.models import KnowledgeSearchResult, PdfPage
from backend.rag.service import RagBusyError, RagNotFoundError


def test_chunker_preserves_page_ranges_and_overlap() -> None:
    pages = [
        PdfPage(1, "text", "alpha beta gamma delta"),
        PdfPage(2, "text", "epsilon zeta eta theta"),
    ]
    chunks = chunk_pdf_pages(pages, target_tokens=4, overlap_tokens=1)
    assert chunks
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 2
    assert chunks[0].token_count <= 4
    assert chunks[0].text.split()[-1] == chunks[1].text.split()[0]


def test_import_duplicate_and_bm25_search(tmp_path: Path) -> None:
    service = KnowledgeBaseService(tmp_path)
    section = service.ensure_section("user", session_id="session")
    source = tmp_path / "contract.pdf"
    source.write_bytes(b"pdf-content")
    profile = EmbeddingProfile.create(model="bge-m3")
    document, ingestion, duplicate = service.import_document(
        source,
        user_id="user",
        section_id=section.section_id,
        profile=profile,
    )
    assert document.status == "queued"
    assert ingestion.embedding_profile_id == profile.profile_id
    assert duplicate is False
    _, _, duplicate_again = service.import_document(
        source,
        user_id="user",
        section_id=section.section_id,
        profile=profile,
    )
    assert duplicate_again is True
    service.index_extraction(
        document.document_id,
        profile,
        [PdfPage(1, "text", "alpha contract clause")],
        embed=False,
    )
    result = service.search(
        "contract",
        user_id="user",
        section_id=section.section_id,
        profile=profile,
        algorithm="bm25",
    )
    assert result.results and result.results[0].document_id == document.document_id
    service.index_extraction(
        document.document_id,
        profile,
        [PdfPage(1, "text", "合同文本中的付款条款")],
        embed=False,
    )
    assert service.search(
        "付款", user_id="user", section_id=section.section_id, profile=profile, algorithm="bm25"
    ).results


def test_hybrid_uses_rrf_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    service = KnowledgeBaseService(tmp_path)
    section = service.ensure_section("user", session_id="session")
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"pdf-content")
    profile = EmbeddingProfile.create()
    document, _, _ = service.import_document(source, user_id="user", section_id=section.section_id, profile=profile)
    service.index_extraction(document.document_id, profile, [PdfPage(1, "text", "shared alpha")], embed=False)
    with service._connection() as db:
        row = db.execute(
            "SELECT chunk_id, document_id, filename, text, page_start, page_end FROM knowledge_chunks JOIN documents USING(document_id)"
        ).fetchone()
    item = KnowledgeSearchResult(
        row["chunk_id"],
        row["document_id"],
        row["filename"],
        row["text"],
        row["page_start"],
        row["page_end"],
        0.9,
        "vector",
        1,
    )
    monkeypatch.setattr(service, "_vector", lambda *args, **kwargs: [item])
    result = service.search(
        "shared", user_id="user", section_id=section.section_id, profile=profile, algorithm="hybrid"
    )
    assert len(result.results) == 1
    assert result.results[0].source == "hybrid"


def test_profile_status_and_reindex_keep_fts_in_sync(tmp_path: Path) -> None:
    service = KnowledgeBaseService(tmp_path)
    section = service.ensure_section("user", session_id="session")
    source = tmp_path / "notes.pdf"
    source.write_bytes(b"pdf-content")
    first = EmbeddingProfile.create(model="bge-m3")
    second = EmbeddingProfile.create(model="other-embedding")
    document, _, _ = service.import_document(source, user_id="user", section_id=section.section_id, profile=first)
    service.index_extraction(document.document_id, first, [PdfPage(1, "text", "first unique phrase")], embed=False)
    assert (
        service.list_documents(user_id="user", section_id=section.section_id, profile=second)[0]["status"]
        == "not_imported"
    )
    service.import_document(source, user_id="user", section_id=section.section_id, profile=second)
    service.index_extraction(document.document_id, second, [PdfPage(1, "text", "second unique phrase")], embed=False)
    assert (
        service.search("first", user_id="user", section_id=section.section_id, profile=second, algorithm="bm25").results
        == ()
    )
    assert service.search(
        "second", user_id="user", section_id=section.section_id, profile=second, algorithm="bm25"
    ).results


class _DeleteResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None


class _DeleteSession:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise RuntimeError("qdrant unavailable")
        return _DeleteResponse()


def test_delete_document_removes_managed_copy_search_rows_and_vectors(tmp_path: Path) -> None:
    http = _DeleteSession()
    service = KnowledgeBaseService(tmp_path, session=http)  # type: ignore[arg-type]
    section = service.ensure_section("user", session_id="session")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf-content")
    profile = EmbeddingProfile.create()
    document, _, _ = service.import_document(
        source,
        user_id="user",
        section_id=section.section_id,
        profile=profile,
    )
    service.index_extraction(
        document.document_id,
        profile,
        [PdfPage(1, "text", "deletion marker")],
        embed=False,
    )
    managed = service.rag_dir / document.relative_path
    assert managed.is_file()

    deleted, warning = service.delete_document(document.document_id, user_id="user")

    assert deleted.document_id == document.document_id
    assert warning is None
    assert source.is_file()
    assert not managed.exists()
    assert not service.search(
        "deletion",
        user_id="user",
        section_id=section.section_id,
        profile=profile,
        algorithm="bm25",
    ).results
    with pytest.raises(RagNotFoundError):
        service.get_document(document.document_id, user_id="user")
    assert http.calls[0][0].endswith(f"/collections/rag_{profile.profile_id}/points/delete")


def test_delete_document_is_blocked_while_queued_and_is_user_scoped(tmp_path: Path) -> None:
    service = KnowledgeBaseService(tmp_path)
    section = service.ensure_section("user", session_id="session")
    source = tmp_path / "queued.pdf"
    source.write_bytes(b"pdf-content")
    profile = EmbeddingProfile.create()
    document, _, _ = service.import_document(
        source,
        user_id="user",
        section_id=section.section_id,
        profile=profile,
    )

    with pytest.raises(RagBusyError):
        service.delete_document(document.document_id, user_id="user")
    with pytest.raises(RagNotFoundError):
        service.delete_document(document.document_id, user_id="other")


def test_delete_document_keeps_sqlite_authoritative_when_qdrant_cleanup_fails(tmp_path: Path) -> None:
    service = KnowledgeBaseService(tmp_path, session=_DeleteSession(fail=True))  # type: ignore[arg-type]
    section = service.ensure_section("user", session_id="session")
    source = tmp_path / "cleanup.pdf"
    source.write_bytes(b"pdf-content")
    profile = EmbeddingProfile.create()
    document, _, _ = service.import_document(
        source,
        user_id="user",
        section_id=section.section_id,
        profile=profile,
    )
    service.index_extraction(document.document_id, profile, [PdfPage(1, "text", "cleanup")], embed=False)

    _, warning = service.delete_document(document.document_id, user_id="user")

    assert warning is not None and "qdrant unavailable" in warning
    with pytest.raises(RagNotFoundError):
        service.get_document(document.document_id, user_id="user")


def test_queue_document_uses_selected_profile_and_rejects_active_work(tmp_path: Path) -> None:
    service = KnowledgeBaseService(tmp_path)
    section = service.ensure_section("user", session_id="session")
    source = tmp_path / "reindex.pdf"
    source.write_bytes(b"pdf-content")
    first = EmbeddingProfile.create(model="first")
    second = EmbeddingProfile.create(model="second")
    document, _, _ = service.import_document(
        source,
        user_id="user",
        section_id=section.section_id,
        profile=first,
    )
    service.index_extraction(document.document_id, first, [PdfPage(1, "text", "ready")], embed=False)

    queued, ingestion = service.queue_document(
        document.document_id,
        user_id="user",
        profile=second,
    )

    assert queued.status == "queued"
    assert ingestion.embedding_profile_id == second.profile_id
    with pytest.raises(RagBusyError):
        service.queue_document(document.document_id, user_id="user", profile=second)
