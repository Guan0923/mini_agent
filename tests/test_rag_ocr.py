"""Tests for RapidOCR result conversion (no real model is loaded)."""

from __future__ import annotations

import pytest

from backend.rag import OcrImage, OcrLine, OcrResult, RapidOcrEngine


def _box(left: int, top: int, right: int = 0, bottom: int = 0) -> list[list[float]]:
    return [
        [left, top],
        [right or left + 10, top],
        [right or left + 10, bottom or top + 10],
        [left, bottom or top + 10],
    ]


def test_convert_sorts_lines_top_then_left() -> None:
    raw = [
        [_box(200, 100), "Second", 0.7],
        [_box(300, 10), "First line", 0.9],
        [_box(50, 100), "Third", 0.8],
    ]

    result = RapidOcrEngine.convert(raw)

    assert [line.text for line in result.lines] == ["First line", "Third", "Second"]
    assert [line.confidence for line in result.lines] == [0.9, 0.8, 0.7]
    assert result.confidence == pytest.approx(0.8)


def test_convert_handles_none_and_empty_results() -> None:
    assert RapidOcrEngine.convert(None) == OcrResult()
    assert RapidOcrEngine.convert([]) == OcrResult()
    assert RapidOcrEngine.convert(None).confidence == 0.0


def test_convert_skips_malformed_entries() -> None:
    raw = [
        None,
        ["not-a-box", "NoBox", 0.5],
        [[], "EmptyBox", 0.5],
        [_box(10, 20), "Valid", 0.95],
    ]

    result = RapidOcrEngine.convert(raw)

    assert [line.text for line in result.lines] == ["Valid"]
    assert result.lines[0].confidence == pytest.approx(0.95)


def test_convert_preserves_detection_order_for_same_position() -> None:
    raw = [
        [_box(0, 0), "alpha", 0.4],
        [_box(0, 0), "beta", 0.6],
    ]

    assert [line.text for line in RapidOcrEngine.convert(raw).lines] == ["alpha", "beta"]


def test_result_confidence_is_mean_of_line_confidences() -> None:
    result = OcrResult((OcrLine("a", 0.5), OcrLine("b", 0.9)))

    assert result.confidence == pytest.approx(0.7)
    assert OcrResult().confidence == 0.0


def test_rapid_ocr_engine_does_not_load_models_on_construction() -> None:
    engine = RapidOcrEngine()

    assert engine._engine is None


def test_recognize_accepts_rapidocr_output_object(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeOutput:
        txts = ("hello",)
        boxes = ([[0, 0], [50, 0], [50, 10], [0, 10]],)
        scores = (0.95,)

    class FakeEngine:
        def __call__(self, pixels):
            return FakeOutput()

    engine = RapidOcrEngine()
    monkeypatch.setattr(engine, "_load", lambda: FakeEngine())

    result = engine.recognize(OcrImage(b"\x00" * 48, 4, 4))

    assert [line.text for line in result.lines] == ["hello"]
    assert result.confidence == pytest.approx(0.95)


def test_recognize_accepts_legacy_tuple_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        def __call__(self, pixels):
            return ([[[[0, 0], [50, 0], [50, 10], [0, 10]], "legacy", 0.8]], [0.1])

    engine = RapidOcrEngine()
    monkeypatch.setattr(engine, "_load", lambda: FakeEngine())

    result = engine.recognize(OcrImage(b"\x00" * 48, 4, 4))

    assert [line.text for line in result.lines] == ["legacy"]
    assert result.confidence == pytest.approx(0.8)
