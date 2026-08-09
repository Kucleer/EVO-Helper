"""侦察报告只为回答一件事：这个海盗打不打。

守的是判据的**不对称性**——读到实打实的舰队就打，没读全又都是小数目时
不下结论。把「没看清」当成「这里是空的」，是这条链路唯一会把舰队送错地方的方式。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.vision.scout_reports import (
    VERDICT_ATTACK,
    VERDICT_SKIP,
    VERDICT_UNREADABLE,
    PirateScoutReading,
    ScoutReportUnreadable,
    parse_intro_coordinates,
    read_pirate_scout,
)

HEADER = "发件人: Aries [HQ]        09/08/2026 02:55:37\n主题: 侦察报告"
INTRO = ["2:137:18 2:137:4 3\n:\n"]
TARGET = Coordinate(2, 137, 4)
ORIGIN = Coordinate(2, 137, 18)
FULL = {"深空吞噬者": 2, "噬能截击者": 4, "钛能守卫者": 4, "收割者": 0}


class _Screens:
    def __init__(
        self,
        *,
        header: str = HEADER,
        intro: list[str] | None = None,
        counts: dict[str, int] | None = None,
    ) -> None:
        self._header = header
        self._intro = INTRO if intro is None else intro
        self._counts = FULL if counts is None else counts

    def report_header(self) -> str:
        return self._header

    def scout_intro_texts(self) -> list[str]:
        return self._intro

    def named_counts(self, wanted, band, top, bottom, *, count_band=None) -> dict[str, int]:  # type: ignore[no-untyped-def]
        # `count_band` 必须由调用方传进来：不传就会退回现场量数字列，而那在
        # 「整列全 0」的清单上会量到面板左边的水印上去（实机因此误打了一发）。
        assert count_band is not None, "数量列必须写死传入，不能现场量"
        return {name: value for name, value in self._counts.items() if name in wanted}


def _reading(**kwargs: object) -> PirateScoutReading:
    return read_pirate_scout(_Screens(**kwargs), _Screens(**kwargs))  # type: ignore[arg-type]


def test_a_pirate_with_a_real_fleet_is_worth_attacking() -> None:
    reading = _reading()

    assert reading.verdict == VERDICT_ATTACK
    assert reading.target == TARGET
    assert reading.origin == ORIGIN
    assert reading.reported_at_utc == datetime(2026, 8, 9, 2, 55, 37, tzinfo=UTC)


def test_an_empty_pirate_is_skipped() -> None:
    reading = _reading(counts={name: 0 for name in FULL})

    assert reading.verdict == VERDICT_SKIP


def test_a_single_ship_does_not_trigger() -> None:
    """规则是「> 1」。恰好 1 艘不算舰队。"""
    reading = _reading(counts={"深空吞噬者": 1, "噬能截击者": 1, "钛能守卫者": 1, "收割者": 1})

    assert reading.verdict == VERDICT_SKIP


def test_a_read_fleet_wins_even_with_a_missing_row() -> None:
    """缺的那格只可能让对方更强，不会让「有舰队」这个结论反过来。"""
    reading = _reading(counts={"深空吞噬者": 2, "噬能截击者": 4, "收割者": 0})

    assert reading.missing == ("钛能守卫者",)
    assert reading.verdict == VERDICT_ATTACK


def test_small_numbers_plus_a_missing_row_reaches_no_conclusion() -> None:
    """这才是危险的那一档：缺的那格可能正是一支舰队，不能当成 0。"""
    reading = _reading(counts={"深空吞噬者": 0, "噬能截击者": 1, "收割者": 0})

    assert reading.verdict == VERDICT_UNREADABLE
    assert not reading.worth_attacking


def test_a_screen_without_any_trigger_ship_is_refused() -> None:
    """一个判定舰种都读不到，说明这一屏根本不是战舰清单。"""
    with pytest.raises(ScoutReportUnreadable, match="拖到底"):
        _reading(counts={})


def test_a_report_for_another_target_is_refused() -> None:
    """信箱按时间倒序，上一轮的报告看起来永远像「最新那封」。"""
    with pytest.raises(ScoutReportUnreadable, match="目标"):
        read_pirate_scout(
            _Screens(),  # type: ignore[arg-type]
            _Screens(),  # type: ignore[arg-type]
            expected_target=Coordinate(2, 137, 1),
        )


def test_the_expected_target_passes_when_it_matches() -> None:
    reading = read_pirate_scout(
        _Screens(),  # type: ignore[arg-type]
        _Screens(),  # type: ignore[arg-type]
        expected_target=TARGET,
    )

    assert reading.target == TARGET


def test_a_battle_report_is_not_a_scout_report() -> None:
    with pytest.raises(ScoutReportUnreadable, match="侦察报告"):
        _reading(header="发件人: System        09/08/2026 04:38:46\n主题: 海盗攻击报告")


def test_a_dirty_intro_read_is_discarded_in_favour_of_a_clean_one() -> None:
    """数字白名单会把中文笔画并进数字：`382:137:4` 是噪声，不是坐标。

    多出来的东西说明这一遍读脏了，**整遍作废**，而不是从脏读里挑前两个。
    """
    pair = parse_intro_coordinates(
        ["2:137:18 382:137:4 3", "2137:18 92:1374", "2:137:18 2:137:4 3"]
    )

    assert pair == (ORIGIN, TARGET)


def test_an_out_of_range_galaxy_is_not_a_coordinate() -> None:
    assert parse_intro_coordinates(["382:137:4 2:137:18"]) is None


def test_an_intro_without_two_coordinates_is_refused() -> None:
    with pytest.raises(ScoutReportUnreadable, match="坐标"):
        _reading(intro=["2:137:18"])
