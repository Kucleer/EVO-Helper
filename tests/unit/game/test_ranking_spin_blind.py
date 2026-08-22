"""滚轮盲滚：行 → 格的换算、「一次点击都不发」、以及**只在末尾等一次**。

这一组盯的全是**静默**故障：换算反了、每格都等、拨完不等滑行——三种都不会抛，
只会让盲滚走的距离不是要求的距离，而采回来的数只是静悄悄少一截。
"""

from dataclasses import dataclass, field

import pytest

from evo_helper.game.ranking_nav import RankingNavigator, SpinResult
from evo_helper.game.ranking_ui import GLIDE_SETTLE_S, ROWS_PER_NOTCH, WHEEL_GAP_S


@dataclass
class FakeDriver:
    """`RankingDriver` 的假替身。滚轮只记格数——**驱动面上没有「拨 N 格」**。"""

    notches: int = 0
    waits: list[float] = field(default_factory=list)
    clicks: list[tuple[int, int]] = field(default_factory=list)
    presses: int = 0

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y))

    def press(self, x: int, y: int, *, label: str = "") -> None:
        self.presses += 1

    def move_to(self, x: int, y: int) -> None: ...

    def release(self) -> None: ...

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    def wheel_notch(self) -> None:
        self.notches += 1


def _nav(driver: FakeDriver) -> RankingNavigator[object]:
    return RankingNavigator(
        driver=driver,
        read_labels=lambda: [],
        read_rows=lambda: [],
        row_has_score=lambda row: True,
        say=lambda _m: None,
    )


def test_rows_convert_to_notches_by_the_calibration() -> None:
    driver = FakeDriver()
    result = _nav(driver).spin_blind(rows=108)
    assert driver.notches == round(108 / ROWS_PER_NOTCH)
    assert result == SpinResult(
        rows_requested=108, notches=driver.notches, spin_seconds=result.spin_seconds
    )


def test_a_bigger_request_sends_more_notches() -> None:
    # ⚠️ 这条挡的是「换算写成乘法」——`ROWS_PER_NOTCH` 是 1.08，乘和除只差 8%，
    # 单点断言看不出来，但方向单调性和量级会。
    few = FakeDriver()
    many = FakeDriver()
    _nav(few).spin_blind(rows=100)
    _nav(many).spin_blind(rows=700)
    assert few.notches < many.notches
    assert many.notches == round(700 / ROWS_PER_NOTCH)
    # 每格约推进一行，所以格数该和行数同一个量级，不该差出一个 8.3 倍的「屏」。
    assert 600 < many.notches < 800


def test_zero_rows_sends_nothing() -> None:
    # 0 是最保守的合法取值：「一格都别拨」。这一支也是这次改动的一键回滚——
    # 置 0 就退回纯慢拖，所以它连末尾那次滑行等待都不该做。
    driver = FakeDriver()
    result = _nav(driver).spin_blind(rows=0)
    assert driver.notches == 0
    assert result.notches == 0
    assert driver.waits == []


def test_negative_rows_is_rejected() -> None:
    with pytest.raises(ValueError):
        _nav(FakeDriver()).spin_blind(rows=-1)


def test_spin_waits_once_for_the_glide_not_once_per_notch() -> None:
    # ⚠️ 这条是整个改动的要害：每格都等 = 白改（原先 70 屏 × 2 秒的等待就是
    # 要消掉的东西）。所以「长等待」必须恰好出现一次。
    driver = FakeDriver()
    _nav(driver).spin_blind(rows=500)
    assert driver.waits.count(GLIDE_SETTLE_S) == 1
    assert len([w for w in driver.waits if w >= 1.0]) == 1
    # 末尾那一次，不是开头也不是中间——检测段紧接着就要读行。
    assert driver.waits[-1] == GLIDE_SETTLE_S
    assert set(driver.waits[:-1]) == {WHEEL_GAP_S}


def test_every_notch_is_followed_by_the_measured_gap() -> None:
    # 间隔和格数一对一：拉稀了攒不起动量，实测 117ms/格 时 80 格只走 2 行。
    driver = FakeDriver()
    _nav(driver).spin_blind(rows=200)
    assert driver.waits.count(WHEEL_GAP_S) == driver.notches


def test_spin_never_clicks_or_presses() -> None:
    # 盲滚段全程 `allow_actions` 为假，一次点击、一次按下都不该发出去。
    driver = FakeDriver()
    _nav(driver).spin_blind(rows=500)
    assert driver.clicks == []
    assert driver.presses == 0


def test_spin_seconds_is_measured_not_computed() -> None:
    # 记的是实测用时（好在事后看出 `time.sleep` 粒度把 16ms 撑成了 31ms），
    # 而假驱动的 wait 是空操作，所以这一趟必然远快于「格数 × 16ms」。
    driver = FakeDriver()
    result = _nav(driver).spin_blind(rows=700)
    assert result.spin_seconds >= 0.0
    assert result.spin_seconds < result.notches * WHEEL_GAP_S
