"""派出舰队之后的等待调度。

派出之后助手**不持有会话**。用户会切换登录去玩，助手要做的是：读出飞行时间、
算出战报大概什么时候出现、把这个时间存下来、然后完全松手。到点再回来登录收报告。

因此这里的判定必须是纯函数、且只依赖持久化过的时间——进程被关掉、机器重启、
用户占着号玩了三小时，恢复时都要能从数据库算出「现在该等还是该收」。

另一条约束：助手不能和用户抢登录。两个会话互相顶号会陷入死循环，所以拿不到会话时
退避重试，退避耗尽就安全暂停，而不是反复强登。用户有优先权。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from evo_helper.domain.bot_round import is_probe_preset
from evo_helper.domain.records import MISSION_KIND_SCOUT

#: `X天Y时Z分W秒`，缺省段会被省略（`8时3分20秒`、`45秒`）。
_CN_DURATION_RE = re.compile(
    r"(?:(\d+)\s*天)?\s*(?:(\d+)\s*时)?\s*(?:(\d+)\s*分)?\s*(?:(\d+)\s*秒)?"
)

#: 顶部栏用的 `01:53:19` 冒号格式。
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,3}):([0-5]\d):([0-5]\d)(?!\d)")

#: 飞行时间读不到（`expected_report_at_utc` 为 NULL）时，按派出时刻算的放弃阈值。
#:
#: 没有这条阈值，NULL 的派遣就既永远「可收」、又永远不被判缺失：`plan()` 见到
#: 任何一条 NULL 就无条件返回 `COLLECT`，于是调度器每个 tick 都去收一封永远不会
#: 到的战报。6 小时按实机的最长一趟取，比它还久没闭合的只可能是丢了。
#:
#: **只管 NULL 那一档。** 飞行时间读到了的，老不老由它自己的预计时间加宽限期
#: 说了算——拿派出时刻一起卡会把一发飞十小时、还没到的远征当成缺失排掉。
MAX_REPORT_AGE = timedelta(hours=6)

#: 唤醒余量：预计时间之上再等这么久才去收。
#:
#: 5 秒足够，因为预计时间本来就是**本地记的发出时刻**加上简报里读到的飞行时长
#: （`repository.record_flight_time`），不依赖任何一次现场识别。原先的 1 分钟是
#: 在没有可靠预计时间的年代留的：那时预计时间靠猜，余量得大到能盖住猜错。
#: 现在还留着 1 分钟，只是让每一份战报都白白晚收将近一分钟。
DEFAULT_MARGIN = timedelta(seconds=5)

#: 战报批量收取的分组窗口：与最早那份相差不超过这么久的，并进同一趟收。
#:
#: 每一趟收取都要 `ensure_game_window()` + 认屏 + 进信箱，中间还夹一次任务切换。
#: 10:00:00 和 10:00:30 各一份，分两趟收就是把这套开销付两遍，而并成一趟只需要
#: 多等 30 秒。60 秒是「多等一会儿」与「压着已到的战报不收」之间的取舍点。
BATCH_WINDOW = timedelta(seconds=60)

#: 首次重试等 30 秒，随后倍增。
BASE_SESSION_BACKOFF = timedelta(seconds=30)

#: 退避封顶。不能太长：战报有有效期，助手醒得太晚就读不到了。
MAX_SESSION_BACKOFF = timedelta(minutes=8)

#: 默认重试次数。超过就安全暂停，交回人工。
DEFAULT_MAX_SESSION_ATTEMPTS = 8


def line_free_at(
    dispatched_at_utc: datetime,
    flight: timedelta | None,
    *,
    mission_kind: str,
    preset_name: str,
) -> datetime | None:
    """这条航线什么时候空出来。读不到飞行时长时返回 None。

    **派出之后有两个钟，用错一个就白飞一趟舰队：**

    | 问题 | 时刻 |
    |---|---|
    | 什么时候回去收战报？ | 出发 + 飞行时长 × 1（战报在抵达时产生） |
    | 什么时候能再派？ | 出发 + 飞行时长 × 本函数给的倍数 |

    倍数按发次类型分岔：

    - **攻击发** × 2——打完还要飞回来。
    - **探路发** × 1——探路舰队会在攻击中损失，**没有返程**。
    - **侦察发** × 2——探测器会飞回来。侦察根本不选预设，所以它由
      `mission_kind` 认，且**先于**预设名判：那一发不会损失，哪怕预设名恰好
      写成了探路，也仍然要飞回来。

    返回 None 的那些**不计入在飞数**，也就是当作不占航线。这是一个自觉的
    乐观口径：估高了空闲航线，最坏结果是 runner 起来发现没位子、空跑一轮就退，
    权威闸门（`game.capacity.LineCapacityGate`，它看屏）兜得住；估低了则是
    航线空着不派，那一侧没人兜。
    """
    if flight is None:
        return None
    if mission_kind == MISSION_KIND_SCOUT:
        return dispatched_at_utc + flight * 2
    return dispatched_at_utc + (flight if is_probe_preset(preset_name) else flight * 2)


class WaitAction(Enum):
    """一个运行实例在派出之后该做什么。"""

    #: 有战报到点了，去登录收取。
    COLLECT = "collect"
    #: 都还在飞，松手等到 ``resume_at_utc``。
    WAIT = "wait"
    #: 全部闭合，运行结束。
    COMPLETE = "complete"


@dataclass(frozen=True)
class PendingReport:
    """一次已派出、尚未闭合的攻击。"""

    dispatch_id: str
    #: 预计战报出现的时间。读不到飞行时间时为 None。
    expected_report_at_utc: datetime | None
    closed: bool = False


@dataclass(frozen=True)
class WaitPlan:
    action: WaitAction
    #: 仅 ``WAIT`` 时有值：该睡到什么时候。
    resume_at_utc: datetime | None = None
    #: 供日志与界面显示的原因。
    detail: str = ""


class ReportWaitPlanner:
    """根据已派出的攻击和当前时间，决定该等还是该收。"""

    def __init__(
        self,
        margin: timedelta = DEFAULT_MARGIN,
        *,
        batch_window: timedelta = BATCH_WINDOW,
    ) -> None:
        """``margin`` 是唤醒时间上加的余量，``batch_window`` 是批量分组的宽度。

        提前登录只是白跑一趟，但每一趟都要抢一次会话——而用户可能正在玩。
        宁可晚几秒，也不要为了抢早而多顶一次号。
        """
        self._margin = margin
        self._batch_window = batch_window

    def plan(self, pending: Sequence[PendingReport], *, now_utc: datetime) -> WaitPlan:
        open_reports = [item for item in pending if not item.closed]
        if not open_reports:
            return WaitPlan(WaitAction.COMPLETE, detail="all dispatches closed")

        # 飞行时间没读到的，立即尝试收取。宁可白跑，也不能无限等一个不知道何时到的战报。
        #
        # 这一档**不参与批量分组**：它根本没有可比的到期时间，没法判断该并进哪一组。
        # 让它跟着某个邻居一起等，等于把「未知即立即收取」这条既定降级悄悄改成了延迟。
        if any(item.expected_report_at_utc is None for item in open_reports):
            return WaitPlan(WaitAction.COLLECT, detail="a dispatch has no expected report time")

        expected = sorted(
            item.expected_report_at_utc
            for item in open_reports
            if item.expected_report_at_utc is not None
        )
        # 一组 = 最早那份，加上所有与它相差不超过 `batch_window` 的。等到组里**最晚**
        # 那份到期再去收，一趟读完，省掉重复的认屏与进信箱。
        #
        # 分组按「距最早那份多远」算，**不是**一份挨一份地传递着续下去。续着算的话，
        # 每隔 59 秒来一份就能把收取无限期往后推，本该有界的等待会变成永远不收；
        # 按这个写法，等待封顶在 `batch_window + margin`。
        earliest = expected[0]
        batch = [moment for moment in expected if moment - earliest <= self._batch_window]
        collect_at = batch[-1] + self._margin
        if collect_at <= now_utc:
            return WaitPlan(WaitAction.COLLECT, detail=f"{len(batch)} report(s) due")
        return WaitPlan(
            WaitAction.WAIT,
            resume_at_utc=collect_at,
            detail=f"{len(open_reports)} report(s) still in flight",
        )


class SessionBackoff:
    """拿不到登录会话时的退避重试。"""

    def __init__(
        self,
        base: timedelta = BASE_SESSION_BACKOFF,
        max_delay: timedelta = MAX_SESSION_BACKOFF,
        max_attempts: int = DEFAULT_MAX_SESSION_ATTEMPTS,
    ) -> None:
        self._base = base
        self._max_delay = max_delay
        self._max_attempts = max_attempts

    def delay_for(self, *, attempt: int) -> timedelta:
        if attempt < 1:
            raise ValueError("attempt must be 1 or greater")
        # 2 ** (attempt - 1) 会很快溢出成巨大的 timedelta，所以先按封顶截断指数。
        shift = min(attempt - 1, 32)
        delay: timedelta = self._base * (2**shift)
        return min(delay, self._max_delay)

    def exhausted(self, *, attempt: int) -> bool:
        return attempt > self._max_attempts

    def pause_reason(self, *, attempt: int) -> str:
        return (
            f"session unavailable after {attempt} attempt(s); "
            "the account is most likely in use, so the run pauses instead of "
            "competing for the login"
        )


def parse_game_duration(text: str) -> timedelta | None:
    """解析游戏内倒计时，读不到返回 None。

    读成 0 也返回 None：那说明一个数字都没匹配到，不能当成「已抵达」——
    把 0 当成已抵达会让助手立刻去收一份还没产生的战报。
    """
    clock = _CLOCK_RE.search(text or "")
    if clock is not None:
        hours, minutes, seconds = (int(value) for value in clock.groups())
        total = timedelta(hours=hours, minutes=minutes, seconds=seconds)
        return total or None

    # 所有分段都是可选的，所以正则会在任意位置匹配到空串。逐个匹配、取第一个
    # 真的含数字的，否则 "即将抵达" 会被读成 0 秒。
    for match in _CN_DURATION_RE.finditer(text or ""):
        if not any(match.groups()):
            continue
        days, hours, minutes, seconds = (int(value or 0) for value in match.groups())
        total = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        if total:
            return total
    return None
