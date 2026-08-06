from __future__ import annotations

from evo_helper.domain.models import Coordinate
from evo_helper.vision.fusion import CoordinateFusion, NameFusion


def test_coordinate_fusion_requires_three_agreeing_sources() -> None:
    fusion = CoordinateFusion()
    votes = [
        ("yolo", "1:2:3", 0.999),
        ("template", "1:2:3", 0.998),
        ("ocr", "1:2:3", 0.997),
    ]
    result = fusion.fuse(votes)
    assert result is not None
    assert result.value == Coordinate(1, 2, 3)
    assert len(result.sources) == 3


def test_coordinate_fusion_rejects_conflict() -> None:
    fusion = CoordinateFusion()
    votes = [
        ("yolo", "1:2:3", 0.999),
        ("template", "1:2:3", 0.998),
        ("ocr", "1:2:4", 0.997),
    ]
    assert fusion.fuse(votes) is None


def test_coordinate_fusion_rejects_low_confidence() -> None:
    fusion = CoordinateFusion(min_confidence=0.999)
    votes = [
        ("yolo", "1:2:3", 0.9),
        ("template", "1:2:3", 0.9),
        ("ocr", "1:2:3", 0.9),
    ]
    assert fusion.fuse(votes) is None


def test_name_fusion_marks_bot_prefix() -> None:
    fusion = NameFusion()
    votes = [("ocr", "bot_alice", 0.99), ("template", "bot_alice", 0.99)]
    result = fusion.fuse(votes)
    assert result is not None
    assert result.is_bot
