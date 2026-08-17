"""Tests for workspace-confined PDF extraction and page classification."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest

from backend.rag import OcrImage, OcrLine, OcrResult, PdfExtractor
from backend.tools import ToolError


class FakeOcrEngine:
    """Records every call and returns a fixed result or raises."""

    def __init__(self, lines: list[OcrLine] | None = None, *, fail: Exception | None = None) -> None:
        self.calls: list[OcrImage] = []
        self.lines = lines
        self.fail = fail

    def recognize(self, image: OcrImage) -> OcrResult:
        self.calls.append(image)
        if self.fail is not None:
            raise self.fail
        return OcrResult(tuple(self.lines or ()))


def _png_bytes() -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 90), False)
    pix.set_rect(pix.irect, (180, 30, 30))
    return pix.tobytes("png")


def _text_page(doc: fitz.Document, text: str = "Hello Mini-Agent") -> None:
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=14)


def _blank_page(doc: fitz.Document) -> None:
    doc.new_page()


def _image_page(
    doc: fitz.Document,
    *,
    fraction: float = 1.0,
    text_layer: str | None = None,
    text: str | None = None,
) -> None:
    page = doc.new_page()
    page.insert_image(
        fitz.Rect(0, 0, page.rect.width, page.rect.height * fraction),
        stream=_png_bytes(),
        keep_proportion=False,
    )
    if text_layer is not None:
        page.insert_text((72, 72), text_layer, fontsize=14, render_mode=3)
    if text is not None:
        page.insert_text((72, 72), text, fontsize=14)


def _save(doc: fitz.Document, path: Path) -> Path:
    doc.save(str(path))
    doc.close()
    return path


def make_pdf(tmp_path: Path, name: str, build) -> Path:
    doc = fitz.open()
    build(doc)
    return _save(doc, tmp_path / name)


def test_text_document_classifies_all_pages_and_exposes_metadata(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        doc.set_metadata({"title": "Sample Paper", "author": "Ada"})
        _text_page(doc, "First page body")
        _text_page(doc, "Second page body")

    make_pdf(tmp_path, "text.pdf", build)
    fake = FakeOcrEngine([OcrLine("never", 1.0)])

    result = PdfExtractor(tmp_path, ocr=fake).extract("text.pdf")

    assert result.kind == "text"
    assert result.total_pages == 2
    assert result.metadata["title"] == "Sample Paper"
    assert result.metadata["author"] == "Ada"
    assert [page.kind for page in result.pages] == ["text", "text"]
    assert [page.number for page in result.pages] == [1, 2]
    assert "First page body" in result.pages[0].text
    assert "Second page body" in result.pages[1].text
    assert [page.is_blank for page in result.pages] == [False, False]
    assert fake.calls == []
    assert result.warnings == ()


def test_image_document_ocrs_every_image_page(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        _image_page(doc)
        _image_page(doc)

    make_pdf(tmp_path, "scan.pdf", build)
    fake = FakeOcrEngine([OcrLine("scanned line one", 0.8), OcrLine("scanned line two", 0.6)])

    result = PdfExtractor(tmp_path, ocr=fake).extract("scan.pdf")

    assert result.kind == "image"
    assert len(fake.calls) == 2
    assert fake.calls[0].width > 1_000
    assert result.pages[0].kind == "image"
    assert result.pages[0].text == "scanned line one\nscanned line two"
    assert result.pages[0].ocr_confidence == pytest.approx(0.7)
    assert result.pages[0].image_coverage == pytest.approx(1.0)
    assert result.pages[1].ocr_confidence == pytest.approx(0.7)


def test_ocred_page_keeps_text_layer_and_skips_ocr(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        _image_page(doc, text_layer="Embedded text layer")

    make_pdf(tmp_path, "ocred.pdf", build)
    fake = FakeOcrEngine([OcrLine("never", 1.0)])

    result = PdfExtractor(tmp_path, ocr=fake).extract("ocred.pdf")

    assert result.kind == "ocred"
    assert fake.calls == []
    assert result.pages[0].kind == "ocred"
    assert "Embedded text layer" in result.pages[0].text
    assert result.pages[0].ocr_confidence is None
    assert result.pages[0].image_coverage == pytest.approx(1.0)


def test_scan_margins_cover_at_least_seventy_percent(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        _image_page(doc, fraction=0.8, text_layer="Margined layer")

    make_pdf(tmp_path, "margins.pdf", build)
    fake = FakeOcrEngine([OcrLine("never", 1.0)])

    result = PdfExtractor(tmp_path, ocr=fake).extract("margins.pdf")

    assert result.kind == "ocred"
    assert result.pages[0].kind == "ocred"
    assert fake.calls == []

    def build_without_layer(doc: fitz.Document) -> None:
        _image_page(doc, fraction=0.8)

    make_pdf(tmp_path, "margins_scan.pdf", build_without_layer)
    result = PdfExtractor(tmp_path, ocr=fake).extract("margins_scan.pdf")

    assert result.kind == "image"
    assert len(fake.calls) == 1


def test_image_below_threshold_with_text_is_text_page(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        _image_page(doc, fraction=0.4, text="Real body text")

    make_pdf(tmp_path, "figure.pdf", build)
    fake = FakeOcrEngine([OcrLine("figure caption", 0.9)])

    result = PdfExtractor(tmp_path, ocr=fake).extract("figure.pdf")

    assert result.kind == "text"
    assert result.pages[0].kind == "text"
    assert result.pages[0].image_coverage == pytest.approx(0.4, abs=0.01)
    assert result.pages[0].ocr_confidence is None
    assert fake.calls == []


def test_sparse_text_still_classifies_as_text_page(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        _text_page(doc, "Hi")

    make_pdf(tmp_path, "sparse.pdf", build)

    result = PdfExtractor(tmp_path).extract("sparse.pdf")

    assert result.kind == "text"
    assert result.pages[0].kind == "text"
    assert "Hi" in result.pages[0].text


def test_mixed_document_aggregates_to_mixed_kind(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        _text_page(doc, "Abstract body")
        _image_page(doc)
        _image_page(doc, text_layer="References layer")

    make_pdf(tmp_path, "mixed.pdf", build)
    fake = FakeOcrEngine([OcrLine("figure text", 0.95)])

    result = PdfExtractor(tmp_path, ocr=fake).extract("mixed.pdf")

    assert result.kind == "mixed"
    assert [page.kind for page in result.pages] == ["text", "image", "ocred"]
    assert len(fake.calls) == 1
    assert "figure text" in result.pages[1].text


def test_blank_pages_are_flagged_and_do_not_affect_document_kind(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        _blank_page(doc)
        _text_page(doc, "Only real page")

    make_pdf(tmp_path, "with_blank.pdf", build)
    fake = FakeOcrEngine([OcrLine("never", 1.0)])

    result = PdfExtractor(tmp_path, ocr=fake).extract("with_blank.pdf")

    assert result.kind == "text"
    assert result.pages[0].kind == "text"
    assert result.pages[0].is_blank is True
    assert result.pages[0].text == ""
    assert fake.calls == []

    def build_blank(doc: fitz.Document) -> None:
        _blank_page(doc)

    make_pdf(tmp_path, "blank.pdf", build_blank)
    result = PdfExtractor(tmp_path).extract("blank.pdf")

    assert result.kind == "text"
    assert result.pages[0].is_blank is True


def test_normalises_newlines_and_unicode(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        page = doc.new_page()
        page.insert_text((72, 72), "\uff21\uff22\uff23", fontsize=14, fontname="china-s")

    make_pdf(tmp_path, "unicode.pdf", build)

    result = PdfExtractor(tmp_path).extract("unicode.pdf")

    assert "ABC" in result.pages[0].text
    assert "\uff21" not in result.pages[0].text


def test_page_range_and_truncation_warning(tmp_path: Path) -> None:
    def build(doc: fitz.Document) -> None:
        for index in range(1, 6):
            _text_page(doc, f"page {index} body")

    make_pdf(tmp_path, "long.pdf", build)
    extractor = PdfExtractor(tmp_path)

    result = extractor.extract("long.pdf", start_page=2, max_pages=2)

    assert [page.number for page in result.pages] == [2, 3]
    assert result.warnings == ("Extraction limited to pages 2-3 of 5; increase max_pages to read more.",)

    full = extractor.extract("long.pdf")
    assert [page.number for page in full.pages] == [1, 2, 3, 4, 5]
    assert full.warnings == ()


def test_start_page_beyond_document_raises(tmp_path: Path) -> None:
    make_pdf(tmp_path, "one.pdf", lambda doc: _text_page(doc))

    with pytest.raises(ToolError, match="start_page 2"):
        PdfExtractor(tmp_path).extract("one.pdf", start_page=2)


def test_extract_validates_start_page_and_max_pages(tmp_path: Path) -> None:
    make_pdf(tmp_path, "one.pdf", lambda doc: _text_page(doc))
    extractor = PdfExtractor(tmp_path)

    with pytest.raises(ToolError, match="start_page"):
        extractor.extract("one.pdf", start_page=0)
    with pytest.raises(ToolError, match="max_pages"):
        extractor.extract("one.pdf", max_pages=0)


def test_rejects_non_pdf_missing_and_traversal_paths(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("plain text", encoding="utf-8")
    extractor = PdfExtractor(tmp_path)

    with pytest.raises(ToolError, match="not a PDF"):
        extractor.extract("notes.txt")
    with pytest.raises(ToolError, match="Not a file"):
        extractor.extract("missing.pdf")
    with pytest.raises(ToolError, match="workspace"):
        extractor.extract("../outside.pdf")
    with pytest.raises(ToolError, match="relative"):
        extractor.extract(str(Path(tmp_path).anchor + "Windows.pdf"))


def test_rejects_corrupt_pdf_file(tmp_path: Path) -> None:
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.7\n%%EOF nothing else here")

    with pytest.raises(ToolError, match="open PDF"):
        PdfExtractor(tmp_path).extract("broken.pdf")


def test_rejects_encrypted_pdf(tmp_path: Path) -> None:
    doc = fitz.open()
    _text_page(doc, "secret body")
    path = tmp_path / "locked.pdf"
    doc.save(str(path), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()

    with pytest.raises(ToolError, match="encrypted"):
        PdfExtractor(tmp_path).extract("locked.pdf")


def test_rejects_oversized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PdfExtractor, "MAX_FILE_BYTES", 10)
    make_pdf(tmp_path, "big.pdf", lambda doc: _text_page(doc))

    with pytest.raises(ToolError, match="byte limit"):
        PdfExtractor(tmp_path).extract("big.pdf")


def test_rejects_document_with_too_many_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PdfExtractor, "MAX_PAGES", 2)
    make_pdf(tmp_path, "huge.pdf", lambda doc: (_text_page(doc), _text_page(doc), _text_page(doc)))

    with pytest.raises(ToolError, match="page limit"):
        PdfExtractor(tmp_path).extract("huge.pdf")


def test_ocr_empty_result_warns_and_keeps_image_page(tmp_path: Path) -> None:
    make_pdf(tmp_path, "empty_ocr.pdf", lambda doc: _image_page(doc))
    fake = FakeOcrEngine([])

    result = PdfExtractor(tmp_path, ocr=fake).extract("empty_ocr.pdf")

    assert result.kind == "image"
    assert result.pages[0].text == ""
    assert result.pages[0].ocr_confidence == 0.0
    assert result.warnings == ("OCR found no text on page 1.",)


def test_ocr_failure_warns_and_keeps_page(tmp_path: Path) -> None:
    make_pdf(tmp_path, "broken_ocr.pdf", lambda doc: _image_page(doc))
    fake = FakeOcrEngine(fail=RuntimeError("engine crashed"))

    result = PdfExtractor(tmp_path, ocr=fake).extract("broken_ocr.pdf")

    assert result.kind == "image"
    assert result.pages[0].text == ""
    assert result.pages[0].ocr_confidence is None
    assert result.warnings[0].startswith("OCR failed on page 1:")


def test_oversized_page_skips_ocr_with_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PdfExtractor, "MAX_PAGE_MEGAPIXELS", 0.001)
    make_pdf(tmp_path, "huge_page.pdf", lambda doc: _image_page(doc))
    fake = FakeOcrEngine([OcrLine("never", 1.0)])

    result = PdfExtractor(tmp_path, ocr=fake).extract("huge_page.pdf")

    assert result.kind == "image"
    assert fake.calls == []
    assert result.pages[0].text == ""
    assert result.warnings == ("Skipped OCR on page 1: rendered page exceeds the 0.001 megapixel limit.",)
