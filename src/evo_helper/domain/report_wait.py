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

from evo_helper.domain.fleet_preset import is_probe_preset
from evo_helper.domain.records import MISSION_KIND_ATTACK, MISSION_KIND_SCOUT

#: `X天Y时Z分W秒`，缺省段会被省略（`8时3分20秒`、`45秒`）。
#:
#: ⚠️ **每一段都是可选的，所以这条正则在任意位置都能匹配空串。** 它本身
#: 不提供任何「读全了」的保证；判「读全了」的是 `_reads_the_whole_duration()`。
#:
#: 段与段之间只允许空白，**故意不容忍噪声**：容忍噪声就等于允许把一个数字
#: 配到隔了一段距离的单位上，而那正是本模块最怕的错误（数量级错位）。
#: 段间插进了怪字符的输入一律读不出来——理由见 `parse_game_duration()`。
_CN_DURATION_RE = re.compile(
    r"(?:(\d+)\s*天)?\s*(?:(\d+)\s*时)?\s*(?:(\d+)\s*分)?\s*(?:(\d+)\s*秒)?"
)

#: 分段的单位字。匹配之外还留着一个，就说明有一段没被读进来。
_DURATION_UNITS = frozenset("天时分秒")

_DIGIT_RE = re.compile(r"\d")

#: 顶部栏用的 `01:53:19` 冒号格式。
#:
#: **这条不跟着 `_CN_DURATION_RE` 一起收紧**：它三段全是必需的、且两侧有
#: `(?<!\d)`/`(?!\d)` 守着，本来就没有「匹配上半截」这回事。
_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,3}):([0-5]\d):([0-5]\d)(?!\d)")

#: 攻击派遣的飞行时长下限：读出来比这还短的，当**没读出来**处理。
#:
#: 这是**第二道防线**，兜的是「解析器挑不出毛病、值却仍然错」的那一类。
#: 第一道（`parse_game_duration()` 的读全校验）从根上不产生截断值，但它只认
#: 得出「有一段没读进来」的痕迹；痕迹被 OCR 一并抹掉时，剩下的碎片自身是
#: 一条合法的时长，解析器没有任何依据拒绝它。
#:
#: 取 3 分钟的依据（生产库 `attack_dispatches`，2026-08-13）：
#:
#: - 攻击的 197 条飞行时长分成两簇，中间 60–300 秒**一条都没有**：
#:   0–60 秒 66 条（最大值 **59 秒**）、300 秒以上 131 条（最小值 **300 秒**）。
#: - 59 不是任何物理量，它是**一个「秒」字段能装下的最大数**——这就是
#:   「只剩秒段活下来」的铁证。300 则正好是当前科技下的真实下限（5 分钟）。
#: - 卡在 3 分钟而不是 5 分钟：科技会升级、舰队会变快，真实下限会往下走，
#:   而 180 秒落在那段空白的正中，两边都留着余量。
#:
#: **只对攻击成立，因为只有攻击那一簇被量过。** 同一张表里 371 发侦察落在
#: 14–135 秒，但那批数字**不能当成侦察的真实量程**：最久的几发全是 135 秒
#: （= 2 分 15 秒）、次一批全是 121 秒（= 2 分 1 秒），都是「分+秒」两段的
#: 形状，而飞得最久的那几发打的偏偏是主星系内最近的目标（2:137:1~4）——距离
#: 完全解释不了。它们本身多半就是本模块修的那种截断产物（真值可能是
#: `1时2分15秒`）。拿它们反推一条侦察下限，等于把截断读数当成基准。
#:
#: 所以侦察这一侧**先不设下限、也不做回归**：读全校验那一道对所有发次一视同仁
#: 已经生效，攻击这一半是要及时处理的那一半。
MIN_CREDIBLE_ATTACK_FLIGHT = timedelta(minutes=3)

