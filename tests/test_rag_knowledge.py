from __future__ import annotations

from pathlib import Path

from backend.rag import EmbeddingProfile, KnowledgeBaseService, chunk_pdf_pages
from backend.rag.models import KnowledgeSearchResult, PdfPage


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
