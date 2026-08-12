"""侦察判定规则：打、不打、还是没看清。

整套规则建立在**「没读出来」不等于 0** 这一个区分上，所以这里的断言几乎都在
钉那条边界，而不是钉「大于 1 就打」这种一眼就对的部分。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from evo_helper.application.report_ingest import to_scout_reading
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import ScoutReport, ScoutTriggerShip
from evo_helper.domain.scout_verdict import (
    PIRATE_TRIGGER_SHIPS,
    VERDICT_ATTACK,
    VERDICT_SKIP,
    VERDICT_UNREADABLE,
    triggers_attack,
    verdict_for,
    verdict_of_record,
)

ALL_READ_ZERO = dict.fromkeys(PIRATE_TRIGGER_SHIPS, 0)


def _record(counts: dict[str, int | None]) -> ScoutReport:
    return ScoutReport(
        report_id=uuid4(),
        reported_at_utc=datetime(2026, 8, 11, 21, 40, tzinfo=UTC),
        raw_time_text="11/08/2026 21:40:00",
        origin=Coordinate(2, 137, 18),
        target=Coordinate(2, 140, 1),
        trigger_ships=tuple(
            ScoutTriggerShip(ship_type=name, count=count) for name, count in counts.items()
        ),
    )


class TestVerdictFor:
    def test_any_ship_above_the_threshold_means_attack(self) -> None:
        assert verdict_for({**ALL_READ_ZERO, "收割者": 2}) == VERDICT_ATTACK

    def test_exactly_the_threshold_is_not_enough(self) -> None:
        # 门槛是「> 1」，不是「>= 1」。1 艘不算有舰队。
        assert verdict_for({**ALL_READ_ZERO, "收割者": 1}) == VERDICT_SKIP

    def test_all_four_read_and_all_small_is_the_only_way_to_skip(self) -> None:
        assert verdict_for(ALL_READ_ZERO) == VERDICT_SKIP

    def test_a_blind_cell_blocks_skip(self) -> None:
        # 读出来的都 ≤ 1、却有一格没读出来：这是「没看清」，不是「这里是空的」。
        counts = {name: 0 for name in PIRATE_TRIGGER_SHIPS if name != "收割者"}

        assert verdict_for(counts, unread=("收割者",)) == VERDICT_UNREADABLE

    def test_a_blind_cell_does_not_block_attack(self) -> None:
        # 正面证据不会因为别处没看清而反转：缺的那格只会让舰队更强。
        counts = {"深空吞噬者": 8}

        assert verdict_for(counts, unread=("收割者",)) == VERDICT_ATTACK

    def test_a_rule_ship_absent_from_the_counts_counts_as_blind(self) -> None:
        """调用方没把它列进 `unread` 也照样算没看清。

        规则表以后加一个舰种，旧报告里根本没有那一行。少了这一条，
        加舰种的当天所有旧报告会集体从「没看清」翻成「不值得打」。
        """
        counts = {name: 0 for name in PIRATE_TRIGGER_SHIPS if name != "钛能守卫者"}

        assert verdict_for(counts) == VERDICT_UNREADABLE

    def test_ships_outside_the_rule_table_do_not_trigger_an_attack(self) -> None:
        # 判据只认那四个舰种；别的舰种再多也不是这条规则要问的事。
        assert not triggers_attack({**ALL_READ_ZERO, "探测器": 99})
        assert verdict_for({**ALL_READ_ZERO, "探测器": 99}) == VERDICT_SKIP


class TestVerdictOfRecord:
    def test_a_null_cell_reads_as_blind_not_as_zero(self) -> None:
        """实机 2026-08-11 的 2:140:1：三格读到 1/0/1，`收割者` 那格是 NULL。

        补成 0 就会判成「不值得打」，而真相是那一格从来没看清过。
        """
        record = _record(
            {"深空吞噬者": 1, "噬能截击者": 0, "钛能守卫者": 1, "收割者": None},
        )

        assert verdict_of_record(record) == VERDICT_UNREADABLE

    def test_a_read_zero_is_evidence(self) -> None:
        record = _record(dict.fromkeys(PIRATE_TRIGGER_SHIPS, 0))

        assert verdict_of_record(record) == VERDICT_SKIP

    @pytest.mark.parametrize(
        "counts",
        [
            {"深空吞噬者": 8, "噬能截击者": 4, "钛能守卫者": 1, "收割者": None},
            {"深空吞噬者": 1, "噬能截击者": 0, "钛能守卫者": 1, "收割者": None},
            {"深空吞噬者": 0, "噬能截击者": 0, "钛能守卫者": 0, "收割者": 0},
            {"深空吞噬者": 0, "噬能截击者": None, "钛能守卫者": None, "收割者": None},
        ],
    )
    def test_it_agrees_with_the_live_chain(self, counts: dict[str, int | None]) -> None:
        """库里那份算出来的判定，必须和活链路当场算的那一份一模一样。

        两条路：`verdict_of_record` 直接读记录，`to_scout_reading(...).verdict`
        先把记录还原成活链路的读数再问。分叉的后果是界面上说「不值得打」、
        而链路当时判的是「没看清」，两句话对应相反的处置。
        """
        record = _record(counts)

        assert verdict_of_record(record) == to_scout_reading(record).verdict
