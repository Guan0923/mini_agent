"""Workspace-confined PDF extraction with per-page classification."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

import pymupdf as fitz

from backend.tools.base import ToolError
from backend.tools.filesystem.paths import workspace_relative_parts

from .models import PdfExtractionResult, PdfPage
from .ocr import OcrEngine, OcrImage, RapidOcrEngine


def _validate_integer(name: str, value: Any, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{name} must be an integer.")
    if value < minimum:
        raise ToolError(f"{name} must be at least {minimum}.")


def normalize_text(text: str) -> str:
    """Normalize line endings and unicode in extracted PDF text."""
    return unicodedata.normalize("NFKC", text.replace("\r\n", "\n").replace("\r", "\n"))


class PdfExtractor:
    """Extract bounded text from one workspace-confined PDF file.

    Pages are classified per page: ``text`` (valid text layer, image coverage
    below the threshold), ``ocred`` (valid text layer over a dominant scan
    image), or ``image`` (no text layer; rendered at 200 DPI and OCRed).
    Blank pages carry no content and do not affect the document-level kind.
    """

    MAX_FILE_BYTES = 100 * 1024 * 1024
    MAX_PAGES = 2_000
    MAX_PAGE_MEGAPIXELS = 20.0
    OCR_DPI = 200
    IMAGE_COVERAGE_THRESHOLD = 0.70

    def __init__(self, workspace: Path, *, ocr: OcrEngine | None = None) -> None:
        self._workspace = workspace.resolve()
        self._ocr = ocr or RapidOcrEngine()

    def extract(self, path: str, start_page: int = 1, max_pages: int | None = None) -> PdfExtractionResult:
        """Extract pages ``[start_page, start_page + max_pages)`` from one PDF.

        ``max_pages=None`` reads to the end of the document. The workspace is
        the confinement boundary: only a regular ``.pdf`` file inside it may be
        opened, encrypted, corrupt, oversized, or over-large files are rejected.
        """
        self._validate_page_arguments(start_page, max_pages)
        pdf_path = self._resolve_pdf_path(path)
        try:
            document = fitz.open(pdf_path)
        except Exception as exc:
            raise ToolError(f"Unable to open PDF {path}: {exc}") from exc
        try:
            return self._extract_document(document, path, start_page, max_pages)
        finally:
            document.close()

    def _extract_document(
        self,
        document: fitz.Document,
        path: str,
        start_page: int,
        max_pages: int | None,
    ) -> PdfExtractionResult:
        if document.needs_pass:
            raise ToolError(f"PDF is encrypted and password-protected files are not supported: {path}")
        total_pages = document.page_count
        if total_pages > self.MAX_PAGES:
            raise ToolError(f"PDF exceeds the {self.MAX_PAGES} page limit: {total_pages} pages")
        if start_page > total_pages:
            raise ToolError(f"start_page {start_page} is beyond the document's {total_pages} pages")

        last = min(start_page + ((max_pages or total_pages) - 1), total_pages)
        warnings: list[str] = []
        if last < total_pages:
            warnings.append(
                f"Extraction limited to pages {start_page}-{last} of {total_pages}; increase max_pages to read more."
            )

        pages: list[PdfPage] = []
        for number in range(start_page, last + 1):
            page, page_warnings = self._extract_page(document, number)
            pages.append(page)
            warnings.extend(page_warnings)

        metadata = {key: value for key, value in document.metadata.items() if value}
        return PdfExtractionResult(
            kind=self._document_kind(pages),
            total_pages=total_pages,
            metadata=metadata,
            pages=tuple(pages),
            warnings=tuple(warnings),
        )

    def _extract_page(self, document: fitz.Document, number: int) -> tuple[PdfPage, tuple[str, ...]]:
        page = document.load_page(number - 1)
        text = normalize_text(page.get_text("text"))
        coverage = self._dominant_image_coverage(page)
        if text.strip():
            kind = "ocred" if coverage >= self.IMAGE_COVERAGE_THRESHOLD else "text"
            return PdfPage(number, kind, text, is_blank=False, image_coverage=coverage), ()
        if coverage > 0 or page.get_drawings():
            return self._ocr_image_page(page, number, coverage)
        return PdfPage(number, "text", "", is_blank=True, image_coverage=0.0), ()

    def _ocr_image_page(self, page: fitz.Page, number: int, coverage: float) -> tuple[PdfPage, tuple[str, ...]]:
        limit = self.MAX_PAGE_MEGAPIXELS
        zoom = self.OCR_DPI / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, colorspace=fitz.csRGB)
        if pixmap.width * pixmap.height > limit * 1_000_000:
            return (
                PdfPage(number, "image", "", is_blank=False, image_coverage=coverage),
                (f"Skipped OCR on page {number}: rendered page exceeds the {limit} megapixel limit.",),
            )
        image = OcrImage(bytes(pixmap.samples), pixmap.width, pixmap.height)
        try:
            result = self._ocr.recognize(image)
        except Exception as exc:
            return (
                PdfPage(number, "image", "", is_blank=False, image_coverage=coverage),
                (f"OCR failed on page {number}: {exc}",),
            )
        if not result.lines:
            return (
                PdfPage(number, "image", "", is_blank=False, image_coverage=coverage, ocr_confidence=0.0),
                (f"OCR found no text on page {number}.",),
            )
        text = normalize_text("\n".join(line.text for line in result.lines))
        return (
            PdfPage(
                number,
                "image",
                text,
                is_blank=False,
                image_coverage=coverage,
                ocr_confidence=result.confidence,
            ),
            (),
        )

    @staticmethod
    def _dominant_image_coverage(page: fitz.Page) -> float:
        """Return the largest single image's clipped share of the page area."""
        page_rect = page.rect
        page_area = max(page_rect.width * page_rect.height, 1e-9)
        best = 0.0
        for info in page.get_image_info():
            bbox = fitz.Rect(info["bbox"])
            intersection = bbox & page_rect
            best = max(best, intersection.get_area() / page_area)
        return best

    @staticmethod
    def _document_kind(pages: list[PdfPage]) -> str:
        """Summarize non-blank page kinds; multiple kinds aggregate to mixed."""
        kinds = {page.kind for page in pages if not page.is_blank}
        if len(kinds) > 1:
            return "mixed"
        if kinds == {"ocred"}:
            return "ocred"
        if kinds == {"image"}:
            return "image"
        return "text"

    def _resolve_pdf_path(self, path: str) -> Path:
        parts = workspace_relative_parts(path)
        pdf_path = self._workspace.joinpath(*parts).resolve()
        if pdf_path != self._workspace and self._workspace not in pdf_path.parents:
            raise ToolError("Path must stay inside the workspace.")
        if not pdf_path.is_file():
            raise ToolError(f"Not a file: {path}")
        if pdf_path.suffix.lower() != ".pdf":
            raise ToolError(f"Path is not a PDF file: {path}")
        if pdf_path.stat().st_size > self.MAX_FILE_BYTES:
            raise ToolError(f"PDF exceeds the {self.MAX_FILE_BYTES} byte limit: {path}")
        return pdf_path

    @staticmethod
    def _validate_page_arguments(start_page: int, max_pages: int | None) -> None:
        _validate_integer("start_page", start_page, minimum=1)
        if max_pages is not None:
            _validate_integer("max_pages", max_pages, minimum=1)