#: 同一个恒星系之内那一趟攻击要多久——也就是**攻击飞行时间出现过的最低值**。
#:
#: 用户口径（2026-08-13）：「同星系是 5 分钟，也就是出现过的最低值。」
#:
#: 拿它当**跨恒星系**那一档的下限（严格大于），见 `vet_flight_time`：更远不可能
#: 更快。这一条不需要速度模型——而速度确实会变（简报上那行 `速度: 14.520` 随舰队
#: 组成而不同，本仓根本没读它），所以任何按距离插值的估算都是靠不住的，
#: 「不可能比最近的那一档还快」却始终成立。
#:
#: 用户实拍参考（同银河系、从主星发出，主星带银河石加成）：
#:
#:     跨 50 个恒星系   飞行 23 分 13 秒
#:     跨 100 个恒星系  飞行 29 分 8 秒
#:
#: 注意**不是线性的**：距离翻倍只多两成半。所以别把这两个点连成一条直线去外推，
#: 那样得到的下限在近距离处会高得离谱、把正常值判死。
SAME_SYSTEM_ATTACK_FLIGHT = timedelta(minutes=5)

#: 飞行时间读不到（`expected_report_at_utc` 为 NULL）时，按派出时刻算的放弃阈值。
#:
#: 没有这条阈值，NULL 的派遣就既永远「可收」、又永远不被判缺失：`plan()` 见到
#: 任何一条 NULL 就无条件返回 `COLLECT`，于是调度器每个 tick 都去收一封永远不会
#: 到的战报。6 小时按实机的最长一趟取，比它还久没闭合的只可能是丢了。
#:
#: **只管 NULL 那一档。** 飞行时间读到了的，老不老由它自己的预计时间加宽限期
#: 说了算——拿派出时刻一起卡会把一发飞十小时、还没到的远征当成缺失排掉。
MAX_REPORT_AGE = timedelta(hours=6)

#: 飞行时间读不到（`line_free_at_utc` 为 NULL）时，这条航线按**派出时刻**起算的
#: 占用时长。
#:
#: **NULL 的语义是「不知道什么时候回来」，不是「没占航线」。** 被游戏接受的那一发
#: 舰队一定占着一条航线，简报上读没读到飞行时间和它占不占位毫无关系。此前这一档
#: 按「不占」记，于是每一发读不出飞行时间的派遣都让调度器凭空多出一条空闲航线，
#: 到点就起一轮、导航几十秒、撞上游戏的「同时派遣的舰队数量已达上限。」、退出、
#: 冷却、再来。
#:
#: **取值不能借 `MAX_REPORT_AGE`（6 小时）。** 那 6 小时是「等一封战报等到什么
#: 时候就死心」的上界，也是 `pirate_loop.MAX_CREDIBLE_FLIGHT` 那道「OCR 读出来的
#: 数字大到这个份上一定是读错了」的量级闸门——两者都是**离谱值的天花板**，不是
#: 对「一支舰队占多久航线」的估计。这两条链路打的是同系目标，往返按分钟计：
#: 仓库里最长的一份实测简报是 28 分 21 秒（还是一趟深空探索），生产库里 236 条
#: 有航线钟的派遣，实际占用时长中位数 48 秒、最长 62.6 分钟。
#:
#: 借来的 6 小时会把一次读不到直接放大成一次停摆。实机 2026-08-11：08:48–10:07
#: 之间 6 发 bot 攻击都没读到飞行时间，正好等于 `fleet_line_limit`，于是从 10:07
#: 起 `free_lines` 恒为 0；而航线满了就不再派遣，也就再没有新证据能推翻这个估算，
#: 唯一的出口是熬到第一发满 6 小时（14:48）。页面上两条攻击链路一齐显示「等航线」、
#: 调度器「空转中」，实际上那 6 支舰队早在 11:10 前就全回来了。
#:
#: 90 分钟 = 实测最长往返（62.6 分钟）留四成余量。估短了的代价有界且自纠：
#: runner 的 `game.capacity.LineCapacityGate` 看屏复核，撞上限就正常收尾，
#: `domain.scheduler.waiting_for_a_line` 再把这条链路压到有航线真的空出来为止。
#: 估长了的代价则是上面那种没有出口的停摆。
#:
#: ⚠️ **这是「没配置时」的默认值，不是唯一取值。** 它是一个**运维旋钮**——上面
#: 那个「四成余量」是拍出来的估算，取值取决于用户当下的舰队速度与激进程度，
#: 没有唯一正确答案。攻击配置页上有一个框
#: （`military_attack_config.unknown_line_hold_minutes`），留空才走这里。
#: 因此**这个数只该写在这一处**：`repository.count_inflight` /
#: `release_held_lines` 都拿它当参数默认值，谁也不再抄一遍数字。
UNKNOWN_LINE_HOLD = timedelta(minutes=90)

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

    返回 None 只表示**这条航线什么时候空出来算不出来**，不表示它没被占。
    那一档照样计入在飞数，占到 `dispatched_at_utc + UNKNOWN_LINE_HOLD` 为止——
    理由写在那个常量上。

    原先的口径是「None 就当不占」，理由是「估高了空闲航线，最坏也只是 runner
    起来发现没位子、空跑一轮就退」。**那个「最坏」被实机推翻了**：空跑一轮不是
    一次性的代价，调度器的估算没有任何回写路径，同一个错估会每隔一个
    `RESTART_COOLDOWN` 原样再来一次，而每一轮都要几十秒导航并一直占着鼠标。
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


