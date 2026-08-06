"""OpenCV template matcher adapter (optional)."""

from __future__ import annotations


class OpenCvTemplateMatcher:
    def match(self, image: object, template: object) -> float:
        try:
            import cv2  # type: ignore[import-not-found]
            import numpy as np
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("opencv-python is not installed") from exc
        result = cv2.matchTemplate(np.asarray(image), np.asarray(template), cv2.TM_CCOEFF_NORMED)
        return float(result.max())
