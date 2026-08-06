from __future__ import annotations

import pytest

from evo_helper.vision.engines import (
    Detection,
    NullDetectorEngine,
    NullOcrEngine,
    NullTemplateMatcher,
)
from evo_helper.vision.parsers import UnknownUiVersionError
from evo_helper.vision.pipeline import VisionPipeline


def test_pipeline_recognizes_mail_list() -> None:
    ocr = """
battle report: bot_alice
1:2:3
    """
    pipeline = VisionPipeline(
        NullDetectorEngine([Detection("mail_list_item", 0, 0, 10, 10, 1.0)]),
        NullOcrEngine(ocr),
        NullTemplateMatcher(),
    )
    result = pipeline.mail_list(object())
    assert result.ui_version == "mail-list-v2"
    assert len(result.items) == 1
    assert result.items[0].coordinate is not None


def test_pipeline_stops_on_unknown_page() -> None:
    pipeline = VisionPipeline(NullDetectorEngine(), NullOcrEngine(""), NullTemplateMatcher())
    with pytest.raises(UnknownUiVersionError):
        pipeline.mail_list(object())


def test_pipeline_requires_stable_page_across_frames() -> None:
    detections = [
        Detection("mail_list_item", 0, 0, 10, 10, 1.0),
        Detection("battle_detail", 0, 0, 10, 10, 1.0),
    ]

    class FlipDetector:
        def __init__(self) -> None:
            self._tick = 0

        def detect(self, image: object) -> list[Detection]:
            self._tick += 1
            return [detections[self._tick % 2]]

    pipeline = VisionPipeline(FlipDetector(), NullOcrEngine(""), NullTemplateMatcher())
    page = pipeline._observe_consistent_page(object(), frames=2)
    assert page.ui_version is None
