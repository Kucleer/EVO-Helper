"""「今天哪个出发星球效率最高」这一段的口径。**只放判据，不碰数据库。**

用户口径（2026-08-20）：

> 比如我想知道今天哪个星球的效率最高，用按天、按星球的资源收益去除他的航线数。

SQL 在 `storage.origin_efficiency`，页面在 `web.origin_efficiency`。判据留在这里
是因为下面每一条都有一个**看起来更自然、但会给出假结论**的写法，而那种错只有
用例钉得住（页面上每一格都会有一个像模像样的数）。

## ⚠️ 分子只算稀有三样，基础三样绝不许进来

槽位取 `domain.overview.RARE_SLOTS`（合金碎片 / 泰坦立方 / 收割者碎片），
名字取 `domain.battle_resources.slot_label`——**两者都不许在查询或模板里另抄
一份**。`SLOT_LABELS` 的顺序与游戏「太空舱」页并不一致（银河素与合金碎片对调），
抄第二份出去，对不上的症状是「数字全对、只是安在了别的资源名下」。

把金属 / 晶体 / 气体加进分子会让这个指标彻底失效：实测（2026-08-20）那三样由
**我方货舱容量**决定、与目标无关——同一预设的 6 条战报变异系数只有 0.0001
（719,900 / 720,000 / 720,100），而同期目标军力从 10,020 跨到 11,170 纹丝不动。
掺进来之后「预设大的星球」会无脑领先，而那是个假结论。

## ⚠️ 按**派出日**归属，不按读回日

一发的功劳记在它**派出**的那一天（`attack_dispatches.dispatched_at_utc`），
即使战报是隔天才读回来的。按读回日归属会把「昨天派出、今天读回」的收获算进
今天，于是「今天效率高」可能只是「今天补读了昨天的战报」。

**代价必须在页面上说出来**：按派出日归属时，当天的数**永远是不完整的**——
还有战报没回来。这就是为什么「回收率」必须和效率**并列显示、不能做成小字**。

## ⚠️ 日切按 UTC+0

同 `domain.overview` 那一段：切日在 Python 里做（`overview.day_start`），
不用 `func.date()`——那个函数在 PostgreSQL 上按**会话时区**换算，服务器在
UTC+8 时整条日界会挪 8 小时。

## 两个分母，两列都要

- **每线** = 稀有三样 ÷ 该星球那一天配着的航线数。
- **每线小时** = 每线 ÷ 该星球当天的**在岗时长**。

⚠️ **只给「每线」会罚晚开工的星球。** 实测 2026-08-20：一颗星球当天只跑了
11.7 小时，另两颗跑了 21 小时，而它的「每线」是另两颗的 2.2~3.6 倍——
那个差里有一部分只是「另两颗多跑了 9 个小时」。所以「每线小时」不是锦上添花，
它是这一段的主排序键（见 `rank_rows`）。

## ⚠️ 每颗星球的航线数没有历史，所以它分两档——判据借的是同一套

`mission_runs.configured_lines`（PR #235 加的那一列）记的是**账号一共配着几条**，
不是每颗星球各配着几条。所以「那一天这颗星球配着几条」这个数**库里根本没有**，
只能分两档给（见 `origin_lines`）：

- 那一天记下来的账号总数和**此刻**配置的总数一致时，按此刻这颗星球的
  `mission_task_origins.fleet_lines` 算，标成真值（`LineSource.RECORDED`）。
- 否则（那一天没记下总数，或者总数变过）退到**这颗星球当天的最大并发在飞数**
  这个下界（`LineSource.LOWER_BOUND`）。

⚠️ **两档都直接用 `domain.overview` 的现成件**（`LineCount` / `LineSource` /
`max_concurrent_lines`），一份都不许在这里重写：「不知道线数时怎么算」这件事
用户已经定过（退到最大并发在飞数当下界），这一段再造一套，同一页上就会有两种
答案。

⚠️ **下界的方向要记牢，它和利用率那边正好相反着读：** 线数取下界 ⇒ 分母偏小
⇒ **效率取上界**，所以页面上带「≤」。

⚠️ **一个查不出来的盲点**：账号总数没变、而星球之间的分配变了（2/4/3 → 3/3/3），
上面那个一致性检查看不出来，那一天会被当成真值。**只标注、不修正**——真要修得
给 `mission_task_origins` 记历史，而这个需求是纯读的、不该带迁移。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from evo_helper.domain.models import Coordinate
from evo_helper.domain.overview import (
    MAX_DAY_ROWS,
    RESOURCE_STATS_START_UTC,
    LineCount,
    LineSource,
    Occupancy,
    day_start,
    max_concurrent_lines,
    recovery_rate,
)

#: 回收率低于这个水平时，这一行的效率数标成**不可信**。
#:
#: ## 这个数是怎么来的（改它之前先读完）
#:
#: 效率的分子只数**已读回**战报里的收获，所以回收率就是分子的**覆盖率**：
#: 回收 60% 时算出来的效率至少低估 1 ÷ 0.6 = 1.67 倍。而实测 2026-08-20 三颗
#: 星球「每线」的真实差距是 29,212 / 8,007 = 3.6 倍，相邻两颗之间只差
#: 13,053 / 8,007 = **1.63 倍**——也就是说 1.67 倍的低估足以让相邻两行**换位**。
#: 阈值就压在这个「能翻排序」的边上：再宽一点，页面会拿一个已经排错了的名次
#: 当结论；再严一点，正常的日子会被无谓地打上不可信（2026-08-20 当天最差的
#: 一颗是 69%）。
#:
#: 已知的两个参照：08-17 那天某颗星球 39 发只读回 13 发（33%）、08-16 是 2 发
#: 读回 0 发（0%）。**那两天不是没赚，是战报没读回来**——不标出来，用户会得出
#: 「08-17 效率崩了」这个错结论。
#:
#: ⚠️ **这既不是纯标定常量、也还没做成旋钮，理由说清楚**：它的取值取决于用户
#: 愿意容忍多大的低估（偏旋钮），但把它做成旋钮要在 `military_attack_config`
#: 上加一列，而这个需求是纯读的、不该带迁移。所以先留成常量，把推导写在这里，
#: 让下一次改动是**有意的**而不是随手调的。真要可配置，加列那一步需要用户授权。
LOW_RECOVERY_THRESHOLD = 0.6

#: 「在岗时长」小于这么久时，「每线小时」显示成「—」而不是一个大数。
#:
#: ⚠️ **这不是偏好项，是防爆除法。** 在岗时长趋近 0 时（首发就在统计时刻前后）
#: 每线小时会飙到几十万，而那个数只反映「刚开工」，不反映效率。取 6 分钟：
#: 比任何一次真实往返（实测最短 42 分钟）都短得多，所以它挡掉的只有「刚派出
#: 第一发」这一种情形。
MIN_ON_DUTY = timedelta(minutes=6)


@dataclass(frozen=True, slots=True)
class OriginDay:
    """一颗出发星球在**某一个 UTC 日**里的原始事实，全部按派出日归属。

    这里一个比率都没有——比率一律由下面几个函数算，好让「分子之和 ÷ 分母之和」
    这条规矩只有一份实现。
    """

    origin: Coordinate
    #: 当天从这颗星球派出去、被游戏接受的发数。
    dispatches: int
    #: 上面那些发次里，**已经读回战报**的有几发。⚠️ 不是「当天读回的战报数」——
    #: 后者会把昨天派出的战报算进来，让回收率变成一个自己跟自己比的数。
    reports: int
    #: 稀有三样的合计（`domain.overview.RARE_SLOTS`），来自上面那些已读回的战报。
    rare_amount: int
    #: 这个合计里有没有近似读数（画面上写成 `928K` 那种）。页面要标「约」。
    rare_approximate: bool
    #: 最大绝对误差，逐份战报相加（同 `storage.overview.ResourceTotal.uncertainty`）。
    rare_uncertainty: int
    #: 当天这颗星球的首发时刻；一发没派时为 None。
    first_dispatch_at_utc: datetime | None
    #: 当天这颗星球的末发时刻；一发没派时为 None。
    last_dispatch_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class OriginEfficiency:
    """一行「按星球效率」。派生量在构造时就算好，模板里一个算术都不做。"""

    day: OriginDay
    #: 那一天这颗星球按几条航线算，以及那个数是真值还是下界（见 `origin_lines`）。
    line_count: LineCount
    #: 当前配置里还有没有这颗星球。没有的（被删掉了）照样列出来——它当天真打出去过。
    in_config: bool
    #: 当前配置里这颗星球是不是启用状态。停用的照样出现在表里（它当天真打出去过）。
    enabled: bool
    #: 在岗时长（秒），见 `on_duty_seconds`。
    on_duty_s: float
    #: 稀有三样 ÷ 航线数。分母不可用时为 None。
    per_line: float | None
    #: 稀有三样 ÷ 航线数 ÷ 在岗小时。两个分母任一不可用时为 None。
    per_line_hour: float | None
    #: 读回战报数 ÷ 派出数。派出数为 0 时为 None（**不是 0%**）。
    recovery: float | None
    #: 回收率低到足以让排序翻转（见 `LOW_RECOVERY_THRESHOLD`）。
    untrustworthy: bool

    @property
    def on_duty_hours(self) -> float:
        return self.on_duty_s / 3600.0

    @property
    def lines(self) -> int:
        return self.line_count.lines

    @property
    def lines_exact(self) -> bool:
        """线数是真值还是下界。假 ⇒ 分母偏小 ⇒ 两个效率数都是**上界**，页面带「≤」。"""
        return self.line_count.exact

    @property
    def origin(self) -> Coordinate:
        return self.day.origin

    @property
    def origin_key(self) -> tuple[int, int, int]:
        """排序用的稳定次序。同值时按坐标排，页面次序才不随库里的行序抖动。"""
        origin = self.day.origin
        return (origin.galaxy, origin.system, origin.position)


def on_duty_seconds(
    *,
    first_dispatch_at_utc: datetime | None,
    day_start_utc: datetime,
    day_end_utc: datetime,
    now_utc: datetime,
) -> float:
    """这颗星球当天「在岗」了多少秒：**首发 → min(现在, 当日结束)**。

    一发没派就是 0 秒。

    ## 为什么是「首发 → 现在」而不是「首发 → 末发」

    「首发 → 末发」是个更直觉的写法，而且它是这个需求的基线数据用的算法——
    但它**奖励早收工的星球**：一颗星球 00:00 和 00:30 各派一发然后再没动过，
    它的「在岗时长」是 0.5 小时，「每线小时」会高得离谱。那和它想纠正的
    「罚晚开工」是同一个错，只是方向相反。

    「首发 → 现在」把开工之后的**空转也算进分母**，而空转本来就该算进去：
    一条闲着的航线是被浪费掉的产能，正是这一页存在的理由。

    ## ⚠️ 它在两种情形下会失真，页面上要说出来

    1. **当天被停用过的星球被罚。** 停用那几个小时照样进分母，而那几个小时
       它不可能派活。库里**没有**「启用/停用」的历史（`mission_task_origins.enabled`
       只有当前值），所以这段时间取不回来。实测 2026-08-20 就有一颗星球中途被
       自动停用过。
    2. **首发之前的空转看不见。** 一颗星球配好了却从 00:00 干等到 20:00 才派
       第一发，它只按 4 小时计费——这个数会偏高。真要修，得知道「配置启用的
       时长」，而那同样不在库里。

    这两条**只标注、不修正**：没有证据支撑的修正比不修正更糟。

    ## 为什么不用「调度器运行时长」

    那个数（`domain.overview.available_seconds` 的那一半）对**所有星球都一样**，
    拿它当分母等于把「每线小时」变成「每线」乘一个常数——排序一模一样，
    那一列就白加了，而它存在的全部理由就是要和「每线」排出不同的名次。
    """
    if first_dispatch_at_utc is None:
        return 0.0
    began = max(_utc(first_dispatch_at_utc), _utc(day_start_utc))
    finished = min(_utc(now_utc), _utc(day_end_utc))
    if finished <= began:
        return 0.0
    return (finished - began).total_seconds()


def origin_lines(
    *,
    recorded_total: int | None,
    configured_total: int,
    configured: int | None,
    enabled: bool,
    occupancies: tuple[Occupancy, ...],
    window_start: datetime,
    window_end: datetime,
) -> LineCount:
    """那一天这颗星球按几条航线算，以及那个数有多硬。

    - `recorded_total`：那一天 `mission_runs.configured_lines` 记下来的**账号总数**
      （`storage.overview.OverviewRepository.recorded_lines`）。None = 那一天没记。
    - `configured_total`：**此刻**账号一共配着几条（启用的那些 `fleet_lines` 之和）。
    - `configured`：**此刻**这颗星球配着几条。None = 当前配置里没有它了。
    - `enabled`：**此刻**这颗星球是启用的吗。
    - `occupancies`：**这颗星球**当天的航线占用段。

    ## 判据

    ⚠️ **`mission_runs.configured_lines` 是账号总数，不是每颗星球各自的数**
    （见那一列的注释）。所以真值那一档只能这么给：**那一天的账号总数和此刻配置的
    总数一致**时，才敢把此刻的**每颗星球分配**当成那一天的分配用。

    总数对不上（配置改过），或者那一天压根没记总数（那一列 2026-08-20 才加），
    就退到「这颗星球当天的最大并发在飞数」这个**下界**——判据直接用
    `domain.overview.max_concurrent_lines`，和利用率那一列退的是同一档。

    ⚠️ **绝不许拿此刻的配置去顶一个总数对不上的历史天。** 用户 2026-08-20 把航线
    从 4 条加到 9 条；按 9 条去算 08-15（当时 4 条），效率会被砍到 44%，而页面上
    看不出任何异样（同 `domain.overview.period_lines` 那一段的理由，一字不差）。

    ⚠️ **`configured` 为 0 或 None 时同样退下界。** 「配了 0 条」和「配置里没有它」
    在这里是同一件事：都拿不到分母，而它当天真把活打出去了——那些活得有个数去除。

    ⚠️ **此刻停用的星球一律退下界，哪怕总数对得上。** 停用的那些不进
    `mission_runs.configured_lines`（那一列记的是启用的之和），所以「总数一致」
    这个检查压根没检查到它——它当天配着几条，这条链路上没有任何证据。
    而它当天确实派出过（否则不会有这一行），也就是说它当时是启用的、当时的总数
    里含着它，于是「总数一致」在它身上恰恰说明那个快照是它被停用**之后**才记的。
    """
    if (
        configured is not None
        and configured > 0
        and enabled
        and recorded_total is not None
        and recorded_total == configured_total
    ):
        return LineCount(lines=configured, source=LineSource.RECORDED)
    return LineCount(
        lines=max_concurrent_lines(occupancies, window_start, window_end),
        source=LineSource.LOWER_BOUND,
    )


def per_line(rare_amount: int, lines: int) -> float | None:
    """稀有三样 ÷ 航线数。**航线数 ≤ 0 时返回 None，不是 0。**

    0 会在页面上和「这颗星球一点收获都没有」长得一模一样，而这里的实情是
    「当前配置里没有这颗星球，分母取不到」。
    """
    if lines <= 0:
        return None
    return rare_amount / lines


def per_line_hour(rare_amount: int, lines: int, on_duty_s: float) -> float | None:
    """每线小时 = 稀有三样 ÷ 航线数 ÷ 在岗小时。

    **两个分母都必须有**：漏掉在岗时长这一层，这一列就退化成「每线」的复制品
    （而那正是需求里点名要防的那个缺陷）。在岗时长太短时返回 None，见
    `MIN_ON_DUTY`。
    """
    lines_only = per_line(rare_amount, lines)
    if lines_only is None:
        return None
    if on_duty_s < MIN_ON_DUTY.total_seconds():
        return None
    return lines_only / (on_duty_s / 3600.0)


def is_untrustworthy(recovery: float | None) -> bool:
    """这一行的效率数该不该标成不可信。

    回收率取不到（一发没派）时**不标**：那一行没有效率数可言，标一个「不可信」
    只是噪音。
    """
    if recovery is None:
        return False
    return recovery < LOW_RECOVERY_THRESHOLD


def rank_rows(rows: Iterable[OriginEfficiency]) -> list[OriginEfficiency]:
    """按「每线小时」**从高到低**排。

    ⚠️ **主排序键是「每线小时」，不是「每线」。** 用「每线」排会把晚开工的星球
    排在前面，而那正是这一段要纠正的偏差。

    「每线小时」取不到的行（当前配置里没有这颗星球、或者在岗时长太短）一律排在
    最后：它们没有名次可言，混在中间会让读者以为它们排在某个位置上。
    同值时按坐标排，好让页面上的次序不随库里的行序抖动。
    """
    return sorted(
        rows,
        key=lambda row: (
            row.per_line_hour is None,
            -(row.per_line_hour or 0.0),
            row.origin_key,
        ),
    )


def selectable_days(now_utc: datetime, *, limit: int = MAX_DAY_ROWS) -> list[datetime]:
    """这一段能选哪几天（**倒序**，今天在最前）。

    ⚠️ **不早于 `RESOURCE_STATS_START_UTC`。** 那 12 格的识别是 2026-08-18 才修好
    的，更早的战报**根本没有资源明细**——把 08-17 摆出来，页面上会是一排
    「收获 0 / 效率 0」，而那天真打出去了几十发。零和「没有数据」在这一段里
    必须分开，而最省事又最诚实的分法就是：不提供那些天。
    """
    today = day_start(now_utc)
    floor = day_start(RESOURCE_STATS_START_UTC)
    days: list[datetime] = []
    cursor = today
    while len(days) < limit and cursor >= floor:
        days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def parse_day(value: str | None, *, now_utc: datetime) -> datetime:
    """把 `?origin_day=YYYY-MM-DD` 读成一个 UTC 日的 00:00；认不出来回落到今天。

    认不出来**不报 422**（同 `domain.overview.parse_granularity`）：日期是几个
    链接，手改地址写错一位换来一页 JSON 报错，读起来就是「控制台坏了」。
    超出 `selectable_days` 范围的同样回落——那些天没有资源明细，见那个函数。
    """
    allowed = selectable_days(now_utc)
    if value is None:
        return allowed[0]
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return allowed[0]
    return parsed if parsed in allowed else allowed[0]


def day_label(day_start_utc: datetime, *, now_utc: datetime) -> str:
    """这一天在页面上叫什么。今天写「今天」——它是唯一一个还在长的格子。"""
    head = f"{day_start_utc:%m-%d}"
    return f"{head} 今天" if day_start_utc == day_start(now_utc) else head


def build_rows(
    days: Sequence[OriginDay],
    *,
    configured: dict[Coordinate, tuple[int, bool]],
    occupancies: dict[Coordinate, tuple[Occupancy, ...]],
    recorded_total: int | None,
    day_start_utc: datetime,
    now_utc: datetime,
) -> list[OriginEfficiency]:
    """把原始事实拼成排好序的行。

    - `configured`：「坐标 →（航线数, 是否启用）」，由调用方从
      `MissionScheduler.configured_line_origins()` 取——**这里不自己去读那张表**，
      `planet_id` 与坐标快照谁优先这条规则只该有一份（同 `storage.overview`）。
    - `occupancies`：每颗星球当天的航线占用段，线数没有真值时按它推下界。
    - `recorded_total`：那一天记下来的**账号**航线总数，见 `origin_lines`。

    ⚠️ **行集是「当天真派出过的星球」∪「当前配着的星球」。** 只取当前配置会漏掉
    当天被停用、甚至被删掉的那些，而它们当天真把活打出去了（实测 2026-08-20
    有一颗中途被自动停用）；只取派出过的会漏掉「配了却一发没派」，而那是这一页
    最该喊出来的一种浪费。

    ⚠️ **`configured_total` 只数启用的那些**，因为
    `mission_runs.configured_lines` 记的就是「启用的那些 `fleet_lines` 之和」
    （见那一列的注释）。把停用的也加进来，两个数永远对不上，于是每一天都退成
    下界——真值那一档就白做了。
    """
    day_end_utc = day_start_utc + timedelta(days=1)
    facts = {item.origin: item for item in days}
    origins = list(facts) + [origin for origin in configured if origin not in facts]
    configured_total = sum(lines for lines, enabled in configured.values() if enabled)
    rows: list[OriginEfficiency] = []
    for origin in origins:
        fact = facts.get(origin) or _empty_day(origin)
        setting = configured.get(origin)
        line_count = origin_lines(
            recorded_total=recorded_total,
            configured_total=configured_total,
            configured=None if setting is None else setting[0],
            enabled=bool(setting is not None and setting[1]),
            occupancies=occupancies.get(origin, ()),
            window_start=day_start_utc,
            window_end=min(now_utc, day_end_utc),
        )
        on_duty = on_duty_seconds(
            first_dispatch_at_utc=fact.first_dispatch_at_utc,
            day_start_utc=day_start_utc,
            day_end_utc=day_end_utc,
            now_utc=now_utc,
        )
        recovery = _recovery(fact)
        rows.append(
            OriginEfficiency(
                day=fact,
                line_count=line_count,
                in_config=setting is not None,
                enabled=bool(setting is not None and setting[1]),
                on_duty_s=on_duty,
                per_line=per_line(fact.rare_amount, line_count.lines),
                per_line_hour=per_line_hour(fact.rare_amount, line_count.lines, on_duty),
                recovery=recovery,
                untrustworthy=is_untrustworthy(recovery),
            )
        )
    return rank_rows(rows)


def _recovery(fact: OriginDay) -> float | None:
    """回收率。判据 import `domain.overview.recovery_rate`，**不在这里另写一遍**。

    那个函数钉着一条这一段同样需要的规矩：比率永远是「分子之和 ÷ 分母之和」，
    而且分母为 0 时给 None（一发没派时「回收率 0%」是句假话）。
    """
    return recovery_rate(fact.reports, fact.dispatches)


def _empty_day(origin: Coordinate) -> OriginDay:
    return OriginDay(
        origin=origin,
        dispatches=0,
        reports=0,
        rare_amount=0,
        rare_approximate=False,
        rare_uncertainty=0,
        first_dispatch_at_utc=None,
        last_dispatch_at_utc=None,
    )


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("时刻必须带时区；naive 的时刻算不出在岗时长")
    return moment.astimezone(UTC)


__all__ = [
    "LOW_RECOVERY_THRESHOLD",
    "MIN_ON_DUTY",
    "OriginDay",
    "OriginEfficiency",
    "build_rows",
    "day_label",
    "is_untrustworthy",
    "on_duty_seconds",
    "origin_lines",
    "parse_day",
    "per_line",
    "per_line_hour",
    "rank_rows",
    "selectable_days",
]
