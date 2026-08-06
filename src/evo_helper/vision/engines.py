"""Pluggable vision engine protocols and safe fallback implementations.

Real YOLO/OCR/template engines are optional; the pipeline only depends on these
protocols so unit tests run without heavyweight frameworks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Detection:
    label: str
    x: int
    y: int
    width: int
    height: int
    confidence: float


class OcrEngine(Protocol):
    def recognize(self, image: object) -> str: ...


class DetectorEngine(Protocol):
    def detect(self, image: object) -> list[Detection]: ...


class TemplateMatcher(Protocol):
    def match(self, image: object, template: object) -> float: ...


class NullOcrEngine:
    """Deterministic engine used by tests and as a safe offline fallback."""

    def __init__(self, text: str = "") -> None:
        self._text = text

    def recognize(self, image: object) -> str:
        return self._text


class NullDetectorEngine:
    def __init__(self, detections: list[Detection] | None = None) -> None:
        self._detections = detections or []

    def detect(self, image: object) -> list[Detection]:
        return list(self._detections)


class NullTemplateMatcher:
    def __init__(self, score: float = 0.0) -> None:
        self._score = score

    def match(self, image: object, template: object) -> float:
        return self._score


def build_ocr_engine(kind: str, *, text: str = "") -> OcrEngine:
    """Factory for OCR engines; unknown kinds raise rather than guess."""
    if kind == "null":
        return NullOcrEngine(text)
    if kind == "tesseract":
        from .optional.tesseract import TesseractOcrEngine

        return TesseractOcrEngine()
    raise ValueError(f"unsupported OCR engine: {kind}")


def build_detector_engine(
    kind: str, *, detections: list[Detection] | None = None
) -> DetectorEngine:
    if kind == "null":
        return NullDetectorEngine(detections)
    if kind == "yolo":
        from .optional.yolo import YoloDetectorEngine

        return YoloDetectorEngine(weights="")
    raise ValueError(f"unsupported detector engine: {kind}")


def build_template_matcher(kind: str, *, score: float = 0.0) -> TemplateMatcher:
    if kind == "null":
        return NullTemplateMatcher(score)
    if kind == "opencv":
        from .optional.opencv import OpenCvTemplateMatcher

        return OpenCvTemplateMatcher()
    raise ValueError(f"unsupported template matcher: {kind}")
