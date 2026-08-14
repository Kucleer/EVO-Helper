"""派遣面板开过之后必须清导航器缓存。

`SystemNavigator.goto` 只重设它认为变了的字段：

    if at is None or at.galaxy != coordinate.galaxy:
        self._set(GALAXY_FIELD, ...)      # 认为一样就整个跳过

这是个正确的优化——前提是它的记忆和导航栏实际值没分岔。一旦分岔，它**再也不会
自己纠回来**，因为「一样」的判断用的就是那份错记忆。

实机（2026-08-11 00:55–01:08，13 分钟、一发没派）：

1. 第一个目标 2:320:11 导航正常、坐标核对通过，走到派遣面板；
2. 预设条读成空，走 `PresetNotFound` 分支关掉面板 —— **这里原来没清缓存**；
3. 下一个目标 2:321:5：缓存说银河系已经是 2，跳过重设；那一下「设恒星系」落到了
   银河系框上，游戏把 136 截断成它的最大值 9；恒星系没被设，还停在 320；
4. 从此导航栏是 `[9:320:5]`，而缓存说 `2:320:11`。后面每个目标的银河系都是 2，
   于是**银河系字段再也不会被重设**，连续 44 个目标坐标核对全不过。

坐标核对每一次都拦对了——它拦的是「往银河系 9 打」。所以判据一个字都不动，
要补的是这一处缺失的 `invalidate()`。

## 但「每关一层浮层就清一次」是另一回事

用户 2026-08-11：「海盗侦查不用每次都修改 3 个坐标，降低效率。」派出一发侦察之后
`_leave_dispatch_list` 也清了一次缓存，于是同一恒星系里**每颗星球都要重设三个字段**
——一次字段输入是三个动作约 3 秒，每颗星球白花 6 秒。

不清的依据不是「大概没事」，而是缓存里只放**回读确认过**的坐标
（`SystemNavigator` 的类注释）：那份记忆来自派遣之前面板坐标行的一次核对，
而关掉一层浮层并不改导航栏的值。真会改值的动作照旧清——切视图由
`ensure_system_view` 自己清，重连、关窗重开也各有一处。
"""

from __future__ import annotations

from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.game import pirate_ui
from evo_helper.game.preset_picker import PresetNotFound
from evo_helper.game.system_navigator import (
    GALAXY_FIELD,
    POSITION_FIELD,
    SYSTEM_FIELD,
    SystemNavigator,
)
from evo_helper.tools.pirate_loop import LoopOptions, Outcome, PirateLoop

TARGET = Coordinate(2, 320, 11)


class _Driver:
    def click(self, _x: int, _y: int, *, label: str = "") -> None:
        pass

    def wait(self, _seconds: float) -> None:
        pass


class _Navigator:
    def __init__(self) -> None:
        self.invalidated = 0

    def invalidate(self) -> None:
        self.invalidated += 1


def _loop(monkeypatch: Any, *, preset_found: bool) -> tuple[Any, _Navigator]:
    navigator = _Navigator()
    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = _Driver()  # type: ignore[attr-defined]
    loop._navigator = navigator  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._options = LoopOptions(systems=(), scout=False, attack=True)  # type: ignore[attr-defined]
    loop._preset_names = lambda: []  # type: ignore[attr-defined, assignment]

    class _Picker:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def pick(self, wanted: str) -> None:
            if not preset_found:
                raise PresetNotFound(f"预设条上找不到 {wanted!r}；这一屏读到的是 []")

    monkeypatch.setattr("evo_helper.tools.pirate_loop.PresetPicker", _Picker)
    return loop, navigator


def test_a_missing_preset_invalidates_the_nav_cache(monkeypatch: Any) -> None:
    """本文件的重点。少了这一步，下一个目标就会把恒星系号写进银河系框。"""
    loop, navigator = _loop(monkeypatch, preset_found=False)

    assert loop.attack(TARGET, preset="探路") is False
    assert navigator.invalidated == 1, "关掉派遣面板之后导航栏状态已不可知，必须清缓存"


