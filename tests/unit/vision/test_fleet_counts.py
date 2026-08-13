"""舰队数量「读到合计对上为止」的判据。

背景是实测：这个游戏的数字字体把相邻笔画粘在一起，`117`→`17`、`11`→`1`、`39`→`33`。
没有校验时这些错误一路「成功」入库——守方合计 247 存成了 144，全程零报错。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.fleet_counts import parse_fleet_count as parse_count
from evo_helper.vision.fleet_counts import (
    COUNT_RECIPES,
    FleetCountsUnresolved,
    FleetReading,
    nudge_offset,
    pick_count,
    read_until_total,
    reconcile_counts,
    row_grid,
)

TRUTH = (117, 31, 13, 11, 6, 8, 4, 1, 39, 17)
WRONG = (117, 31, 13, 1, 6, 8, 4, 1, 33, 17)


def test_a_reading_that_sums_right_is_confirmed() -> None:
    reading = FleetReading(TRUTH, sum(TRUTH), (4, "lanczos"), 0)
    assert reading.confirmed
    assert reading.total == 247


def test_a_reading_that_sums_wrong_is_not() -> None:
    assert not FleetReading(WRONG, 247, (4, "lanczos"), 0).confirmed


def test_an_empty_reading_never_confirms() -> None:
    # 0 == 0 不是证据；空读数意味着这一屏根本没读到东西。
    assert not FleetReading((), 0, (4, "lanczos"), 0).confirmed


def test_stops_at_the_first_recipe_that_matches() -> None:
    tried: list[tuple[int, str]] = []

    def sample(recipe: tuple[int, str]) -> tuple[int, ...]:
        tried.append(recipe)
        return TRUTH if recipe == COUNT_RECIPES[1] else WRONG

    reading = read_until_total(
        sample=sample, expected_total=247, nudge=lambda _dy: pytest.fail("不该需要拖动")
    )
    assert reading.confirmed
    assert reading.recipe == COUNT_RECIPES[1]
    assert tried == list(COUNT_RECIPES[:2])


def test_nudges_and_retries_when_no_recipe_matches_this_capture() -> None:
    """实测：`39` 在六次拖动里对了三次——换背景确实能换来一个独立样本。"""
    nudges: list[int] = []
    state = {"attempt": 0}

    def nudge(dy: int) -> None:
        nudges.append(dy)
        state["attempt"] += 1

    def sample(_recipe: tuple[int, str]) -> tuple[int, ...]:
        return TRUTH if state["attempt"] == 2 else WRONG

    reading = read_until_total(sample=sample, expected_total=247, nudge=nudge)
    assert reading.confirmed
    assert reading.attempt == 2
    assert len(nudges) == 2


def test_gives_up_loudly_rather_than_storing_a_near_miss() -> None:
    """差不多的数不能存——它看起来像数据，不会有人再去核。"""
    with pytest.raises(FleetCountsUnresolved, match="247"):
        read_until_total(
            sample=lambda _r: WRONG,
            expected_total=247,
            nudge=lambda _dy: None,
            max_recaptures=3,
        )


def test_the_failure_says_how_close_it_got() -> None:
    with pytest.raises(FleetCountsUnresolved, match="231"):
        read_until_total(
            sample=lambda _r: WRONG,
            expected_total=247,
            nudge=lambda _dy: None,
            max_recaptures=2,
        )


def test_a_nonsense_expected_total_is_rejected_up_front() -> None:
    with pytest.raises(ValueError, match="期望总数"):
        read_until_total(sample=lambda _r: TRUTH, expected_total=0, nudge=lambda _dy: None)


def test_nudges_alternate_direction() -> None:
    # 一直朝一个方向拖会把内容推出可视区。
    offsets = [nudge_offset(i) for i in range(1, 7)]
    assert all(a * b < 0 for a, b in zip(offsets, offsets[1:], strict=False))


def test_nudge_magnitude_varies() -> None:
    # 同样的位移大概率复现同样的叠合，也就复现同样的错误。
    assert len({abs(nudge_offset(i)) for i in range(1, 7)}) > 1


# 实测投票（5 次截图 × 12 套配方，2026-08-08 的 bot_2_137_14 战报）。
# 真值 117,31,13,11,6,8,4,1,39,17，合计 247。
MEASURED = [
    ("轻型战斗机", {117: 9}),
    ("重型战斗机", {41: 15, 31: 12, 1: 9}),
    ("巡洋舰", {13: 36}),
    ("战列舰", {1: 18, 11: 2}),
    ("无畏舰", {6: 7, 5: 7}),
    ("轰炸机", {8: 6}),
    ("毁灭者", {4: 18}),
    ("钛能守卫者", {1: 21, 7: 3}),
    ("火箭发射器", {39: 23, 44: 9}),
    ("轻型激光炮", {7: 12, 17: 3}),
]


def test_a_fleet_whose_rows_add_up_is_fully_trusted() -> None:
    fleet = reconcile_counts([("甲", {5: 3}), ("乙", {7: 3})], 12)
    assert fleet.reconciled
    assert fleet.uncertain_rows == 0
    assert [row.count for row in fleet.rows] == [5, 7]


def test_the_total_comes_from_the_detail_page_not_the_rows() -> None:
    """逐行读不准时总数仍然可信——它是独立来源。"""
    fleet = reconcile_counts([("甲", {5: 3}), ("乙", {7: 3})], 247)
    assert fleet.total == 247
    assert fleet.rows_total == 12
    assert not fleet.reconciled


def test_only_rows_whose_reads_disagreed_are_flagged() -> None:
    # 「甲」每一遍都读成 5，是现有证据里最扎实的一档；「乙」自己读出过两个值。
    fleet = reconcile_counts([("甲", {5: 9}), ("乙", {5: 9, 15: 2})], 20)
    flagged = {row.ship for row in fleet.rows if row.uncertain}
    assert flagged == {"乙"}


def test_unanimous_rows_stay_trusted_even_when_the_fleet_does_not_add_up() -> None:
    fleet = reconcile_counts([("甲", {5: 9}), ("乙", {7: 9})], 100)
    assert not fleet.reconciled
    assert fleet.uncertain_rows == 0


def test_the_measured_report_flags_the_rows_that_are_actually_wrong() -> None:
    """按票数标星会标错——实测 `11→1`、`17→7` 都是 100% 一致的误读，
    而只有 50% 一致的「无畏舰」反倒是对的。合计差额才指得准。
    """
    fleet = reconcile_counts(MEASURED, 247)
    assert not fleet.reconciled
    flagged = {row.ship for row in fleet.rows if row.uncertain}
    picked = {row.ship: row.count for row in fleet.rows}
    truth = dict(zip([s for s, _ in MEASURED], (117, 31, 13, 11, 6, 8, 4, 1, 39, 17), strict=True))

    # 三个真错的行必须全被标住——漏标一个就等于放一个假数据进库。
    wrong = {ship for ship, count in picked.items() if count != truth[ship]}
    assert wrong <= flagged, f"漏标了 {wrong - flagged}"

    # 未标的行必须全对，否则这个「可信」的说法就是假的。
    unflagged = {row.ship for row in fleet.rows if not row.uncertain}
    assert all(picked[ship] == truth[ship] for ship in unflagged)
    assert unflagged == {"轻型战斗机", "巡洋舰", "轰炸机", "毁灭者"}


def test_the_measured_report_keeps_the_true_total() -> None:
    fleet = reconcile_counts(MEASURED, 247)
    assert fleet.total == 247


def test_a_tie_is_broken_deterministically() -> None:
    # 平票取小的：结果不该随读取顺序变。
    first = reconcile_counts([("甲", {6: 7, 5: 7})], 6)
    second = reconcile_counts([("甲", {5: 7, 6: 7})], 6)
    assert first.rows[0].count == second.rows[0].count == 5


class TestPickCount:
    """选票规则：掉了字的让位于更全的候选。"""

    def test_a_truncated_reading_loses_to_the_full_one_even_with_more_votes(self) -> None:
        # 实测：`74` 读成 `4` 21 次、读对 15 次。多数票会选错。
        assert pick_count({"4": 21, "74": 15}) == "74"

    def test_a_tie_goes_to_the_longer_reading(self) -> None:
        # 实测：`210` 与 `10` 各 15 票。
        assert pick_count({"210": 15, "10": 15}) == "210"

    def test_unrelated_candidates_still_go_to_the_majority(self) -> None:
        # 不是后缀关系就照常比票——这条规则只针对截断。
        assert pick_count({"570": 20, "670": 9}) == "570"

    def test_the_k_suffix_survives(self) -> None:
        assert pick_count({"5.73K": 9, "73K": 4}) == "5.73K"

    def test_no_votes_reads_as_empty(self) -> None:
        assert pick_count({}) == ""

    def test_a_dropped_decimal_point_loses_to_the_dotted_reading(self) -> None:
        """实机 2026-08-11：2:48:12 的守方「单位」实为 `1.22K`（1220 艘）。

        小数点丢了就成了 `122K` = 122000，差整整 100 倍，而分档的三条边界
        （2K/5K/8K）全落在这两个读数之间——那一发因此从「2K 以下，不该打」
        变成了「8K+，用最重的组合打」。
        """
        assert pick_count({"122K": 5, "1.22K": 1}) == "1.22K"

    def test_the_dot_only_travels_one_way(self) -> None:
        """这个字体只会漏笔画，不会凭空多出一个点——所以只有带点的能吸收去点的。

        反过来允许的话，一个真的 `122` 会被一票 `1.22` 拽成 1220。
        """
        assert parse_count(pick_count({"122K": 5, "1.22K": 1})) == 1220
        assert parse_count(pick_count({"1.22K": 5, "122K": 1})) == 1220

    def test_the_dot_rule_does_not_swallow_unrelated_readings(self) -> None:
        """判据只认「同一串数字、只差一个点」，不是「子序列」。

        放宽成子序列的话 `11` 会被并进 `1.17K`（1、1 确实按顺序出现在里面），
        一个 11 艘的读数就成了 1170。
        """
        assert pick_count({"11": 5, "1.17K": 2}) == "11"


class TestRowGrid:
    """行位置用等距网格，不用逐行检测。"""

    def test_rows_are_evenly_spaced_from_the_first(self) -> None:
        assert row_grid(410, 22, 4) == [410, 432, 454, 476]

    def test_the_grid_covers_rows_detection_missed(self) -> None:
        """实测 17 行的表检出 18 行：一整行没被认出来，位置被碎片顶替，
        之后所有索引错开一位。网格按真值行数排，漏检不影响其余行。
        """
        assert len(row_grid(410, 22, 17)) == 17

    def test_a_nonsense_pitch_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="行距"):
            row_grid(410, 0, 5)
