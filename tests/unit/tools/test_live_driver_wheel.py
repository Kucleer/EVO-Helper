"""驱动发出去的滚轮事件必须是「单格」，而且 pyautogui 的全局 `PAUSE` 必须被关掉。

⚠️ 这三条盯的是**同一种静默故障**：事件发出去了、钩子也收到了、列表却没走。
实测过的两种发法（`scroll(-1)` 一格不足、`scroll(-800)` 单事件被封顶）都不抛异常，
所以只能靠断言「发出去的到底是什么」来拦。
"""

import sys
import types
from typing import Any

import pytest

from evo_helper.game.ranking_ui import WHEEL_DELTA
from evo_helper.tools.scan_coordinates import SlowDragDriver


class _StubLive:
    """`LiveDriver` 的最小替身。

    `wheel_notch` **有意不碰这两个方法**（16ms 的循环里抢不起前台、也取不起原点，
    见它的注释），留着它们是为了让「哪天有人往里加一次 focus()」当场露出来。
    """

    def __init__(self) -> None:
        self.focused = 0
        self.origins = 0

    def focus(self, *, attempts: int = 5) -> None:
        self.focused += 1

    def origin(self) -> tuple[int, int]:
        self.origins += 1
        return (0, 0)


def _stub_live_driver() -> Any:
    return _StubLive()


@pytest.fixture
def fake_pyautogui(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """替掉 `sys.modules['pyautogui']`——`wheel_notch` 是在方法里 import 的。"""
    module = types.ModuleType("pyautogui")
    module.PAUSE = 0.1  # type: ignore[attr-defined]
    module.FAILSAFE = True  # type: ignore[attr-defined]
    module.scrolled = []  # type: ignore[attr-defined]
    module.scroll = lambda clicks: module.scrolled.append(clicks)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyautogui", module)
    return module


def test_wheel_notch_sends_exactly_one_standard_notch(fake_pyautogui: Any) -> None:
    # ⚠️ 必须是 -120，不是 -1：`pyautogui.scroll(n)` 在 Windows 上把 n 原样当
    # `dwData`，不乘 120。发 -1 只是 1/120 格，实测 80 次只走 0-3 行。
    driver = SlowDragDriver(_stub_live_driver())
    driver.wheel_notch()
    assert fake_pyautogui.scrolled == [-WHEEL_DELTA]


def test_wheel_notch_never_batches_notches_into_one_big_event(fake_pyautogui: Any) -> None:
    # ⚠️ 单个事件的幅度会被游戏封顶（实测 100/400/800 格都只走约 14px），
    # 而封顶是静默的。所以拨 3 格就得是 3 个 -120，不是 1 个 -360。
    driver = SlowDragDriver(_stub_live_driver())
    for _ in range(3):
        driver.wheel_notch()
    assert fake_pyautogui.scrolled == [-WHEEL_DELTA] * 3


def test_wheel_notch_scrolls_down_not_up(fake_pyautogui: Any) -> None:
    # 符号反了就是往上滚：榜单会退回榜首，而盲滚段不读屏，没人会发现。
    SlowDragDriver(_stub_live_driver()).wheel_notch()
    assert fake_pyautogui.scrolled[0] < 0


def test_wheel_notch_zeroes_the_pause_while_it_scrolls(fake_pyautogui: Any) -> None:
    # ⚠️ PAUSE=0.1 会把 16ms 的间隔撑成 117ms，动量攒不起来——
    # 症状是「拨了但没走」，和发不足一格一模一样（实测 80 格只走 2 行）。
    seen: list[float] = []
    fake_pyautogui.scroll = lambda clicks: seen.append(fake_pyautogui.PAUSE)
    SlowDragDriver(_stub_live_driver()).wheel_notch()
    assert seen == [0], "发滚轮的那一刻 PAUSE 必须是 0，否则攒不起动量"


def test_wheel_notch_puts_the_pause_back(fake_pyautogui: Any) -> None:
    """⚠️ 2026-08-22 生产事故：`PAUSE = 0` 泄漏出去，把检测段的慢拖打坏了。

    原先这里是裸赋值、永不恢复，于是盲滚跑完之后同进程**所有** pyautogui 调用都
    没有停顿——包括分步慢拖。而本仓早就记着「一步到位的拖动会被游戏面板当成点击」：
    一屏 14 个调用 × 0.1 秒的节奏被抹平，游戏就把整段拖动当点击，**列表一行不动**。

    生产日志的算术：出事那轮检测段 2.95 秒/屏 vs 改动前盲拖 4.21 秒/屏，
    差 1.26 秒 ≈ 14 × 0.1。症状是「翻了 30 屏、名字列重合率 0.97」。
    """
    fake_pyautogui.PAUSE = 0.1
    SlowDragDriver(_stub_live_driver()).wheel_notch()
    assert fake_pyautogui.PAUSE == 0.1, "PAUSE 没还回去：后面每一次慢拖都会被当成点击"


def test_wheel_notch_puts_the_pause_back_even_when_scroll_raises(fake_pyautogui: Any) -> None:
    # FAILSAFE 就是从 scroll() 里抛出来的——那条最该恢复的路径不能漏。
    def boom(clicks: int) -> None:
        raise RuntimeError("failsafe")

    fake_pyautogui.PAUSE = 0.1
    fake_pyautogui.scroll = boom
    with pytest.raises(RuntimeError):
        SlowDragDriver(_stub_live_driver()).wheel_notch()
    assert fake_pyautogui.PAUSE == 0.1


def test_wheel_notch_keeps_failsafe_on(fake_pyautogui: Any) -> None:
    # 一趟盲滚要发几百个事件，急停（鼠标甩到左上角）是唯一的人工刹车。
    SlowDragDriver(_stub_live_driver()).wheel_notch()
    assert fake_pyautogui.FAILSAFE is True


def test_wheel_notch_does_not_grab_the_foreground_per_notch(fake_pyautogui: Any) -> None:
    # ⚠️ `focus()` 抢不到会退避重试最多 4.5 秒、`origin()` 会在窗口不见时把游戏
    # 重新拉起来——两者都塞不进 16ms 的循环。落点由上一个动作负责。
    live = _StubLive()
    driver = SlowDragDriver(live)
    for _ in range(5):
        driver.wheel_notch()
    assert live.focused == 0
    assert live.origins == 0


def test_slow_drag_driver_satisfies_the_ranking_driver_protocol() -> None:
    # 协议是结构化的，运行时不校验；漏了 `wheel_notch` 只会在实机上炸。
    from evo_helper.game.ranking_nav import RankingDriver

    driver: RankingDriver = SlowDragDriver(_stub_live_driver())
    assert callable(driver.wheel_notch)
