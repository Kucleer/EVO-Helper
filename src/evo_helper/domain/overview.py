"""数据概览页的口径：统计起点、周期切分、比率与航线占用时长。

这个模块**只放判据，不碰数据库**——SQL 在 `storage.overview`，页面在
`web.overview_routes`。放在 domain 里的理由是这几条全都被「页面自己算一遍就会
算错」钉过（见 `docs/数据概览页-需求.md` 第八节），而能用例钉住的前提是它们
不掺在查询里。

## ⚠️ 两个统计起点，**不许合并成一个常量**

- 计数类（派遣 / 战报 / 撞保护期 / 覆盖坐标）从 **2026-08-17** 起算：
  用户口径「数据统计从 8/17 开始计算，算是正式运行」（`docs/数据概览-方案.md`）。
- 资源明细从 **2026-08-18** 起算：那 12 格的识别是 08-18 才修好的（PR #191/#193），
  更早的战报**根本没有资源明细**。

实测（2026-08-19）「今天 / 最近 7 天 / 最近 30 天」三个窗口的稀有三样完全相同，
正是因为资源只有那么两天。把两个日期并成一个，「合计」要么把资源起点提前
（凭空多出一段没有明细的日子，看起来像收成骤降），要么把计数起点推后
（08-17 那天真打出去的 42 发凭空消失）。

## ⚠️ 统计一律按 UTC+0 切天，页面上的时刻仍按 UTC+8 显示

用户口径（2026-08-19）：「使用 UTC+0 作为统计口径」。这条会让页面上的数和用户
按 `AT TIME ZONE 'Asia/Shanghai'` 手查的对不上（实测 08-19 的合金碎片：UTC+8 切法
117,600、UTC+0 切法 27,500，**合计一样、只有日切位置不同**），**这不是 bug**。

⚠️ 切日刻意在 Python 里做，不用 `func.date()`：那个函数在 PostgreSQL 上按**会话
时区**换算，服务器在 UTC+8 时整条日界会挪 8 小时（同 `storage.repository._utc_day`，
来历是 PR #159 那个海盗配额的日界缺陷）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

#: 计数类统计的起点（UTC）。**只管派遣、战报、撞保护期这类计数。**
#:
#: 这个日期之前的数据不进「合计」那一档——不是划个线好看，08-17 之前那段确实
#: 不该算数：`flight_seconds` 里躺着一批 08-13 之前的垃圾读数、战报从 08-15 21:40
#: 起两天一份都没读回来（那几天的回收率是故障值不是业务值）、期间还有多次人工
#: 撤回舰队与手动清理航线。整段理由在 `docs/数据概览-方案.md` 第〇节。
COUNT_STATS_START_UTC = datetime(2026, 8, 17, tzinfo=UTC)

#: 资源明细统计的起点（UTC）。**和上面那个不是同一天，也不许合并。**
RESOURCE_STATS_START_UTC = datetime(2026, 8, 18, tzinfo=UTC)

#: 首屏那三样。用户口径：「最关注合金碎片/泰坦立方/收割者碎片，其他可以忽略
#: 不计」。**存的是槽位不是名字**——名字由 `battle_resources.SLOT_LABELS` 翻译，
#: 改名不用动这里，同样也不许把名字抄进来（理由见那个模块）。
#:
#: 5 = 合金碎片、8 = 泰坦立方、9 = 收割者碎片。
RARE_SLOTS: tuple[int, ...] = (5, 8, 9)

#: 「今天收益」那一行第四张卡上的三样常规资源。用户口径（2026-08-19）：
#: 「这里改成只显示 金属/晶体/气体，整合进一个标签即可」。
#:
#: 0 = 金属、1 = 晶体、2 = 气体。**同 `RARE_SLOTS`，存的是槽位不是名字**，
#: 而且这三个数只在这里出现一次——模板与查询里不许另抄一份：
#: `battle_resources.SLOT_LABELS` 的顺序与游戏「太空舱」页**并不一致**
#: （银河素与合金碎片对调），抄一份出去，日后对不上的症状是「数字全对、
#: 只是安在了别的资源名下」，页面上一点异样都没有。
#:
#: ⚠️ 这一张替掉了原先那张「其余九种」——把九种加总成一个数是把千万级的金属和
#: 个位数的银河石能量加在一起，量纲都不一样，那个数没有意义。
BASIC_SLOTS: tuple[int, ...] = (0, 1, 2)

#: 「按天」最多给几行。用户口径（2026-08-19）：「按天最多 7 行」。
MAX_DAY_ROWS = 7

#: 「按周」「按月」各给几行。天数那一档由用户定死 7；这两档沿用旧方案里的
#: 默认量（8 周 / 6 个月），末尾全空的行会被 `trim_empty_tail` 砍掉，所以
#: 数据只有几天时页面上不会挂一串零。
MAX_WEEK_ROWS = 8
MAX_MONTH_ROWS = 6


class Granularity(StrEnum):
    """周期统计的四档。取值直接进 URL 查询参数，所以是 `StrEnum` 不是 `Enum`。"""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    TOTAL = "total"


def parse_granularity(value: str | None) -> Granularity:
    """把查询参数读成一档；认不出来就回落到「按天」。

    认不出来**不报 422**：这一页的档位切换是四个链接，手改地址写错一个字母
    换来一页 JSON 报错，读起来就是「控制台坏了」（同 `web.app._blank_to_none`
    那一段的理由）。
    """
    if value is None:
        return Granularity.DAY
    try:
        return Granularity(value.strip().lower())
    except ValueError:
        return Granularity.DAY


def row_limit(granularity: Granularity) -> int:
    """这一档最多给几行。"""
    if granularity is Granularity.DAY:
        return MAX_DAY_ROWS
    if granularity is Granularity.WEEK:
        return MAX_WEEK_ROWS
    if granularity is Granularity.MONTH:
        return MAX_MONTH_ROWS
    return 1


def day_start(moment: datetime) -> datetime:
    """这个时刻所属 **UTC 日**的 00:00。

    ⚠️ **先 `astimezone(UTC)` 再截断。** 传进来的可能是任何时区的 aware 时刻；
    直接 `replace(hour=0)` 会按它自己的时区切，而那正是「页面按 UTC+8 切天」
    这个缺陷的形状——同一批战报会整体挪一天。
    """
    at = _require_utc(moment).astimezone(UTC)
    return at.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(moment: datetime) -> datetime:
    """这个时刻所属 **ISO 周**（周一起）的 UTC 00:00。

    ⚠️ **周一起，不是周日起。** 周一是 bot 刷新日、全服都在打，保护期跳过会
    大量增加（见 `docs/数据概览-方案.md` 第三节）。周界压在周一，那一天的异常
    才不会被劈到两周里去。
    """
    start = day_start(moment)
    return start - timedelta(days=start.weekday())


def month_start(moment: datetime) -> datetime:
    """这个时刻所属 UTC 自然月的 1 号 00:00。"""
    return day_start(moment).replace(day=1)


def period_start(moment: datetime, granularity: Granularity) -> datetime:
    """这个时刻落在哪个周期里，返回那个周期的起点。

    `TOTAL` 一律返回计数类起点——「合计」的含义是「自 2026-08-17 起的合计」，
    不是「库里的全部」。资源那一列另用 `RESOURCE_STATS_START_UTC` 收窄，
    见 `resource_window`。
    """
    if granularity is Granularity.DAY:
        return day_start(moment)
    if granularity is Granularity.WEEK:
        return week_start(moment)
    if granularity is Granularity.MONTH:
        return month_start(moment)
    return COUNT_STATS_START_UTC


def period_end(start: datetime, granularity: Granularity, *, now: datetime) -> datetime:
    """周期的**右开**边界。`TOTAL` 一档的右边界是「现在」。"""
    if granularity is Granularity.DAY:
        return start + timedelta(days=1)
    if granularity is Granularity.WEEK:
        return start + timedelta(days=7)
    if granularity is Granularity.MONTH:
        return _next_month(start)
    return _require_utc(now).astimezone(UTC)


def period_starts(now: datetime, granularity: Granularity) -> list[datetime]:
    """从当前周期往回数，给出这一档要显示的那几个周期起点（**倒序**）。

    不按统计起点截断：原型上「按天」是能看到 08-13 的（那天真派了 140 发），
    而统计起点只管「合计」那一行。把每一档都截到 08-17，页面就再也看不见
    08-15~08-16 那段战报一份没读回来的故障——而让那种故障第一天就显眼，
    正是这一页存在的理由（`docs/数据概览-方案.md` 第五节）。
    """
    if granularity is Granularity.TOTAL:
        return [COUNT_STATS_START_UTC]
    starts: list[datetime] = []
    cursor = period_start(now, granularity)
    for _ in range(row_limit(granularity)):
        starts.append(cursor)
        cursor = _previous(cursor, granularity)
    return starts


def resource_window(start: datetime, end: datetime) -> tuple[datetime, datetime] | None:
    """把一个周期收窄到「资源明细真的存在」的那一段；整段都在起点之前就返回
    None（也就是「这一格没有资源数据」，而不是「收成是 0」）。

    ⚠️ 计数类**不走这个函数**。08-13 那天真的派了 140 发，把它也按资源起点截掉，
    页面上那一行会变成一排零——而那天的问题恰恰是「派了很多、一份战报都没读
    回来」。两类指标各用各的起点，这就是它们不能合并成一个常量的可观察后果。
    """
    clipped = max(start, RESOURCE_STATS_START_UTC)
    if clipped >= end:
        return None
    return clipped, end


def recovery_rate(reports: int, dispatches: int) -> float | None:
    """战报回收率。派遣数为 0 时返回 None（**不是 0%**）。

    ⚠️ **比率永远是「分子之和 ÷ 分母之和」，不是把每天的百分比平均一遍。**
    天数不齐、量级不齐时那两个算法给的数不一样，而后者是错的：08-19 只派了 8 发
    却 100% 回收，08-16 派了 39 发一份没回收——平均下来 50%，而真实的周回收率是
    8 ÷ 47 = 17%。这条在 `docs/数据概览-方案.md` 第三节写着「要有用例钉住」。

    分母为 0 时返回 None 而不是 0：那一天一发没派，「回收率 0%」是句假话
    （页面上显示成「—」）。
    """
    if dispatches <= 0:
        return None
    return reports / dispatches


def utilisation(occupied_seconds: float, available_seconds: float) -> float | None:
    """航线利用率 = 航线被占用的时长 ÷ 可用航线时长。分母为 0 时返回 None。

    ⚠️ **超过 100% 不许截断。** 舰队在天上飞的时候 runner 不一定在跑——实测
    2026-08-15 只开了 3 小时、派了 42 发，那些舰队要飞到几小时后才回来，算出来
    243%。那是个真信号（「派出去的活比开机时间还多」＝关机太早、舰队回来时没人
    接），截成 100% 就把它抹掉了。整段在 `docs/数据概览-方案.md` 第二节 C。
    """
    if available_seconds <= 0:
        return None
    return occupied_seconds / available_seconds


def occupancy_end(
    *,
    dispatched_at_utc: datetime,
    line_free_at_utc: datetime | None,
    line_released_at_utc: datetime | None,
    hold: timedelta,
) -> datetime:
    """这一发**占到什么时候**为止。三档的次序与判据同
    `storage.repository._still_holding_a_line` 逐条对应。

    - **人工放过手**（`line_released_at_utc` 非 NULL）：到那一刻为止，别的一概
      不看。用户在游戏里数过航线、确认舰队已回港，那是比另两个推算出来的钟更硬
      的证据。⚠️ 它必须**罩住**后两档而不是排在最后：只在有航线钟那一档上判的
      话，读不出飞行时间的那些（正是实机上最容易卡住的一批）按下按钮纹丝不动。
    - 航线钟读到了：到 `line_free_at_utc` 为止。
    - 航线钟为 NULL：到 `dispatched_at_utc + hold` 为止。NULL 的意思是「不知道
      它什么时候回来」，不是「它没占位」。

    `hold` **必填、没有默认值**：漏传就会静默退回到写死的 90 分钟，而那正是
    用户在攻击配置页上刚改掉的那个数（同 `_still_holding_a_line` 的理由）。

    返回值**不会早于派出时刻**：人工放手那一列理论上可能记在派出之前（换库、
    补录），钳一下免得算出负的占用时长去冲抵别的派遣。
    """
    if line_released_at_utc is not None:
        return max(dispatched_at_utc, line_released_at_utc)
    if line_free_at_utc is not None:
        return max(dispatched_at_utc, line_free_at_utc)
    return dispatched_at_utc + hold


def overlap_seconds(
    start: datetime, end: datetime, window_start: datetime, window_end: datetime
) -> float:
    """`[start, end)` 与 `[window_start, window_end)` 相交多少秒；不相交给 0。

    航线占用要按天摊：一发 22:30 派出、次日 00:40 回港的舰队，两天各占一段。
    整段算给派出那一天的话，跨零点的那些会让当天利用率虚高、次日虚低。
    """
    left = max(start, window_start)
    right = min(end, window_end)
    if right <= left:
        return 0.0
    return (right - left).total_seconds()


@dataclass(frozen=True, slots=True)
class Occupancy:
    """一发派遣占着航线的那一段。`storage.overview` 把库里的行翻成它。"""

    start: datetime
    end: datetime


def occupied_seconds(
    occupancies: tuple[Occupancy, ...], window_start: datetime, window_end: datetime
) -> float:
    """这些占用段落在窗口里的总秒数。**逐段相加、不去重**——同一时刻可以有
    好几条航线各占各的，合并区间等于把并行的航线算成一条。
    """
    return sum(
        overlap_seconds(item.start, item.end, window_start, window_end) for item in occupancies
    )


@dataclass(frozen=True, slots=True)
class RunWindow:
    """调度器真的在跑的一段，以及那段时间这台账号配着几条航线。"""

    start: datetime
    end: datetime
    lines: int


def available_seconds(
    runs: tuple[RunWindow, ...], window_start: datetime, window_end: datetime
) -> float:
    """可用航线时长 = Σ（任务实际运行时长 × 该时段的航线数）。

    ⚠️ **分母不能用「24 小时 × 当前航线数」。** 调度器停着的那段不该算进产能——
    关一晚上机器、第二天利用率腰斩，而那不是「资源闲着」，是「本来就没开工」。
    用户口径（2026-08-17）：「C 的分母是任务实际运行时间 × 航线数」。
    """
    return sum(
        overlap_seconds(run.start, run.end, window_start, window_end) * run.lines for run in runs
    )


def trim_empty_tail[T](rows: list[T], is_empty: Callable[[T], bool]) -> list[T]:
    """砍掉末尾**连着的**空行，至少留一行。

    只砍末尾、不砍中间：中间那些空行是有信息量的（08-14 一发没派），砍掉之后
    趋势看起来是连续的，而它其实断过。至少留一行是为了让「一条数据都没有」
    仍旧显示成一个周期，而不是一张没有行的表。
    """
    trimmed = list(rows)
    while len(trimmed) > 1 and is_empty(trimmed[-1]):
        trimmed.pop()
    return trimmed


#: 航线格子的三种样子。`fly` 在飞、`unk` 时长未知（按 `hold` 兜底占着）、`free` 空。
SLOT_FLYING = "fly"
SLOT_UNKNOWN = "unk"
SLOT_FREE = "free"


def line_slots(*, configured_lines: int, holding: int, unknown_duration: int) -> tuple[str, ...]:
    """一颗星球的航线格子。**格子数恒等于 `configured_lines`。**

    ⚠️ **按配置的航线数画，不按占用数画**（需求文档 8.3）。原型第一版按
    「在飞 + 时长未知」画格子，于是一颗配了 4 条的星球画出了 7 格——那张图
    表达的是「这颗星球有 7 条航线」，而它只有 4 条。配几条就画几格，
    占多少点亮多少；占用超了另说（见 `overflow_lines`），不许靠加格子表达。

    次序是「在飞 → 时长未知 → 空」：时长未知那一档挨着空格子，一眼看得出
    「满了，但其中几条是因为读不出飞行时间才占着的」。
    """
    flying = max(holding - unknown_duration, 0)
    cells = [SLOT_FLYING] * flying + [SLOT_UNKNOWN] * max(unknown_duration, 0)
    if len(cells) < configured_lines:
        cells.extend([SLOT_FREE] * (configured_lines - len(cells)))
    return tuple(cells[:configured_lines])


def overflow_lines(*, configured_lines: int, holding: int) -> int:
    """占用比配置多出来几条。

    ⚠️ **这不是 bug，要说出来。** 在飞数可能超过该星球配置的航线数（实测
    `4:277:15` 在飞 5 条而配置是 4 条），因为它**跨任务种类**——海盗与 bot 抢
    同一批航线。页面上不说的话，这件事会被当成算错（需求文档 2.1）。
    """
    return max(holding - configured_lines, 0)


def period_label(start: datetime, granularity: Granularity, *, now: datetime) -> str:
    """这一行周期在页面上叫什么。

    「今天 / 本周 / 本月」用相对说法，其余用日期——一张倒序的表里，最上面那行
    是不是「现在这一档」是读者第一个要判断的事，而它同时也是唯一一个还在长的
    格子（资源列会一直涨，见 `storage.overview.resource_totals`）。
    """
    if granularity is Granularity.TOTAL:
        return f"合计 自 {COUNT_STATS_START_UTC:%m-%d}"
    if granularity is Granularity.DAY:
        head = f"{start:%m-%d}"
        return f"{head} 今天" if start == day_start(now) else head
    if granularity is Granularity.WEEK:
        finish = start + timedelta(days=6)
        span = f"{start:%m-%d}~{finish:%m-%d}"
        return f"本周 {span}" if start == week_start(now) else span
    head = f"{start:%Y-%m}"
    return f"本月 {head}" if start == month_start(now) else head


def _previous(start: datetime, granularity: Granularity) -> datetime:
    if granularity is Granularity.DAY:
        return start - timedelta(days=1)
    if granularity is Granularity.WEEK:
        return start - timedelta(days=7)
    if granularity is Granularity.MONTH:
        return _previous_month(start)
    return start


def _next_month(start: datetime) -> datetime:
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def _previous_month(start: datetime) -> datetime:
    if start.month == 1:
        return start.replace(year=start.year - 1, month=12)
    return start.replace(month=start.month - 1)


def _require_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("时刻必须带时区；naive 的时刻切不出 UTC 日")
    return moment


__all__ = [
    "BASIC_SLOTS",
    "COUNT_STATS_START_UTC",
    "MAX_DAY_ROWS",
    "MAX_MONTH_ROWS",
    "MAX_WEEK_ROWS",
    "RARE_SLOTS",
    "RESOURCE_STATS_START_UTC",
    "SLOT_FREE",
    "SLOT_FLYING",
    "SLOT_UNKNOWN",
    "Granularity",
    "Occupancy",
    "RunWindow",
    "available_seconds",
    "day_start",
    "line_slots",
    "month_start",
    "occupancy_end",
    "occupied_seconds",
    "overflow_lines",
    "overlap_seconds",
    "parse_granularity",
    "period_end",
    "period_label",
    "period_start",
    "period_starts",
    "recovery_rate",
    "resource_window",
    "row_limit",
    "trim_empty_tail",
    "utilisation",
    "week_start",
]
