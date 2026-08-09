"""扫描期间的断线重连。

游戏会话会掉——用户切换登录、服务端踢线、或长时间无操作。实测一次调试里掉了四次，
所以「掉线」是常态而非异常，扫描流程必须能自己接回去，而不是中断。

设计取舍：

- **按固定间隔巡检**，而不是每一步都查。每步都 OCR 一次会把单坐标耗时抬高一截，
  而掉线是分钟级事件，10 分钟一次足够；真掉了的话，下一步操作也会立刻暴露。
- **重连只走已知的入口序列**（语言页 → 进入 → START），任何认不出的画面一律停止
  并保留证据，绝不乱点。乱点可能误触派遣、删信或领奖。
- **不与用户抢登录**。重连失败按 `SessionBackoff` 退避，退避耗尽就安全暂停。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

#: 巡检间隔（秒）。用户指定 10 分钟。
HEALTH_CHECK_INTERVAL_S = 600.0

#: 入口序列各步的等待上限（秒）。游戏加载慢，且慢得不均匀，所以轮询而非死等。
ENTRY_TIMEOUT_S = 30.0
START_LOAD_TIMEOUT_S = 90.0
START_POLL_S = 3.0


#: 判定「已在游戏内」的文字标记。
#:
#: 只收 OCR 读得稳的词。底部导航条实测读成「和量 般队 太空能 商店 联盟」——
#: 行星/舰队/太空舱 三个都会被读错，只有 商店 和 联盟 稳定；早先用前者做判据，
#: 结果在线的会话被判成「认不出」。
IN_GAME_MARKERS: tuple[str, ...] = ("商店", "联盟", "银河系", "恒星系")

#: 掉线弹窗上的字。实测原文：「连接已断开，无法重新连接。」
#:
#: ⚠️ **这一屏最危险的地方是它看起来像在线。** 弹窗是浮层，底下的导航条
#: 还完整地画在画面上，`IN_GAME_MARKERS` 里的「商店」「联盟」照样读得出来——
#: 于是会话判定给出 IN_GAME，而实际上任何点击都没有效果。
#: 所以判掉线**必须排在判 IN_GAME 之前**，和「先判入口页再判 START」同一类陷阱。
DISCONNECT_MARKERS: tuple[str, ...] = ("连接已断开", "无法重新连接")


class ScreenState(Enum):
    """重连流程认得的画面。认不出的一律 UNKNOWN，然后停止。"""

    #: 语言选择页，有「进入」按钮。
    ENTRY = "entry"
    #: 账号页，有 START。
    START = "start"
    #: 已在游戏内。
    IN_GAME = "in_game"
    #: 掉线弹窗，只有一个绿色 ✓。点掉它才能回到入口序列。
    DISCONNECTED = "disconnected"
    #: 加载转圈。
    LOADING = "loading"
    #: 认不出——必须停止并保留证据。
    UNKNOWN = "unknown"


def classify_screen(text: str) -> ScreenState:
    """按画面上的文字判断当前处于哪一屏。

    顺序有讲究，两处都是实机踩出来的：

    - **先判掉线**：掉线弹窗底下的导航条仍在画面里，后判会读出「商店/联盟」
      并给出 IN_GAME，于是助手在一个死会话上一路点下去，全程不报错。
    - **再判 START**：START 页的背景里也印着淡淡的 ETERNAL VOID。
    """
    haystack = text or ""
    if any(marker in haystack for marker in DISCONNECT_MARKERS):
        return ScreenState.DISCONNECTED
    if "START" in haystack.upper():
        return ScreenState.START
    if "ETERNAL VOID" in haystack.upper() or "点击任意位置继续" in haystack:
        return ScreenState.ENTRY
    if any(marker in haystack for marker in IN_GAME_MARKERS):
        return ScreenState.IN_GAME
    return ScreenState.UNKNOWN


@dataclass(frozen=True)
class ReconnectOutcome:
    state: ScreenState
    reconnected: bool
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.state is ScreenState.IN_GAME


class SessionKeeper:
    """按间隔巡检会话，掉线时走已知入口序列接回去。"""

    def __init__(
        self,
        *,
        observe: Callable[[], ScreenState],
        click_entry: Callable[[], None],
        click_start: Callable[[], None],
        dismiss_disconnect: Callable[[], None] | None = None,
        interval_s: float = HEALTH_CHECK_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._observe = observe
        self._click_entry = click_entry
        self._click_start = click_start
        self._dismiss_disconnect = dismiss_disconnect
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_check: float | None = None

    def due(self) -> bool:
        """距上次巡检是否已满一个间隔。首次调用必定为真。"""
        if self._last_check is None:
            return True
        return self._clock() - self._last_check >= self._interval

    def ensure_connected(self, *, force: bool = False) -> ReconnectOutcome | None:
        """到点就巡检；掉线则重连。未到点且未强制时返回 None。"""
        if not force and not self.due():
            return None
        self._last_check = self._clock()
        return self.reconnect()

    def reconnect(self) -> ReconnectOutcome:
        state = self._observe()
        if state is ScreenState.IN_GAME:
            return ReconnectOutcome(state, reconnected=False, detail="session still alive")

        if state is ScreenState.UNKNOWN:
            # 认不出的画面不乱点：可能是弹窗、维护公告或改版。
            return ReconnectOutcome(state, reconnected=False, detail="unrecognised screen")

        if state is ScreenState.DISCONNECTED:
            if self._dismiss_disconnect is None:
                # 没给关闭动作就停在这里，而不是把掉线当成「认不出」——
                # 前者说得清「卡在哪一屏」，后者查起来只能翻截图。
                return ReconnectOutcome(
                    state, reconnected=False, detail="disconnected dialog with no way to dismiss it"
                )
            self._dismiss_disconnect()
            # 点掉弹窗之后回到的是入口序列的某一屏，具体是哪一屏不假设——
            # 重新观察，让判据来自画面本身。
            state = self._wait_for(
                {ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME}, ENTRY_TIMEOUT_S
            )

        if state is ScreenState.ENTRY:
            self._click_entry()
            # 固定等待不够：切屏时间不稳定，等 4 秒常常还停在入口页，
            # 于是整条序列在第一步就断掉。改为轮询到出现 START。
            state = self._wait_for({ScreenState.START, ScreenState.IN_GAME}, ENTRY_TIMEOUT_S)

        if state is ScreenState.START:
            self._click_start()
            state = self._wait_for_game()

        if state is ScreenState.IN_GAME:
            return ReconnectOutcome(state, reconnected=True, detail="re-entered the session")
        return ReconnectOutcome(
            state, reconnected=False, detail="entry sequence did not reach the game"
        )

    def _wait_for_game(self) -> ScreenState:
        return self._wait_for({ScreenState.IN_GAME}, START_LOAD_TIMEOUT_S)

    def _wait_for(self, wanted: set[ScreenState], timeout_s: float) -> ScreenState:
        """轮询直到出现目标画面或超时。

        过渡中的画面 OCR 读出来是花的，会被判成 UNKNOWN——那只说明「此刻读不清」，
        不等于「这一屏认不出」。所以轮询期间容忍 UNKNOWN 继续等，超时才算失败。
        安全性不受影响：这里只是等待，从不在 UNKNOWN 上点击；点击前的那次判定
        （``reconnect`` 开头）仍然一遇 UNKNOWN 就停。
        """
        deadline = self._clock() + timeout_s
        state = self._observe()
        while self._clock() < deadline:
            if state in wanted:
                return state
            self._sleep(START_POLL_S)
            state = self._observe()
        return state
