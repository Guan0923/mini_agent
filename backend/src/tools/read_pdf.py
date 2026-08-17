"""Read-only PDF extraction tool registration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .base import Tool
from .default_tools.schema import object_schema

if TYPE_CHECKING:
    from backend.rag import PdfExtractionResult, PdfExtractor


def read_pdf_tool(workspace: Path, *, extractor: PdfExtractor | None = None) -> Tool:
    """Create the read-only ``read_pdf`` tool for one workspace.

    ``backend.rag`` is imported lazily to keep the tool catalog import cycle-free:
    the rag package imports low-level tools helpers, which would otherwise reach
    back into this module while ``backend.rag`` is still initializing.
    """

    from backend.rag import PdfExtractor

    pdf = extractor or PdfExtractor(workspace)

    def handle(
        path: str,
        start_page: int = 1,
        max_pages: int = 5,
        start_char: int = 1,
        max_chars: int = 20_000,
    ) -> str:
        result = pdf.extract(path, start_page=start_page, max_pages=max_pages)
        return format_read_pdf_result(result, start_char=start_char, max_chars=max_chars)

    return Tool(
        "read_pdf",
        (
            "Extracts text from a PDF file inside the workspace, classifying each page as "
            "text (born-digital), image (scanned; OCR runs automatically), or ocred (a scan "
            "with an existing text layer). Returns the document kind, total page count, "
            "per-page kinds, and page text. Page through long documents with start_page and "
            "max_pages; resume long extractions at an exact character offset with start_char "
            "and max_chars."
        ),
        handle,
        object_schema(
            {
                "path": {"type": "string", "minLength": 1, "description": "Workspace-relative .pdf file path."},
                "start_page": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "One-based first page to extract.",
                },
                "max_pages": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                    "description": "Maximum number of pages to extract in one call.",
                },
                "start_char": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "One-based character offset into the combined page text.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20_000,
                    "default": 20_000,
                    "description": "Maximum characters of combined page text to return.",
                },
            },
            ["path"],
        ),
    )


def format_read_pdf_result(result: PdfExtractionResult, *, start_char: int, max_chars: int) -> str:
    """Render one extraction result with a bounded, resumable character window.

    The header (document kind, total pages, per-page kinds) is always shown in
    full. Character offsets apply to the combined page text only, so continuing
    with the reported ``start_char`` always resumes at exactly the next
    character after a truncation.
    """
    header = "\n".join(
        (
            f"document kind: {result.kind}",
            f"total pages: {result.total_pages}",
            "page kinds: " + " ".join(f"{page.number}:{page.kind}" for page in result.pages),
        )
    )
    page_sections = [f"--- Page {page.number} ({page.kind}) ---\n{page.text}" for page in result.pages]
    body = "\n\n".join(page_sections)
    available = body[start_char - 1 :]
    if not available:
        return f"{header}\n(no more text.)"
    window = available[:max_chars]
    lines = [header, window]
    if len(available) > max_chars:
        lines.append(
            f"... output truncated at {max_chars} characters; continue with start_char={start_char + len(window)}."
        )
    return "\n\n".join(lines)
