"""Offline OCR regression against the evo-20260807-live batch images.

Skipped unless the vision extra and the batch are both present: the raw game
screenshots are runtime-only under ``var/`` and never enter Git, so CI runs the
rest of the suite without them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.vision.live_reports import LiveReportReader
from evo_helper.vision.models import PageObservation
from evo_helper.vision.report_layout import layout_for_viewport

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

BATCH = Path("var/captures/evo-20260807-live")
MAIL = BATCH / "evo-20260807-live-000-mail_list.png"
DETAIL = BATCH / "evo-20260807-live-001-mail_detail.png"
REPLAY = BATCH / "evo-20260807-live-003-battle_replay.png"
TESSERACT = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

pytestmark = pytest.mark.skipif(
    not (MAIL.is_file() and DETAIL.is_file() and REPLAY.is_file() and Path(TESSERACT).is_file()),
    reason="evo-20260807-live batch or Tesseract not available",
)


def screens(path: Path, **kwargs: object):
    from evo_helper.vision.optional.report_screens import ImageReportScreens

    image = Image.open(path)
    layout = layout_for_viewport(image.width, image.height)
    return ImageReportScreens(image, layout, tesseract_cmd=TESSERACT, **kwargs)  # type: ignore[arg-type]


class _Chain:
    """Mail rows from one screenshot, report body from the other two."""

    def __init__(self) -> None:
        self._mail = screens(MAIL)
        self._detail = screens(DETAIL)
        self._replay = screens(REPLAY, rounds=[(1, 770, 879)])

    def mail_rows(self) -> list[str]:
        return self._mail.mail_rows()

    def report_header(self) -> str:
        return self._detail.report_header()

    def versus_block(self) -> str:
        return self._replay.replay_versus_block()

    def participating_columns(self) -> tuple[str, str]:
        return self._replay.participating_columns()

    def round_columns(self) -> list[tuple[int, str, str]]:
        return []


def test_mail_list_page_offers_no_matchable_report() -> None:
    reader = LiveReportReader(_Chain())
    rows = reader.list_attack_reports(
        PageObservation(screen="mail_list", ui_version="mail-list-v2", confidence=0.99)
    )
    # The visible page holds one 矮星系统战报, four 海盗攻击报告 and 侦察报告;
    # none of them may be offered as a dispatch match.
    assert rows == ()


def test_report_reads_both_coordinates_and_the_bot_name() -> None:
    reader = LiveReportReader(_Chain())
    report = reader.read_report(
        PageObservation(screen="mail_detail", ui_version="battle-detail-v2", confidence=0.99),
        PageObservation(screen="battle_replay", ui_version="battle-replay-v2", confidence=0.99),
    )

    assert report.attacker.coordinate.value == Coordinate(2, 137, 18)
    assert report.defender.coordinate.value == Coordinate(2, 149, 17)
    assert report.reported_at_utc.isoformat() == "2026-08-06T11:45:03+00:00"
    assert report.raw_time_text == "06/08/2026 11:45:03"


def test_fleet_counts_match_the_screenshot() -> None:
    reader = LiveReportReader(_Chain())
    report = reader.read_report(
        PageObservation(screen="mail_detail", ui_version="battle-detail-v2", confidence=0.99),
        PageObservation(screen="battle_replay", ui_version="battle-replay-v2", confidence=0.99),
    )

    assert [line.count for line in report.participating_attacker] == [265, 178]
    assert [line.count for line in report.participating_defender] == [
        461,
        736,
        257,
        148,
        95,
        166,
        97,
        5,
        2,
        2,
        35,
        51,
        55,
        48,
        16,
    ]


def test_background_filler_produces_no_phantom_rows() -> None:
    """The panel renders dim COMMAND OFFICERS / -17003 text inside the columns."""
    reader = LiveReportReader(_Chain())
    report = reader.read_report(
        PageObservation(screen="mail_detail", ui_version="battle-detail-v2", confidence=0.99),
        PageObservation(screen="battle_replay", ui_version="battle-replay-v2", confidence=0.99),
    )

    assert len(report.participating_attacker) == 2
    assert len(report.participating_defender) == 15
