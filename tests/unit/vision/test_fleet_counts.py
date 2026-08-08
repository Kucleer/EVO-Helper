"""舰队数量「读到合计对上为止」的判据。

背景是实测：这个游戏的数字字体把相邻笔画粘在一起，`117`→`17`、`11`→`1`、`39`→`33`。
没有校验时这些错误一路「成功」入库——守方合计 247 存成了 144，全程零报错。
"""

from __future__ import annotations

import pytest

from evo_helper.vision.fleet_counts import (
    COUNT_RECIPES,
    FleetCountsUnresolved,
    FleetReading,
    nudge_offset,
    read_until_total,
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
