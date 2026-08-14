from __future__ import annotations

from datetime import UTC, datetime

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.tools.ranking_scan import (
    RankingColumns,
    parse_score,
    rows_from_image,
    targets_from_rows,
)


class _Cell:
    def __init__(self, left: int, text: str) -> None:
        self.left = left
        self.text = text
        self.width = 20
        self.height = 20

    def convert(self, _mode: str) -> _Cell:
        return self

    def resize(self, _size: tuple[int, int], _resample: object) -> _Cell:
        return self


class _Image:
    def crop(self, box: tuple[int, int, int, int]) -> _Cell:
        left, top, _right, _bottom = box
        if top != 235:
            return _Cell(left, "")
        return _Cell(left, {0: "[639]", 100: "bot_4_30_12", 500: "29.59K"}.get(left, ""))


class _Ocr:
    def image_to_string(self, cell: _Cell, **_kwargs: object) -> str:
        return cell.text


def test_parse_score_keeps_unreadable_values_empty() -> None:
    assert parse_score("29.59K") == 29_590.0
    assert parse_score("1.2M") == 1_200_000.0
    assert parse_score("not a score") is None


def test_rows_from_image_drops_unrecognised_rows_without_placeholders() -> None:
    rows = rows_from_image(
        _Image(), _Ocr(), RankingColumns(rank=(0, 90), name=(100, 450), score=(500, 650))
    )

    assert rows == [
        RankingRow(
            rank=639,
            name="bot_4_30_12",
            score=29_590.0,
            coordinate=Coordinate(4, 30, 12),
        )
    ]


def test_interpolated_score_is_explicitly_marked_estimated() -> None:
    targets = targets_from_rows(
        [
            RankingRow(639, "bot_4_30_12", 30.0, Coordinate(4, 30, 12)),
            RankingRow(640, "bot_4_100_13", None, Coordinate(4, 100, 13)),
            RankingRow(641, "bot_4_183_20", 20.0, Coordinate(4, 183, 20)),
        ],
        observed_at=datetime(2026, 8, 14, tzinfo=UTC),
    )

    assert [(target.military_score, target.military_score_estimated) for target in targets] == [
        (30.0, False),
        (25.0, True),
        (20.0, False),
    ]
