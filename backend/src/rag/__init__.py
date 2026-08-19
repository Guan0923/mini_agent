"""Bounded PDF text extraction for the agent runtime (rag groundwork)."""

from .chunking import chunk_pdf_pages, pretokenize, token_count
from .extractor import PdfExtractor
from .models import (
    DocumentIngestion,
    EmbeddingProfile,
    KnowledgeBaseSection,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
    PdfDocumentKind,
    PdfExtractionResult,
    PdfPage,
    PdfPageKind,
    RagAlgorithm,
    RagDocumentStatus,
    RagSectionType,
)
from .ocr import OcrEngine, OcrImage, OcrLine, OcrResult, RapidOcrEngine
from .service import KnowledgeBaseService, RagBusyError, RagDependencyError, RagNotFoundError
from .tool import knowledge_base_search_tool

__all__ = [
    "OcrEngine",
    "OcrImage",
    "OcrLine",
    "OcrResult",
    "PdfDocumentKind",
    "PdfExtractionResult",
    "PdfExtractor",
    "PdfPage",
    "PdfPageKind",
    "RapidOcrEngine",
    "chunk_pdf_pages",
    "pretokenize",
    "token_count",
    "DocumentIngestion",
    "EmbeddingProfile",
    "KnowledgeBaseSection",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeSearchResponse",
    "KnowledgeSearchResult",
    "RagAlgorithm",
    "RagDocumentStatus",
    "RagSectionType",
    "KnowledgeBaseService",
    "RagDependencyError",
    "RagBusyError",
    "RagNotFoundError",
    "knowledge_base_search_tool",
]
