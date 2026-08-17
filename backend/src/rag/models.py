"""Data model for bounded PDF extraction results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
