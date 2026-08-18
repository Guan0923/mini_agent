"""Data model for bounded PDF extraction results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RagDocumentStatus = Literal["queued", "indexing", "ready", "not_imported", "stale", "failed"]
RagSectionType = Literal["project", "session"]
RagAlgorithm = Literal["bm25", "vector", "hybrid"]

PdfPageKind = Literal["text", "image", "ocred"]
"""Per-page classification: born-digital text, scanned image, or a scan with a text layer."""

PdfDocumentKind = Literal["text", "image", "ocred", "mixed"]
"""Document-level summary of the extracted (non-blank) page kinds."""


@dataclass(frozen=True)
class PdfPage:
    """One extracted page with its classification and text.

    ``kind`` is the page classification. Blank pages (no text, no images, no
    vector content) are reported as ``"text"`` with ``is_blank=True`` and are
    excluded from the document-level kind summary. ``image_coverage`` is the
    fraction of the page covered by its dominant (largest single) image, and
    ``ocr_confidence`` is the mean OCR line confidence when OCR ran, otherwise
    ``None``.
    """

    number: int
    kind: PdfPageKind
    text: str
    is_blank: bool = False
    image_coverage: float = 0.0
    ocr_confidence: float | None = None


@dataclass(frozen=True)
class PdfExtractionResult:
    """The bounded result of one PDF extraction call."""

    kind: PdfDocumentKind
    total_pages: int
    metadata: dict[str, str]
    pages: tuple[PdfPage, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnowledgeBaseSection:
    """A first-level partition of a user's knowledge base."""

    section_id: str
    user_id: str
    section_type: RagSectionType
    project_id: str | None
    session_id: str | None
    display_name: str
    created_at: float


@dataclass(frozen=True)
class KnowledgeDocument:
    """A managed copy of an imported document."""

    document_id: str
    user_id: str
    section_id: str
    filename: str
    relative_path: str
    size_bytes: int
    sha256: str
    status: RagDocumentStatus
    source: str
    created_at: float
    error: str | None = None


@dataclass(frozen=True)
class EmbeddingProfile:
    """The immutable identity of one embedding model configuration."""

    profile_id: str
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "bge-m3"
    dimension: int = 1024

    @classmethod
    def create(
        cls,
        *,
        provider: str = "ollama",
        base_url: str = "http://127.0.0.1:11434",
        model: str = "bge-m3",
        dimension: int = 1024,
    ) -> EmbeddingProfile:
        import hashlib

        identity = f"{provider.strip().lower()}|{base_url.rstrip('/')}|{model.strip()}|{int(dimension)}"
        profile_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        return cls(profile_id, provider.strip().lower(), base_url.rstrip("/"), model.strip(), int(dimension))


@dataclass(frozen=True)
class DocumentIngestion:
    document_id: str
    embedding_profile_id: str
    status: RagDocumentStatus
    created_at: float
    updated_at: float
    error: str | None = None


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    section_id: str
    text: str
    tokenized_text: str
    page_start: int
    page_end: int
    sequence: int
    token_count: int
    sha256: str


@dataclass(frozen=True)
class KnowledgeSearchResult:
    chunk_id: str
    document_id: str
    filename: str
    text: str
    page_start: int
    page_end: int
    score: float
    source: str
    rank: int


@dataclass(frozen=True)
class KnowledgeSearchResponse:
    results: tuple[KnowledgeSearchResult, ...]
    algorithm: RagAlgorithm
    warning: str | None = None