def _reads_the_whole_duration(text: str, match: re.Match[str]) -> bool:
    """这次匹配是不是把整个时长表达式都吃下去了。

    三条判据，都在找「有一段没被读进来」留下的残骸：

    1. **匹配之外还剩数字。** `3夭19旪36分7秒` 里只有 `36分7秒` 成了链，
       而 `3` 和 `19` 还杵在外面——它们本该是天和时。
    2. **紧贴左边的那个非空白字符是单位字。** `З天19时36分7秒`（首位数字被
       认成西里尔字母）里外面一个数字都不剩，但 `36` 前面顶着一个 `时`，
       说明时那一段的数字被吃掉了、单位还在。
       左边**只看紧邻的一个字**：标签和散文都在左边，而它们自己就含单位字
       （`飞行时间` 里有「时」），往左扫得太宽会把完全正常的输入判死。
    3. **右边到下一处空白之前还有单位字。** `3天19时36分Ⅶ秒` 里秒那一段的
       数字没了、`秒` 还留着。右边不会出现标签，所以这一侧可以看整个词；
       到空白为止是为了不去管 `3分20秒 抵达` 后面那截散文。

    **判据 1 判死的输入远多于必要**（散文里随便一个数字都会让它返回 False），
    这是有意的：本模块宁可读不出，也不要读出一个小而合理的错值——理由见
    `parse_game_duration()`。

    残留的洞：整个前缀被糊成**既无数字也无单位**的乱码时（`ЖЖЖ36分7秒`），
    三条都看不出异常，仍然会读成后半段。那一档由第二道防线
    （`MIN_CREDIBLE_ATTACK_FLIGHT`）在攻击链路上兜。
    """
    head, tail = text[: match.start()], text[match.end() :]

    if _DIGIT_RE.search(head) or _DIGIT_RE.search(tail):
        return False

    stripped_head = head.rstrip()
    if stripped_head and stripped_head[-1] in _DURATION_UNITS:
        return False

    for char in tail:
        if char.isspace():
            break
        if char in _DURATION_UNITS:
            return False
    return True


def parse_game_duration(text: str) -> timedelta | None:
    """解析游戏内倒计时，读不到返回 None。

    **部分匹配一律失败，绝不截断。** `_CN_DURATION_RE` 的每一段都是可选的，
    于是任何一段被 OCR 认错，后面那个碎片都能自己成一条合法的链。老实现拿
    `finditer` 逐个找、取第一个含数字的匹配，等于「前面糊了就丢掉前面」：

        3夭19旪36分7秒  ->  0:36:07        （真值 3 天 19:36:07）
        2旪15分7秒      ->  0:15:07        （真值 15:07 之外还有 2 小时）

    返回的是一个**看起来完全合理**的值，没有异常、没有日志，只是小了两三个
    数量级。生产库里 197 发有飞行时长的攻击中，66 发落在 0–60 秒、最大值正好
    59 秒，而 60–300 秒一发都没有——59 是「秒」字段能装下的最大数，也就是
    这条路径留下的指纹。

    **方向不许反过来。** 读不全就返回 None，而 None 在这个仓里是有归宿的：
    `expected_report_at_utc` 为 NULL 走「未知即立即收取」，`line_free_at_utc`
    为 NULL 是「不知道什么时候回来」、按 `UNKNOWN_LINE_HOLD` 照旧占着航线。
    多一条 NULL 只是多白跑一趟；一个错的小数字则会让战报被反复空收、让调度器
    以为航线十几秒后就空出来，接着派、撞上游戏的「同时派遣的舰队数量已达上限」。

    读成 0 也返回 None：那说明一个数字都没匹配到，不能当成「已抵达」——
    把 0 当成已抵达会让助手立刻去收一份还没产生的战报。「即将抵达」同理。
    """
    raw = text or ""
    clock = _CLOCK_RE.search(raw)
    if clock is not None:
        hours, minutes, seconds = (int(value) for value in clock.groups())
        total = timedelta(hours=hours, minutes=minutes, seconds=seconds)
        return total or None

    # 所有分段都是可选的，所以正则会在任意位置匹配到空串。跳过空匹配，
    # 否则 "即将抵达" 会被读成 0 秒。
    for match in _CN_DURATION_RE.finditer(raw):
        if not any(match.groups()):
            continue
        # 第一个含数字的匹配就是**唯一**的候选：读不全就到此为止，
        # 不再往后找下一个。往后找正是这个缺陷本身。
        if not _reads_the_whole_duration(raw, match):
            return None
        days, hours, minutes, seconds = (int(value or 0) for value in match.groups())
        total = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        return total or None
    return None


