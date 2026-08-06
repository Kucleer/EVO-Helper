from __future__ import annotations

from evo_helper.vision.classifier import PageClassifier
from evo_helper.vision.engines import Detection


def test_classifier_maps_detection_to_page_and_version() -> None:
    classifier = PageClassifier()
    page = classifier.classify([Detection("battle_detail", 0, 0, 100, 100, 0.95)])
    assert page.screen == "battle_detail"
    assert page.ui_version == "battle-detail-v2"
    assert page.confidence > 0.9


def test_classifier_returns_unknown_without_version() -> None:
    classifier = PageClassifier()
    page = classifier.classify([])
    assert page.screen == "unknown"
    assert page.ui_version is None
    assert page.confidence == 0.0
