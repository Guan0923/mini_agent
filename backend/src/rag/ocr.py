"""Replaceable OCR engine contract and the default RapidOCR-backed engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class OcrImage:
    """RGB pixel buffer for one rendered page."""

    samples: bytes
    width: int
    height: int


@dataclass(frozen=True)
class OcrLine:
    """One recognized text line with its confidence score."""

    text: str
    confidence: float


@dataclass(frozen=True)
class OcrResult:
    """Ordered lines recognized on one page."""

    lines: tuple[OcrLine, ...] = ()

    @property
    def confidence(self) -> float:
        """Mean line confidence; 0.0 when no lines were recognized."""
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


class OcrEngine(Protocol):
    """Recognize text in one rendered page image."""

    def recognize(self, image: OcrImage) -> OcrResult: ...


class RapidOcrEngine:
    """Default offline OCR backend backed by RapidOCR on ONNX Runtime.

    The model is loaded lazily on the first recognition so importing this
    module never downloads or loads model weights.
    """

    def __init__(self, *, device: str = "cpu", **kwargs: object) -> None:
        self._device = device
        self._kwargs = kwargs
        self._engine: Any = None

    def _load(self) -> Any:
        if self._engine is None:
            from rapidocr import RapidOCR

            params: dict[str, object] = dict(self._kwargs)
            if str(self._device).lower() != "cpu":
                params["EngineConfig.onnxruntime.use_cuda"] = True
                try:
                    params["EngineConfig.onnxruntime.cuda_ep_cfg.device_id"] = int(self._device)
                except ValueError:
                    params["EngineConfig.onnxruntime.cuda_ep_cfg.device_id"] = 0
            self._engine = RapidOCR(params=params or None)
        return self._engine

    def recognize(self, image: OcrImage) -> OcrResult:
        import numpy as np  # RapidOCR dependency; imported lazily for text-only runs

        pixels = np.frombuffer(image.samples, dtype=np.uint8).reshape(image.height, image.width, 3)
        output = self._load()(pixels)
        if isinstance(output, tuple) and len(output) == 2:
            result, _elapsed = output  # rapidocr < 3.6 style
        else:
            result = self._to_entries(output)  # rapidocr >= 3.6 RapidOCROutput object
        return self.convert(result)

    @staticmethod
    def _to_entries(output: Any) -> Any:
        """Normalize a RapidOCROutput object into ``[box, text, score]`` entries."""
        txts = getattr(output, "txts", None)
        if txts is None:
            return None
        boxes = getattr(output, "boxes", None)
        scores = getattr(output, "scores", None)
        entries: list[list[Any]] = []
        for index, text in enumerate(txts):
            if boxes is None:
                box = None
            else:
                box = boxes[index]
                if hasattr(box, "tolist"):
                    box = box.tolist()
            score = float(scores[index]) if scores is not None else 1.0
            entries.append([box, text, score])
        return entries

    @staticmethod
    def convert(raw: Any) -> OcrResult:
        """Convert a raw RapidOCR result into ordered, typed lines.

        RapidOCR returns ``[box, text, score]`` entries in detection order,
        which is not guaranteed to be reading order. Lines are sorted by box
        top, then box left, and malformed entries are skipped.
        """
        if not raw:
            return OcrResult()
        entries: list[tuple[float, float, str, float]] = []
        for item in raw:
            try:
                box, text, score = item[0], item[1], item[2]
                top = min(point[1] for point in box)
                left = min(point[0] for point in box)
            except (IndexError, TypeError, ValueError):
                continue
            entries.append((float(top), float(left), str(text), float(score)))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        return OcrResult(tuple(OcrLine(text, confidence) for _top, _left, text, confidence in entries))