def vet_flight_time(
    flight: timedelta | None, *, mission_kind: str, same_system: bool | None = None
) -> timedelta | None:
    """给读到的飞行时长把两道下限关，不可信的降级成 None。

    第二道防线，和 `parse_game_duration()` 的读全校验互补、都要：

    | 防线 | 管什么 | 管不住什么 |
    |---|---|---|
    | 读全校验 | 所有发次；从根上不产生截断值 | 痕迹被一并抹掉的那一档 |
    | 本函数 | 只管攻击；解析得干干净净、值却偏小的 | 值仍大于下限的截断（`3天19时` 丢了 36 分） |

    **不要因为有了这一道就把读全校验放松。** 被截成 `3天19:00:00` 的那种值
    远在下限之上，两道都不会响，只能靠第一道从根上不产生它。

    ## 两道关

    1. **绝对下限** `MIN_CREDIBLE_ATTACK_FLIGHT`（3 分钟）——取值理由见那个常量。
    2. **跨恒星系下限** `SAME_SYSTEM_ATTACK_FLIGHT`（5 分钟）：目标不在出发星球
       那个恒星系里，飞行时间就必须**严格大于**同星系那个最低值。

    第 2 道是第 1 道漏掉的那一类的出口。实机（生产库 2026-08-13）三发：

        08-10 18:25  探路 → 2:320:11   300 秒
        08-11 01:07  探路 → 2:320:11   300 秒
        08-11 07:31  探路 → 2:320:11   300 秒

    出发星球 2:137:18，跨了 183 个恒星系，却只用 300 秒——**比同星系那一档还
    快**，物理上不可能（真值多半是 `X时5分0秒` 被截成了 `5分0秒`）。三次是同一个
    目标、同一个值，说明那份简报上的失手是**可重复的**，不是偶然噪声。而 300 秒
    在第 1 道那里稳稳过关（300 > 180），只有拿距离才拦得住。

    **不需要任何速度模型**，只需要「更远不可能更快」这一条。余量也足够大：库里
    距离 1 个恒星系的实测是 932–960 秒，是这道门槛的三倍以上。

    `same_system=None` 表示调用方不知道位置关系，此时**只过第 1 道**——宁可漏判，
    也不要在缺少事实时凭空拒绝一个可能正确的值。

    下限只对 `MISSION_KIND_ATTACK` 生效。**其余发次原样放行**，包括侦察和将来
    新增的发次类型：一条没被量过的下限没有理由套用攻击的经验值，而侦察那批历史值
    自己就疑似截断产物、量不出下限来。侦察靠读全校验那一道防，不靠这一道。

    返回 None 的归宿与解析失败完全相同，见 `parse_game_duration()`。
    """
    if flight is None:
        return None
    if mission_kind != MISSION_KIND_ATTACK:
        return flight
    if flight < MIN_CREDIBLE_ATTACK_FLIGHT:
        return None
    if same_system is False and flight <= SAME_SYSTEM_ATTACK_FLIGHT:
        return None
    return flight
