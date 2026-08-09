"""海盗战报只记胜负与战损总数（用户口径 2026-08-09，为省性能）。

这条链路刻意**不读逐舰种明细**，所以它不能复用 `LiveReportReader`：
后者要求参战两列非空，还会因为「海盗攻击报告」不可与派遣匹配而整份拒收。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_helper.vision.pirate_reports import (
    OUTCOME_FAIL,
    OUTCOME_VICTORY,
    PirateReportUnreadable,
    parse_outcome,
    read_pirate_report,
)

HEADER = "发件人: System        09/08/2026 04:38:46\n主题: 海盗攻击报告"
VERSUS = "Kucleer  Pirates\n奥格瑞玛  Alien Brood\n[2:137:18]  [2:137:4]"


class _Screens:
    """一屏的取字面。真实实现是 Pillow 裁剪 + Tesseract。"""

    def __init__(
        self,
        *,
        header: str = HEADER,
        versus: str = VERSUS,
        banner: str = "VICTORY",
        units: tuple[str, str] = ("100", "783"),
        losses: tuple[str, str] = ("0", "783"),
    ) -> None:
        self._header = header
        self._versus = versus
        self._banner = banner
        self._units = units
        self._losses = losses

    def report_header(self) -> str:
        return self._header

    def versus_block(self) -> str:
        return self._versus

    def outcome_banner(self) -> str:
        return self._banner

    def unit_totals(self) -> tuple[str, str]:
        return self._units

    def loss_totals(self) -> tuple[str, str]:
        return self._losses


def test_a_complete_pirate_report_yields_outcome_and_losses() -> None:
    reading = read_pirate_report(_Screens(), _Screens())

    assert reading.outcome == OUTCOME_VICTORY
    assert (reading.attacker_losses, reading.defender_losses) == (0, 783)
    assert (reading.attacker_units, reading.defender_units) == (100, 783)
    assert reading.reported_at_utc == datetime(2026, 8, 9, 4, 38, 46, tzinfo=UTC)
    assert reading.raw_time_text == "09/08/2026 04:38:46"
    assert reading.defender_target.position == 4
    assert reading.attacker_origin.position == 18


def test_per_ship_detail_is_not_recorded() -> None:
    """明细是这条链路刻意省掉的东西，不能悄悄留个空壳字段冒充。"""
    reading = read_pirate_report(_Screens(), _Screens())

    assert not hasattr(reading, "fleet")


def test_a_lost_battle_reads_as_fail() -> None:
    reading = read_pirate_report(_Screens(banner="FAIL"), _Screens())

    assert reading.outcome == OUTCOME_FAIL


def test_a_banner_missing_a_letter_still_snaps() -> None:
    """实测这行大字压在星空上，`VICTORY` 会掉字母。"""
    assert parse_outcome("VICTORV") == OUTCOME_VICTORY
    assert parse_outcome("VICTORY\n") == OUTCOME_VICTORY
    assert parse_outcome("FAlL") == OUTCOME_FAIL


def test_an_unreadable_banner_rejects_the_whole_report() -> None:
    """胜负与战损是这条记录**唯一**的内容，读不出胜负就没有存的价值。"""
    assert parse_outcome("") is None
    assert parse_outcome("TOTAL CREW") is None

    with pytest.raises(PirateReportUnreadable, match="胜负"):
        read_pirate_report(_Screens(banner=""), _Screens())


def test_unreadable_losses_reject_the_whole_report() -> None:
    with pytest.raises(PirateReportUnreadable, match="战损"):
        read_pirate_report(_Screens(), _Screens(losses=("0", "")))


def test_missing_unit_totals_are_tolerated() -> None:
    """单位总数是附带信息；这条记录的正文是胜负与战损。"""
    reading = read_pirate_report(_Screens(units=("", "")), _Screens())

    assert reading.attacker_units is None
    assert reading.outcome == OUTCOME_VICTORY


def test_a_non_pirate_report_is_refused() -> None:
    header = "发件人: System        08/08/2026 13:09:51\n主题: 攻击报告"

    with pytest.raises(PirateReportUnreadable, match="海盗"):
        read_pirate_report(_Screens(header=header), _Screens())


def test_a_one_sided_versus_block_is_refused() -> None:
    """坐标读不全时不能把战报挂到错的目标上。"""
    with pytest.raises(PirateReportUnreadable, match="VS"):
        read_pirate_report(_Screens(versus="Kucleer\n奥格瑞玛\n[2:137:18]"), _Screens())
