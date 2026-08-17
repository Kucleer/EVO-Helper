"""扫描期间的断线重连。

游戏会话会掉——用户切换登录、服务端踢线、或长时间无操作。实测一次调试里掉了四次，
所以「掉线」是常态而非异常，扫描流程必须能自己接回去，而不是中断。

设计取舍：

- **按固定间隔巡检**，而不是每一步都查。每步都 OCR 一次会把单坐标耗时抬高一截，
  而掉线是分钟级事件，10 分钟一次足够；真掉了的话，下一步操作也会立刻暴露。
- **重连只走已知的入口序列**（语言页 → 进入 → START），任何认不出的画面一律停止
  并保留证据，绝不乱点。乱点可能误触派遣、删信或领奖。
- **不与用户抢登录**。重连失败按 `SessionBackoff` 退避，退避耗尽就安全暂停。
- **掉线分两种，善后完全不同。** 「连接已断开」点掉弹窗还能接回去；
  「连接已断开，**无法重新连接**」是页面自己宣告没救了，点掉弹窗照样回不去，
  只能关掉窗口重开 Chrome。后者有次数上限，理由见 `MAX_WINDOW_RESTARTS`。
- **「关窗重开」不只服务于掉线。** 画面上一个「掉线」字样都没有、但视图就是切不
  回来的时候，调用方原本只能就地抛异常停摆（实机 2026-08-11：读完邮件切不回恒星
  系视图，整轮退出码 1）。`restart_and_reenter` 就是给这些调用方的同一个出口——
  用户口径是「切不回就重启，这是兜底策略」。它和掉线那条**共用同一份重开配额**，
  否则服务端维护时两条路各开各的，配额就拦不住无限重启。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

#: 巡检间隔（秒）。用户指定 10 分钟。
#:
#: 分类（2026-08-17 审计）：**低优先级旋钮**——它是一个真的取舍（查得勤些能早点
#: 发现掉线，代价是每次都要 OCR 一屏），但掉线是分钟级事件、而下一步操作本来就会
#: 立刻暴露掉线，所以这个数往哪边挪都看不出差别。没做成可配置。
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

#: 掉线弹窗上的字，**还能接回去的那一种**。
#:
#: ⚠️ **这一屏最危险的地方是它看起来像在线。** 弹窗是浮层，底下的导航条
#: 还完整地画在画面上，`IN_GAME_MARKERS` 里的「商店」「联盟」照样读得出来——
#: 于是会话判定给出 IN_GAME，而实际上任何点击都没有效果。
#: 所以判掉线**必须排在判 IN_GAME 之前**，和「先判入口页再判 START」同一类陷阱。
DISCONNECT_MARKERS: tuple[str, ...] = ("连接已断开",)

#: 同一个蓝色弹窗，但页面自己宣告**接不回去了**。实测原文（2026-08-11，
#: 截图存在 `var/logs/now-check.png`）：「连接已断开，无法重新连接。」
#:
#: 善后跟上面那种**完全不同**：那一种点掉绿色 ✓ 就能回到入口序列；这一种
#: 页面已经死了，点掉弹窗照样回不去。实机表现就是反复走「点弹窗 → 等入口页」
#: 这条恢复序列却始终回不到游戏内，最后报「会话不可用」。唯一的出路是关掉
#: 窗口重开 Chrome——见 `SessionKeeper._restart_now`。
DEAD_SESSION_MARKERS: tuple[str, ...] = ("无法重新连接",)

#: 服务器维护公告的标题。
#:
#: ⚠️ **2026-08-15 03:30 实机：这一屏把整晚堵死了。** 服务器停机维护，游戏弹出
#: 一张公告盖在 START 页上，而助手完全不认识它——`START_ROI` 那个位置上坐着的
#: 是公告的「知道了」按钮，于是 `start_button` 一遍遍读到「知道了」、一遍遍
#: 判「读不出 START」，bot 链路就那么空转了二十分钟。
#:
#: 判据用标题而不是正文：正文是整段话，OCR 出来碎；标题「服务器维护」四个字
#: 在一条独立的横栏里，读得稳。
MAINTENANCE_MARKERS: tuple[str, ...] = ("服务器维护", "服务器维")

#: 入口页**独有**的记号。
#:
#: ⚠️ 入口页和 START 页在文字上是互相污染的：START 页的背景里印着淡淡的
#: `ETERNAL VOID`，而入口页底下又透着淡淡的 `START`。所以任何一边只靠「对方也有
#: 的那个词」都判不准，先后顺序换来换去只是把错判从一边挪到另一边。
#:
#: 出路是**先判只有入口页才有的东西**：语言选择页上的「进入」按钮，和它下面那句
#: 「点击任意位置继续」。这两个 START 页上都没有。判完它们再判 START，最后才把
#: `ETERNAL VOID` 当弱证据兜底。
ENTRY_MARKERS: tuple[str, ...] = ("进入", "点击任意位置继续")

#: 弱证据：START 页背景里也有它，所以只能排在判完 START 之后。
ENTRY_WEAK_MARKERS: tuple[str, ...] = ("ETERNAL VOID",)

#: 关窗重开的次数上限，以及配额的滚动周期。
#:
#: **上限跟重开本身一样重要。** 服务端维护时每次巡检都会撞到这一屏，没有上限
#: 就成了「每 10 分钟关一次 Chrome 再开一次」，一直折腾到有人来看——那比不重开
#: 更糟：不重开只是停下来，无限重开会一直抢用户的桌面和前台。
#:
#: 取 3 次 / 1 小时：巡检间隔是 10 分钟，一小时最多撞 6 次，3 次意味着
#: 「连着两次重开都没救回来就不是偶发，别再试了」，同时给真正的偶发（一次重开
#: 就好）留足余量。用**滚动窗口**而不是整点清零：整点清零会在小时交界处放出
#: 双倍配额（59 分连开 3 次，01 分又是 3 次）。
#:
#: 分类（2026-08-17 审计）：**低优先级旋钮**——「几次算不是偶发」确实有主观成分，
#: 但这个数还兼着另一个身份：`ReconnectOutcome.restarts_left` 拿它当
#: 「这次故障是不是暂时的」的现成度量，调它会同时改变**退出码语义**
#: （`EXIT_ENVIRONMENT_BUSY` 还是硬失败），而那一侧再往下接的是环境故障豁免。
#: 一个框改两件事、其中一件用户根本看不见——所以没做成可配置。
MAX_WINDOW_RESTARTS = 3
RESTART_BUDGET_WINDOW_S = 3600.0

#: 重开之后等新窗口把入口页画出来的上限。冷启动要开 Chrome、下载资源、初始化
#: WebGL，比「点掉弹窗回到入口页」慢一个量级，所以不共用 `ENTRY_TIMEOUT_S`。
RESTART_ENTRY_TIMEOUT_S = 120.0


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
    #: 同一个弹窗，但写着「无法重新连接」——点掉也回不去，只能关窗重开。
    DEAD_SESSION = "dead_session"
    #: 加载转圈。
    LOADING = "loading"
    #: 服务器维护公告，只有一个「知道了」。点掉它才能回到入口序列。
    MAINTENANCE = "maintenance"
    #: 认不出——必须停止并保留证据。
    UNKNOWN = "unknown"


def classify_screen(text: str) -> ScreenState:
    """按画面上的文字判断当前处于哪一屏。

    顺序有讲究，三处都是实机踩出来的：

    - **先判「无法重新连接」，再判「连接已断开」**：可恢复那条的文案
      （「连接已断开」）是不可恢复那条（「连接已断开，无法重新连接。」）的
      **前缀**，反过来判就永远走不到重开那一支——现象是助手一遍遍点掉弹窗、
      一遍遍等不到入口页，最后报「会话不可用」，而它其实只需要重开一次窗口。
    - **再判掉线**：掉线弹窗底下的导航条仍在画面里，后判会读出「商店/联盟」
      并给出 IN_GAME，于是助手在一个死会话上一路点下去，全程不报错。
    - **入口页独有的记号排在 START 之前**：见 `ENTRY_MARKERS` 的说明——两屏在文字上
      互相污染，只有「进入」「点击任意位置继续」是入口页独占的。
    - **然后判 START**，最后才用 `ETERNAL VOID` 这个弱证据兜底：START 页的背景里
      也印着它。
    """
    haystack = text or ""
    # 维护公告排在最前：它是浮层，底下的 START / 导航条照样读得出来，
    # 后判就会把一台停机的服务器认成「在 START 页上」，然后一路点下去。
    # 同一条道理写在下面掉线那一段里，这是它的第二个实例。
    if any(marker in haystack for marker in MAINTENANCE_MARKERS):
        return ScreenState.MAINTENANCE
    if any(marker in haystack for marker in DEAD_SESSION_MARKERS):
        return ScreenState.DEAD_SESSION
    if any(marker in haystack for marker in DISCONNECT_MARKERS):
        return ScreenState.DISCONNECTED
    # 入口页独有的记号排在 START 之前，理由见 `ENTRY_MARKERS`。
    if any(marker in haystack for marker in ENTRY_MARKERS):
        return ScreenState.ENTRY
    if "START" in haystack.upper():
        return ScreenState.START
    if any(marker in haystack.upper() for marker in ENTRY_WEAK_MARKERS):
        return ScreenState.ENTRY
    if any(marker in haystack for marker in IN_GAME_MARKERS):
        return ScreenState.IN_GAME
    return ScreenState.UNKNOWN


@dataclass(frozen=True)
class ReconnectOutcome:
    state: ScreenState
    reconnected: bool
    detail: str = ""
    #: 这一刻**还剩几次关窗重开的配额**（滚动窗口内，见 `MAX_WINDOW_RESTARTS`）。
    #:
    #: 它是给调用方回答一个问题的：巡检没能回到游戏内时，这一轮该按
    #: 「环境暂时不行、会自己好」收场（`EXIT_ENVIRONMENT_BUSY`），还是按硬失败
    #: 收场？判据就是它——**配额本身就是「这是不是暂时的」的现成度量**：
    #: 还有配额说明这条恢复阶梯还没走到头，下一轮再试有意义；配额耗尽还是回不去，
    #: 说明重开这条路已经证明救不了，得让连续失败计数看见它。
    #:
    #: ⚠️ **默认 0，也就是「没配额」**。默认值必须倒向「按硬失败收场」那一侧：
    #: 判错成 75 的代价是一个**静默死循环**（不计故障、不报警、停顿看门狗也接不住，
    #: 因为每轮几十秒就干净退出），判错成 1 的代价只是多攒几次失败计数。
    restarts_left: int = 0

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
        dismiss_notice: Callable[[], None] | None = None,
        restart_window: Callable[[], None] | None = None,
        max_restarts: int = MAX_WINDOW_RESTARTS,
        restart_budget_window_s: float = RESTART_BUDGET_WINDOW_S,
        log: Callable[[str], None] | None = None,
        interval_s: float = HEALTH_CHECK_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._observe = observe
        self._click_entry = click_entry
        self._click_start = click_start
        self._dismiss_disconnect = dismiss_disconnect
        self._dismiss_notice = dismiss_notice
        # 关窗重开真的会动系统，所以它是**注入进来的**：测试注入假的，
        # 于是单元测试永远碰不到真窗口。默认 None = 不会重开，只会停下来报告。
        self._restart_window = restart_window
        self._max_restarts = max_restarts
        self._restart_budget_window_s = restart_budget_window_s
        self._log = log or (lambda _message: None)
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_check: float | None = None
        #: 最近若干次重开的时刻，用来算滚动配额。
        self._restarts: list[float] = []

    def due(self) -> bool:
        """距上次巡检是否已满一个间隔。首次调用必定为真。"""
        if self._last_check is None:
            return True
        return self._clock() - self._last_check >= self._interval

    def restarts_left(self) -> int:
        """滚动窗口里还剩几次关窗重开的配额。

        **没注入重开动作时恒为 0**：那种配置下重开这条路压根不存在，说「还有配额」
        就是骗调用方「再试一轮会好」。见 `ReconnectOutcome.restarts_left` 里
        为什么默认值要倒向这一侧。
        """
        if self._restart_window is None:
            return 0
        self._forget_expired_restarts()
        return max(0, self._max_restarts - len(self._restarts))

    def _forget_expired_restarts(self) -> None:
        """把滚出窗口的那几次重开忘掉。滚动窗口而不是整点清零，理由见常量。"""
        now = self._clock()
        self._restarts = [at for at in self._restarts if now - at < self._restart_budget_window_s]

    def _report(self, state: ScreenState, *, reconnected: bool, detail: str) -> ReconnectOutcome:
        """出结局。**只此一处**，好让 `restarts_left` 不会在某条分支上漏填。"""
        return ReconnectOutcome(
            state,
            reconnected=reconnected,
            detail=detail,
            restarts_left=self.restarts_left(),
        )

    def ensure_connected(self, *, force: bool = False) -> ReconnectOutcome | None:
        """到点就巡检；掉线则重连。未到点且未强制时返回 None。"""
        if not force and not self.due():
            return None
        self._last_check = self._clock()
        return self.reconnect()

    def reconnect(self) -> ReconnectOutcome:
        state = self._observe()
        if state is ScreenState.IN_GAME:
            return self._report(state, reconnected=False, detail="session still alive")

        if state is ScreenState.UNKNOWN:
            # 认不出的画面不乱点：可能是弹窗、维护公告或改版。
            return self._report(state, reconnected=False, detail="unrecognised screen")

        restarted = False
        if state is ScreenState.DEAD_SESSION:
            # 页面自己写着「无法重新连接」：入口序列救不了它，只能关窗重开。
            refusal = self._restart_now("读到「无法重新连接」：会话已死，点掉弹窗也回不去")
            if refusal is not None:
                return refusal
            restarted = True
            state = self._wait_after_restart()

        return self._walk_entry_sequence(state, restarted=restarted)

    def restart_and_reenter(self, reason: str) -> ReconnectOutcome:
        """不是掉线，但画面已经没救了——关窗重开，再走一遍入口序列。

        `reconnect` 只在读到「无法重新连接」时才重开，可实机上还有另一类死法：
        画面上一个「掉线」字样都没有，`classify_screen` 甚至给出 IN_GAME，但视图
        就是切不回去。调用方原本只能就地抛异常，整轮停摆（2026-08-11：读完邮件
        切不回恒星系视图，退出码 1）。用户口径是「切不回就重启，这是兜底策略」。

        **配额和日志与掉线那条完全共用**（`_restart_now`）。服务端维护时两条路
        都会撞上，各记各的账就等于把上限翻倍，正是 `MAX_WINDOW_RESTARTS` 要防的。

        `reason` 是给日志和结局用的人话，说明「是什么把它逼到要重开」。

        ⚠️ 重开之后**不假定**自己已经在游戏内：新窗口停在入口页，照样得走一遍
        判据驱动的入口序列，认不出就停。「认不出的画面绝不点击」在这条路上一样成立。
        """
        refusal = self._restart_now(reason, refusal_state=ScreenState.UNKNOWN)
        if refusal is not None:
            return refusal
        return self._walk_entry_sequence(self._wait_after_restart(), restarted=True)

    def _wait_after_restart(self) -> ScreenState:
        """等新窗口把画面画出来。

        新窗口停在入口页，不是游戏内——必须重走一遍入口序列，所以这里只等到入口
        序列的某一屏，剩下的交给 `_walk_entry_sequence`。
        """
        return self._wait_for(
            {ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME},
            RESTART_ENTRY_TIMEOUT_S,
        )

    def _walk_entry_sequence(self, state: ScreenState, *, restarted: bool) -> ReconnectOutcome:
        """从当前这一屏走完入口序列：关弹窗 →「进入」→ START → 游戏内。

        掉线重连和关窗重开的收尾是**同一段**，所以只有这一份。这里面有几条来之
        不易的细节（固定等待不够、要轮询到出现 START），复制一份就等于把它们
        留在一份里、丢在另一份里。
        """
        if state is ScreenState.MAINTENANCE:
            if self._dismiss_notice is None:
                return self._report(
                    state, reconnected=False, detail="maintenance notice with no way to dismiss it"
                )
            self._dismiss_notice()
            # 点掉之后回到入口序列的某一屏，具体哪一屏不假设——重新观察。
            # ⚠️ **服务器可能还没起来**：维护中点掉公告只会回到 START，而 START
            # 点下去登不进。那时下面的 `_wait_for_game` 会超时，如实报出去，
            # 由调度器按失败处理并稍后重试——这正是我们要的，而不是死循环。
            state = self._wait_for(
                {ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME}, ENTRY_TIMEOUT_S
            )

        if state is ScreenState.DISCONNECTED:
            if self._dismiss_disconnect is None:
                # 没给关闭动作就停在这里，而不是把掉线当成「认不出」——
                # 前者说得清「卡在哪一屏」，后者查起来只能翻截图。
                return self._report(
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
            detail = (
                "restarted the game window and re-entered the session"
                if restarted
                else "re-entered the session"
            )
            if restarted:
                self._log("重开之后已经重新进到游戏内")
            return self._report(state, reconnected=True, detail=detail)
        if restarted:
            self._log("重开之后仍然没能走完入口序列；停止并保留现场")
        return self._report(
            state, reconnected=False, detail="entry sequence did not reach the game"
        )

    def _restart_now(
        self,
        reason: str,
        *,
        refusal_state: ScreenState = ScreenState.DEAD_SESSION,
    ) -> ReconnectOutcome | None:
        """关窗重开一次。成功发起返回 None，拒绝或失败则返回要上报的结局。

        **重开是有代价的动作，所以每一步都要说话。** 静默重启看起来就是
        「窗口莫名其妙自己关了又开」，事后从日志里根本看不出发生过什么。

        **这是唯一一处重开入口，两条路（死会话 / 视图恢复不了）都从这里走。**
        配额、日志、失败处理因此天然共用；另开一份计数就等于把上限翻倍。
        `reason` 由调用方给，好让日志说清是哪一条路把它逼到要重开。
        """
        if self._restart_window is None:
            # 没给重开动作就停在这里，而不是退回去点弹窗——那条路已经证明没用。
            return self._report(
                refusal_state,
                reconnected=False,
                detail="no way to restart the game window",
            )

        self._forget_expired_restarts()
        now = self._clock()
        minutes = self._restart_budget_window_s / 60
        if len(self._restarts) >= self._max_restarts:
            self._log(
                f"{reason}，但 {minutes:.0f} 分钟内已经重开过 "
                f"{len(self._restarts)} 次（上限 {self._max_restarts}）；"
                "多半是服务端在维护，不再重开，安全停止"
            )
            return self._report(
                refusal_state,
                reconnected=False,
                detail=(
                    f"restart budget exhausted: {len(self._restarts)}/{self._max_restarts} "
                    f"restarts within {self._restart_budget_window_s:.0f}s"
                ),
            )

        self._restarts.append(now)
        self._log(
            f"{reason}。"
            f"关掉游戏窗口并重开 Chrome（{minutes:.0f} 分钟内第 "
            f"{len(self._restarts)}/{self._max_restarts} 次）"
        )
        try:
            self._restart_window()
        except Exception as failure:  # noqa: BLE001 - 重开失败要上报，不能把调用方拖崩
            # 配额已经记上了：重开失败也算用掉一次，否则一个必然失败的重开
            # 会被无限重试，正是这里要防的那种循环。
            self._log(f"关窗重开失败：{failure}；停止而不是接着重试")
            return self._report(
                refusal_state,
                reconnected=False,
                detail=f"restarting the game window failed: {failure}",
            )
        self._log("窗口已重开；新窗口停在入口页，重新走「进入」→ START")
        return None

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
