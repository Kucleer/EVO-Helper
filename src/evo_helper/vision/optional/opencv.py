"""OpenCV template matcher adapter (optional)."""

from __future__ import annotations

from importlib import import_module


class OpenCvTemplateMatcher:
    def match(self, image: object, template: object) -> float:
        try:
            cv2 = import_module("cv2")
            np = import_module("numpy")
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("opencv-python is not installed") from exc
        result = cv2.matchTemplate(np.asarray(image), np.asarray(template), cv2.TM_CCOEFF_NORMED)
        return float(result.max())
