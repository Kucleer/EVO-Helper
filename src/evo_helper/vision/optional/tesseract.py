"""Tesseract OCR adapter (optional; requires pytesseract + tesseract binary)."""

from __future__ import annotations


class TesseractOcrEngine:
    def recognize(self, image: object) -> str:
        try:
            import pytesseract
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("pytesseract is not installed") from exc
        return pytesseract.image_to_string(image)  # type: ignore[no-any-return]