def test_the_refusal_is_still_recorded(monkeypatch: Any) -> None:
    """清缓存不能把原来的拦下记录挤掉——调度器靠它判定这一发没派出去。"""
    loop, _navigator = _loop(monkeypatch, preset_found=False)

    loop.attack(TARGET, preset="探路")

    assert loop._outcome.refused == [(TARGET, "找不到预设 探路")]


# -- 关掉浮层不等于导航栏变了 --------------------------------------------------


def test_leaving_the_dispatch_list_keeps_the_confirmed_coordinate() -> None:
    """**本文件的另一个重点**（用户口径：海盗侦查不用每次都改三个坐标）。

    派出一发之后关掉「飞行中」列表，导航栏的三个值一个都没变——而这份记忆是
    派遣**之前**面板坐标行核对出来的，是有证据的。清掉它，同一恒星系的下一位
    就要重设银河系与恒星系两格，每颗星球白花约 6 秒。

    仍然安全的原因有两层：切视图那一步真要发生时 `ensure_system_view` 自己会清；
    而万一这份记忆终究不作数，下一个目标的回读会当场核不过，走
    `_goto_checked` 的自愈（复位 → 清缓存 → 三格全重设）。
    """
    navigator = _Navigator()
    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = _Driver()  # type: ignore[attr-defined]
    loop._navigator = navigator  # type: ignore[attr-defined]
    loop._require_system_view = lambda _what: None  # type: ignore[assignment, method-assign]

    loop._leave_dispatch_list()

    assert navigator.invalidated == 0


def test_closing_the_mailbox_keeps_the_confirmed_coordinate() -> None:
    """信箱同理：它是浮层，关掉之后导航栏还是原来那三个值。

    进信箱要先切到地表视图，而那一步（`_goto_planet_surface` 与回来时的
    `ensure_system_view`）**换过视图就已经清过缓存了**——在这里再补一次是空动作，
    代价却是下一个目标白设两个字段。
    """
    navigator = _Navigator()
    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = _Driver()  # type: ignore[attr-defined]
    loop._navigator = navigator  # type: ignore[attr-defined]
    loop._on_mail_list = lambda: False  # type: ignore[assignment, method-assign]
    loop._require_system_view = lambda _what: None  # type: ignore[assignment, method-assign]

    loop._close_mail()

    assert navigator.invalidated == 0


# -- 扫一遍 1–4 位要付多少次字段输入 -------------------------------------------


class _NavDriver:
    """一个会照着输入更新导航栏的假游戏：点字段 → 打数字 → 那一格就变成那个数。

    比只记点击更有意义：坐标回读读的就是这三个数，所以「省了字段」与
    「面板显示对不对」在这条测试里是连着的——省错了当场就核不过。
    """

    def __init__(self) -> None:
        self.values = {GALAXY_FIELD: 0, SYSTEM_FIELD: 0, POSITION_FIELD: 0}
        self.sets: list[tuple[int, int]] = []
        self._field: tuple[int, int] | None = None

    def click(self, x: int, y: int, *, label: str = "") -> None:
        if (x, y) in self.values:
            self._field = (x, y)

    def type_number(self, value: int) -> None:
        assert self._field is not None, "没先点开字段就打字，游戏里那是往别处输"
        self.values[self._field] = value
        self.sets.append(self._field)

    def capture(self) -> Any:
        return _Image()

    def wait(self, seconds: float) -> None:
        return None

    def shown(self) -> str:
        return (
            f"[{self.values[GALAXY_FIELD]}:"
            f"{self.values[SYSTEM_FIELD]}:{self.values[POSITION_FIELD]}]"
        )


