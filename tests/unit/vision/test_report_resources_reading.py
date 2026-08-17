"""读战报那一趟怎么把「获得资源」接进来。

⚠️ 这里守的是接线，不是 OCR：**收获接在读战报的同一屏上**，不额外开导航；
提供不了这一屏的实现照样能读出一份完整报告；读不全时**一格都不给**，
绝不因为「读到了几格」就把剩下的当成 0。
"""

from __future__ import annotations

import logging

from evo_helper.domain.records import BattleResourceEntry
from evo_helper.vision.live_reports import DETAIL_UI_VERSION, LiveReportReader
from evo_helper.vision.models import PageObservation

HEADER = "发件人: System                    17/08/2026 11:45:03\n主题: 攻击报告"

VERSUS = (
    "Kucleer                    bot_2_149_17\n"
    "奥格瑞玛                   bot_2_149_17's Planet\n"
    "[2:137:18]                 [2:149:17]"
)

#: 用户 2026-08-17 那份 VICTORY 战报，逐格原样（行优先）。
VICTORY_CELLS = (
    "928K",
    "501.1K",
    "342.9K",
    "7.7K",
    "0",
    "1.2K",
    "233",
    "0",
    "66",
    "4",
    "0",
    "0",
)


class DetailScreens:
    """详情页那一屏。`cells` 为 None 时**不提供** `resource_cells`。

    「不提供」这一档必须桩得出来：它对应的是老实现，而收获是增强项——
    读不到收获不该让整份战报读不出来。
    """

    def __init__(self, cells: tuple[str, ...] | None = VICTORY_CELLS) -> None:
        if cells is not None:
            self.resource_cells = lambda: cells  # type: ignore[method-assign]

    def mail_rows(self) -> list[str]:
        return []

    def report_header(self) -> str:
        return HEADER

    def versus_block(self) -> str:
        return VERSUS

    def participating_columns(self) -> tuple[str, str]:
        return ("", "")

    def round_columns(self) -> list[tuple[int, str, str]]:
        return []

    def unit_totals(self) -> tuple[str, str]:
        return ("100", "319")

    def loss_totals(self) -> tuple[str, str]:
        return ("", "")

    def outcome_banner(self) -> str:
        return "FAIL"


def _read(cells: tuple[str, ...] | None = VICTORY_CELLS):  # type: ignore[no-untyped-def]
    reader = LiveReportReader(DetailScreens(cells))  # type: ignore[arg-type]
    page = PageObservation(screen="mail_detail", ui_version=DETAIL_UI_VERSION, confidence=0.99)
    return reader.read_detail_only(page)


class TestTheHaulRidesAlongWithTheReport:
    def test_non_zero_slots_come_back_with_their_precision_marks(self) -> None:
        assert _read().resources == (
            BattleResourceEntry(slot=0, amount=928_000, approximate=True, uncertainty=500),
            BattleResourceEntry(slot=1, amount=501_100, approximate=True, uncertainty=50),
            BattleResourceEntry(slot=2, amount=342_900, approximate=True, uncertainty=50),
            BattleResourceEntry(slot=3, amount=7_700, approximate=True, uncertainty=50),
            BattleResourceEntry(slot=5, amount=1_200, approximate=True, uncertainty=50),
            BattleResourceEntry(slot=6, amount=233, approximate=False, uncertainty=0),
            BattleResourceEntry(slot=8, amount=66, approximate=False, uncertainty=0),
            BattleResourceEntry(slot=9, amount=4, approximate=False, uncertainty=0),
        )

    def test_an_all_zero_grid_is_a_successful_empty_haul(self) -> None:
        assert _read(("0",) * 12).resources == ()


class TestFailingClosed:
    def test_a_screen_without_the_grid_still_yields_a_report(self) -> None:
        """收获是增强项。提供不了这一屏的实现照样能读出一份完整报告。"""
        report = _read(None)

        assert report.resources == ()
        assert report.attacker_units == 100

    def test_a_partial_read_gives_nothing_and_says_so(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """⚠️ 读不全就一格都不给，**而且要吭一声**。

        交出去的空元组和「12 格全是 0」长得一模一样；不留日志的话，这条链路
        哪天整块失灵都没人看得见。
        """
        cells = list(VICTORY_CELLS)
        cells[6] = ""

        with caplog.at_level(logging.WARNING):
            report = _read(tuple(cells))

        assert report.resources == ()
        assert any("获得资源" in record.getMessage() for record in caplog.records)
