"""Tests for the read_pdf tool, its visibility, and @pdf reference hints."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pymupdf as fitz
import pytest
from mcp import types

from backend.mcp import McpToolAdapter, create_server
from backend.rag import OcrImage, OcrLine, OcrResult, PdfExtractor
from backend.runtime.conversation.references import FileReferenceExpander
from backend.tools import ToolError, ToolRegistry, WorkspaceFiles, build_tool_registry
from backend.tools.read_pdf import read_pdf_tool


class FakeOcrEngine:
    def __init__(self, lines: list[OcrLine]) -> None:
        self.lines = lines

    def recognize(self, image: OcrImage) -> OcrResult:
        return OcrResult(tuple(self.lines))


def _make_pdf(tmp_path: Path, name: str, text: str = "Hello PDF world") -> Path:
    doc = fitz.open()
    page = doc.new_page()
    if len(text) > 500:
        page.insert_textbox(fitz.Rect(50, 50, page.rect.width - 50, page.rect.height - 50), text, fontsize=10)
    else:
        page.insert_text((72, 72), text, fontsize=14)
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return path


def test_read_pdf_is_registered_in_the_default_registry(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    assert "read_pdf" in registry.names()
    assert "read_pdf" in registry.read_only_names()
    assert registry.is_read_only("read_pdf")
    assert not registry.requires_confirmation("read_pdf")

    spec = next(tool for tool in registry.specs() if tool.name == "read_pdf")
    properties = spec.parameters["properties"]
    assert set(properties) == {"path", "start_page", "max_pages", "start_char", "max_chars"}
    assert properties["path"]["minLength"] == 1
    assert properties["max_pages"]["default"] == 5
    assert properties["max_pages"]["maximum"] == 20
    assert properties["max_chars"]["default"] == 20_000
    assert properties["max_chars"]["maximum"] == 20_000
    assert spec.parameters["required"] == ["path"]


def test_read_pdf_returns_header_and_page_text(tmp_path: Path) -> None:
    _make_pdf(tmp_path, "paper.pdf", "Abstract body text")
    registry = build_tool_registry(tmp_path)

    result = registry.invoke("read_pdf", {"path": "paper.pdf"})

    assert result.startswith("document kind: text\ntotal pages: 1\npage kinds: 1:text")
    assert "--- Page 1 (text) ---" in result
    assert "Abstract body text" in result


def test_read_pdf_injects_fake_ocr_for_image_pages(tmp_path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 90), False)
    pix.set_rect(pix.irect, (30, 120, 200))
    page.insert_image(fitz.Rect(0, 0, page.rect.width, page.rect.height), stream=pix.tobytes("png"))
    doc.save(str(tmp_path / "scan.pdf"))
    doc.close()

    fake = FakeOcrEngine([OcrLine("recognized figure text", 0.85)])
    extractor = PdfExtractor(tmp_path, ocr=fake)
    registry = ToolRegistry((read_pdf_tool(tmp_path, extractor=extractor),))

    result = registry.invoke("read_pdf", {"path": "scan.pdf"})

    assert "document kind: image" in result
    assert "recognized figure text" in result
    assert "--- Page 1 (image) ---" in result


def test_read_pdf_truncates_text_and_offers_accurate_resume(tmp_path: Path) -> None:
    _make_pdf(tmp_path, "long.pdf", "A" * 3_000)
    registry = build_tool_registry(tmp_path)
    result = PdfExtractor(tmp_path).extract("long.pdf")
    body = "\n\n".join(f"--- Page {page.number} ({page.kind}) ---\n{page.text}" for page in result.pages)
    marker = "--- Page 1 (text) ---\n"
    assert body.startswith(marker)

    first = registry.invoke("read_pdf", {"path": "long.pdf", "max_chars": 1_000})
    assert body[:1_000] in first
    assert "continue with start_char=1001" in first

    second = registry.invoke("read_pdf", {"path": "long.pdf", "start_char": 1_001, "max_chars": 1_000})
    assert body[1_000:2_000] in second
    assert "--- Page 1" not in second
    assert "continue with start_char=2001" in second

    resume = registry.invoke("read_pdf", {"path": "long.pdf", "start_char": 1_001, "max_chars": 20_000})
    assert body[1_000:] in resume
    assert "truncated" not in resume


def test_read_pdf_reports_no_more_text_after_document_end(tmp_path: Path) -> None:
    _make_pdf(tmp_path, "tiny.pdf", "short")
    registry = build_tool_registry(tmp_path)

    result = registry.invoke("read_pdf", {"path": "tiny.pdf", "start_char": 10_000})

    assert "no more text" in result
    assert result.startswith("document kind: text")


def test_read_pdf_validates_arguments_through_the_schema(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    for arguments in (
        {"path": "x.pdf", "max_pages": 0},
        {"path": "x.pdf", "max_pages": 21},
        {"path": "x.pdf", "max_chars": 20_001},
        {"path": "x.pdf", "start_char": 0},
        {"path": "x.pdf", "start_page": 0},
    ):
        with pytest.raises(ToolError, match="Invalid arguments"):
            registry.invoke("read_pdf", arguments)


def test_read_pdf_rejects_traversal_paths_without_opening(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    with pytest.raises(ToolError, match="workspace"):
        registry.invoke("read_pdf", {"path": "../outside.pdf"})


def test_read_pdf_is_exposed_through_read_only_mcp_and_plan_mode(tmp_path: Path) -> None:
    registry = build_tool_registry(tmp_path)

    assert "read_pdf" in {spec.name for spec in registry.read_only_specs()}

    adapter = McpToolAdapter(registry)
    assert "read_pdf" in {tool.name for tool in adapter.definitions()}

    server = create_server(tmp_path)
    result = asyncio.run(server.request_handlers[types.ListToolsRequest](types.ListToolsRequest()))
    assert "read_pdf" in {tool.name for tool in result.root.tools}

    invocation = adapter.invoke("read_pdf", {"path": "missing.pdf"})
    assert invocation.isError is True


def test_pdf_reference_expands_to_read_pdf_hint(tmp_path: Path) -> None:
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.7 fake binary content")

    expanded = FileReferenceExpander(WorkspaceFiles(tmp_path)).expand("Read @paper.pdf, please.")

    assert "[Referenced PDF: paper.pdf]" in expanded
    assert "read_pdf" in expanded
    assert "Not valid UTF-8" not in expanded
    assert "Paper" not in expanded


def test_uppercase_pdf_extension_also_becomes_a_hint(tmp_path: Path) -> None:
    (tmp_path / "PAPER.PDF").write_bytes(b"%PDF-1.7 fake")

    expanded = FileReferenceExpander(WorkspaceFiles(tmp_path)).expand("@PAPER.PDF")

    assert "[Referenced PDF: PAPER.PDF]" in expanded


def test_text_reference_still_expands_like_before(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hello world", encoding="utf-8")

    expanded = FileReferenceExpander(WorkspaceFiles(tmp_path)).expand("See @note.txt for details.")

    assert "[Referenced file: note.txt]" in expanded
    assert "hello world" in expanded


def test_non_pdf_binary_reference_still_reports_utf8_error(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe")

    with pytest.raises(ToolError, match="valid UTF-8"):
        FileReferenceExpander(WorkspaceFiles(tmp_path)).expand("@blob.bin")