class _Image:
    """`crop_reader` 只要一个 `crop(box)`；把 box 原样传给假的 OCR。"""

    def crop(self, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return box


def _sweeping_loop(driver: _NavDriver, *, pirate_at: int) -> Any:
    """真导航器 + 真 `check_target`，只把「读屏」换成照着假游戏的导航栏回答。"""
    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = driver  # type: ignore[attr-defined]
    loop._navigator = SystemNavigator(driver)  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._options = LoopOptions(systems=((2, 137),), scout=True, attack=False)  # type: ignore[attr-defined]
    loop._coord_dumps = 0  # type: ignore[attr-defined]
    # 今天的账是空的 = 每个坐标都还没侦察过，侦察照常派。这几条钉的是导航缓存，
    # 当日去重另有专文（`test_pirate_loop_daily_dedup.py`）。
    loop._daily = {}  # type: ignore[attr-defined]
    loop._dump_frame = lambda name, roi=None: None  # type: ignore[assignment, method-assign]
    loop._require_system_view = lambda _what: None  # type: ignore[assignment, method-assign]

    def _read(roi: tuple[int, int, int, int], **_recipe: Any) -> str:
        if roi == pirate_ui.PIRATE_COORD_ROI:
            return driver.shown()
        if roi == pirate_ui.PIRATE_TITLE_ROI:
            is_pirate = driver.values[POSITION_FIELD] == pirate_at
            return pirate_ui.PIRATE_TITLE_TEXT if is_pirate else "荒芜行星"
        return ""

    def _ocr(box: tuple[int, int, int, int], **_recipe: Any) -> str:
        from evo_helper.vision.scan_reading import FREE_COORD_ROI, OWNED_COORD_ROI

        return driver.shown() if box in (OWNED_COORD_ROI, FREE_COORD_ROI) else ""

    def _scout(_coordinate: Coordinate) -> bool:
        # 真 `scout()` 要开派遣面板、写库；这里只保留它对导航的影响：
        # 派出之后停在「飞行中」列表上，要自己退出来。
        loop._leave_dispatch_list()
        return True

    loop._read = _read  # type: ignore[assignment, method-assign]
    loop._ocr = _ocr  # type: ignore[attr-defined]
    loop.scout = _scout  # type: ignore[assignment, method-assign]
    return loop


def test_a_whole_system_sets_the_galaxy_and_system_only_once() -> None:
    """**用户口径 2026-08-11 的那一条**：海盗侦查不用每颗星球都改三个坐标。

    1–4 位在同一个恒星系里，银河系与恒星系两格只该在进这个系时各设一次；
    位置那一格每一位都要设——它就是本次要读的那个字段，不能靠推断。

    改之前是 4 × 3 = 12 次字段输入（每颗星球都清一次缓存），现在是 3 + 3 = 6 次。
    一次字段输入是「点字段 → 等浮层 → 打数字 → 点 OK → 等切屏」约 3 秒，
    一个恒星系省下约 18 秒；而这条链路整晚都在一系接一系地扫。

    位 2 上有海盗、会被侦察出去——**派遣面板开过之后照样不重设**，
    那正是原先每颗星球都要付三格的来源。
    """
    driver = _NavDriver()
    loop = _sweeping_loop(driver, pirate_at=2)

    pirates, scouted = loop._find_pirates(2, 137)

    assert [c.position for c in pirates] == [2]
    assert scouted == 1
    assert driver.sets == [
        GALAXY_FIELD,
        SYSTEM_FIELD,
        POSITION_FIELD,
        POSITION_FIELD,
        POSITION_FIELD,
        POSITION_FIELD,
    ]


def test_every_position_is_still_verified_against_the_nav_bar() -> None:
    """省字段的前提是**每一位都回读核对过**，空位也不例外。

    没有这道回读，缓存就成了「我以为我打进去了」——实机上那份自信换来的是
    连续 44 个目标一路报「不是海盗」，而导航栏其实停在银河系 9。
    """
    driver = _NavDriver()
    loop = _sweeping_loop(driver, pirate_at=0)  # 一位海盗都没有
    reads: list[str] = []
    original = loop._read

    def _read(roi: tuple[int, int, int, int], **recipe: Any) -> str:
        reads.append("标题" if roi == pirate_ui.PIRATE_TITLE_ROI else "别的")
        return str(original(roi, **recipe))

    loop._read = _read  # type: ignore[assignment, method-assign]

    loop._find_pirates(2, 137)

    # 四位全是空位，一次复位重试都没有：回读每次都核对通过。
    assert reads.count("标题") == 4
    assert driver.sets.count(GALAXY_FIELD) == 1
