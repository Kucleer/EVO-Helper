"""Ultralytics YOLO detector adapter (optional)."""

from __future__ import annotations

from typing import Any

from ..engines import Detection


class YoloDetectorEngine:
    def __init__(self, weights: str) -> None:
        self._weights = weights
        self._model: Any = None

    def detect(self, image: object) -> list[Detection]:
        if self._model is None:
            try:
                from ultralytics import YOLO
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise RuntimeError("ultralytics is not installed") from exc
            self._model = YOLO(self._weights)
        results = self._model(image)
        detections: list[Detection] = []
        for box in results[0].boxes:
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
            detections.append(
                Detection(
                    label=str(results[0].names[int(box.cls[0])]),
                    x=x1,
                    y=y1,
                    width=x2 - x1,
                    height=y2 - y1,
                    confidence=float(box.conf[0]),
                )
            )
        return detections
