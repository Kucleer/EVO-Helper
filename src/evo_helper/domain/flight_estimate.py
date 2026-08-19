"""简报页上那一发到底飞多久：**三个来源，一个结论**，而且结论要说得清它是谁给的。

## 为什么要三个来源

到 2026-08-18 为止只有一个来源——简报页「飞行时间」那一行的 OCR——而它在实机上
24 小时 62 发里读不出 14 发（23%）。读不出的代价不是白跑一趟，是那一发按
`report_wait.UNKNOWN_LINE_HOLD`（90 分钟）占着航线，而实测往返只有约 46 分钟：
**每次白占约 44 分钟航线，一天约 10 航线小时**，占 2 星 × 4 线 × 24h 总容量的 5.4%。

三个来源，按可信度排：

1. **`BRIEFING_ARRIVAL`——简报页「预计到达时间」减去读屏时刻。** 主来源。
   那一行长这样 `16/08/2026 09:31:27`，纯数字加分隔符、**一个中文字都没有**，
   恰好避开了当前全部失败的成因（`分` 被读成 `5)`、`秒` 被读成 `%`）。
   49 张失败现场实测 47/47、零读错，详见 `game.pirate_ui.ARRIVAL_RECIPES`。
2. **`BRIEFING_DURATION`——「飞行时间」那一行的 OCR。** 降为交叉校验。
3. **`DISTANCE_MODEL`——`domain.flight_time` 的距离公式。** 它**不依赖 OCR**，
   而且在点「出发！」之前就算得出来（起点与终点都是我们自己填进去的）。

## 到达时间为什么可以直接减出时长——这是量出来的，不是推的

拿本机 49 张失败现场核过（`var/logs/dump-briefing-flight-unreadable-*.png`）：
文件名里的 `HHMMSS` 是存图那一刻的本机时刻，

    (画面上的到达时间) - (存图时刻转 UTC) - (画面上的飞行时长)

在 47 张上的分布是 `{-1 秒: 23 张, 0 秒: 24 张}`。也就是说：**到达时间是每秒重算
的**（不是面板铺开那一刻定死的），而且本机时钟与游戏时钟是同步的。所以
`到达时间 - 现在` 就是剩余飞行时长，精度 1 秒。

这正是 `vision.parsers.parse_dispatch_briefing` 早就写下的口径——「到达时间是主
来源：它不依赖本机时钟与游戏时钟同步，也不会因为『读完到点击出发』之间的耗时
而漂移」——现在有像素证据了。

## 距离公式的系数**按出发星球各学一个**（2026-08-19 改）

`domain.flight_time` 的 `SECONDS_PER_ROOT_UNIT = 26.5165` 里裹着舰速，而
用户口径（2026-08-19）：**「每个球的速度都会有点不一样的」**。所以一个全局
常数结构上只可能对一颗星球成立。生产库（只读回测，2026-08-19）把这句话量成了
数字——反解 `单程秒 = 2 + k·√D`，**只看跨银河那一档**：

    出发星球     n     k 中位     用本星球 k 预测   用全局常数预测
    4:277:15    56   26.5165    中位 0.00%       中位 0.00%
    9:250:8     19   26.3327    中位 0.00%       中位 0.70%
    2:137:18     5   26.5165    中位 0.00%       中位 0.00%

⚠️ **上一版这里用的是「屏幕上的速度必须逐字等于 14.520」这道准入闸，而它正是
2026-08-19 那次故障的第二根导火索**：那三发从 9:250:8 起飞，屏幕上写着
`14.720`，于是公式对**整颗星球**一次都不生效。而那三发本该是送分题——
用 9:250:8 自己的 k 算出来 3726 秒，到达时间读到 3725 秒，差 1 秒；
飞行时间那一行读到 126 秒，离群 29 倍，一眼就该丢掉。

⚠️ **速度并没有被扔掉，是降级了**：从「准入闸」降成「编组变了」的探测器。
它**仍然不参与任何算术**——按速度比缩放系数是错的，实测：
`9:250:8 的 k ÷ 4:277:15 的 k = 0.9931`，而速度比 `14.520/14.720 = 0.98641`，
**差 0.7%**。速度是个好的变化探测器，不是好的换算因子。用法见
`fit_seconds_per_root_unit`。

## 只学「跨银河」那一档，同银河与同恒星系一律弃权

同一批回测里，非跨银河那两档的误差大得没法用：

    ATTACK 2:137:18 同银河  n=119  预测误差中位 28.3%  p90 33.0%
    ATTACK 4:277:15 同银河  n=84   预测误差中位 0.06%  p90 3.34%
    ATTACK 2:137:18 同系    n=35   预测误差中位 2.24%  最大 44.4%

    9:250:8  → 9:250:16     实测 600s   公式 906s
    4:277:15 → 4:277:14     实测 616s   公式 906s
    2:137:18 → 2:137:1/3/4  实测 480~495s   公式 906s

根子在 `distance_units` 自己：恒星系环距为 0 时它取固定的
`SAME_GALAXY_BASE_UNITS = 1162`，而反解出来只有约 520；同一档里实测还互不相同
（480 / 600 / 616），说明行星位次也进算式，而公式里压根没有这一维。
**换一个 k 救不了一个形状本身就错的 D**，所以这两档一律弃权，别顺手去改 1162。

## 拿历史 `flight_seconds` 当样本，脏数据怎么办

这条早先被否过一次，理由是那一列里混着读错的值（生产库里 9 发 `X分0秒`、
66 发只剩秒段的残骸）。**用中位数就绕开了**：中位数只要坏样本不过半就纹丝不动，
而上表里坏样本占比远低于一半。另外两条否决理由也各自有了答案：

- 冷启动 → **样本不足就弃权**，不假定任何默认比例（见 `MIN_LEARNING_SAMPLES`）；
- 「要等好几发同向偏离才敢重新学」 → 屏幕上的速度就是那个即时信号，
  记进 `attack_dispatches.fleet_speed_raw`，速度一变，旧样本立刻不算数。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from math import sqrt
from statistics import median

from evo_helper.domain.distance import galaxy_gap
from evo_helper.domain.flight_time import (
    LAUNCH_OVERHEAD_SECONDS,
    ROUND_TRIP_MULTIPLIER,
    distance_units,
    one_way_seconds,
)
from evo_helper.domain.models import Coordinate


class FlightSource(Enum):
    """这一发的飞行时长是谁给的。

    ⚠️ **落库时必须存下来。** 本仓硬规矩：猜出来的数不许长得像量出来的
    （`docs/预计战报时间-估算方案.md` 第 2 条、`storage.models` 里
    `target_military_score_estimated` 那一段都是同一条）。三个来源的可信度
    差着数量级，事后查账时「这个 90 分钟是读出来的还是算出来的」必须答得上。
    """

    #: 简报页「预计到达时间」减去读屏时刻。**主来源**，实测 47/47。
    BRIEFING_ARRIVAL = "briefing_arrival"
    #: 简报页「飞行时间」那一行的 OCR。实测在失败帧上 0/47，现降为交叉校验。
    BRIEFING_DURATION = "briefing_duration"
    #: `domain.flight_time` 的距离公式。**算出来的，不是读出来的。**
    DISTANCE_MODEL = "distance_model"


#: 学一个系数至少要几发实测。少于这个数就弃权，**不给任何默认比例**。
#:
#: 取 3 的依据（生产库回测，2026-08-19，跨银河那一档，按时间排、拿前 n 发学、
#: 去预测后面所有发次）：
#:
#:     出发星球     前 1 发学     前 3 发学     前 8 发学
#:     4:277:15    中位 0.00%    中位 0.00%    中位 0.00%
#:     9:250:8     中位 0.00%    中位 0.00%    中位 0.00%
#:     2:137:18    最大 0.01%    最大 0.00%    （只有 5 发）
#:
#: 也就是说**精度上 1 发就够了**——这个门限挡的不是精度，是**单点被污染**：
#: `flight_seconds` 那一列里混着 OCR 截断的残骸，而中位数要「坏样本不过半」
#: 才免疫。3 是能容忍一个坏样本的最小奇数。
#:
#: 再往上调没有收益、只有代价：换了编组之后（速度一变，旧样本全部作废，
#: 见 `fit_seconds_per_root_unit`）要等够这么多发新的才重新有公式可用，
#: 而那段空窗期里三个来源又只剩两个。
#:
#: 分类：**标定常量，不是偏好项**。它由「中位数要过半才稳」这条算术定死。
MIN_LEARNING_SAMPLES = 3

#: 学系数时最多往回取几发。
#:
#: 有这个窗口，是因为**存量样本没有速度**（`fleet_speed_raw` 这一列 2026-08-19
#: 才加）。速度对不上的样本会被当场剔掉，而速度为 NULL 的存量样本剔不掉——
#: 它们只能靠「被新样本挤出窗口」老去。取 20：实机一天几十发，换一次编组之后
#: 大约十来发（几小时）新样本就能把中位数翻过来，而 20 又远大于
#: `MIN_LEARNING_SAMPLES`，正常情况下不会因为窗口太小而弃权。
#:
#: ⚠️ 这个窗口**不是**「只信最近的数据」那种平滑参数。同一颗星球同一套编组的
#: k 在回测里跨一周纹丝不动（26.5165 / 26.3327 各自 min≈max），窗口再大也不会
#: 更准。它存在的唯一理由是上面那批**无速度的存量样本**，等它们全部老去之后
#: 这个数就没有作用了。
#:
#: 分类：**标定常量**。
LEARNING_WINDOW = 20

#: OCR 读出来的时长比公式小到这个比例以下，就当**读错**丢掉。
#:
#: 只关「小」这一侧，是因为两个失败模式的方向是反的：
#:
#: - `report_wait.parse_game_duration` 的截断残骸**只会把值变小**（丢掉「分」
#:   那一段就只剩秒，docstring 里「59 是秒字段能装下的最大数」说的就是它；
#:   丢掉「秒」那一段则得到 `X分0秒`，生产库里有 9 发）；
#: - 编组变慢**只会把值变大**，而那在物理上是可能的（08-17 那 13 发是真的飞
#:   了那么久）。把它当读错拦下就是误杀，而误杀的代价是回到 90 分钟空占。
#:
#: 取 0.95：生产库回测（n=152，剔掉同系内那一档）里正常那一峰落在
#: 0.9931–1.0021，而低于 0.95 的全是可辨认的坏读数。0.95 这条线正好把
#: 那 9 发 `X分0秒`（比例 0.9497–0.9712）划到外面——它们确实是错的。
#:
#: 分类：**标定常量，不是偏好项**。调大调小改的不是「适不适合我」，
#: 而是「一个已知读错的值算不算数」。
MODEL_UNDERSHOOT_REJECT_RATIO = 0.95

#: 飞行时间那一行比到达时间**少这么多**，就当它丢了一整段（截断指纹），
#: 采信到达时间。**只认这一个方向**，反过来不成立——见 `reconcile_flight`。
#:
#: ## 为什么需要它：公式不在场时，两个读数打架就没人裁
#:
#: 实机 2026-08-19 08:55–08:58，从 9:250:8 打三个跨银河目标，三发一模一样：
#:
#:     ROI(1050, 452, 1210, 482) 读到 'IBY 2分 6秒'
#:     到达时间 1:02:05／飞行时间 0:02:06／公式（无）
#:     → 两个读数打架，公式裁不出来；两个都不采信，回程闹钟留空
#:
#: 真值 1 时 2 分 6 秒（同一批目标别的发实测 `flight_seconds = 3726`）。
#: **`1时` 被 OCR 糊成了 `IBY`**：那是本仓已知的中文字失败族（`分` → `5}`、
#: `秒` → `%`）里的一员，只是这一次糊掉的是**最左那一段**。而
#: `report_wait._reads_the_whole_duration` 对它无能为力——它的三条判据只认
#: 「匹配之外还剩数字」「紧邻左边还杵着单位字」，而 `IBY ` 两样都没有。
#: 那个函数的 docstring 里早就写着这个残留的洞（`ЖЖЖ36分7秒`）。
#:
#: ## 取 5 分钟的依据（生产库 `attack_dispatches`，2026-08-19 回测）
#:
#: 拿距离公式当尺子量「丢一段」到底丢掉多少（只取跨恒星系那一档，n=355）：
#:
#:     丢「秒」那一段（读成 X分0秒）  n=10  缺口 22.1 – 58.4 秒
#:     丢「分」整段（读数 <180 秒）   n=66  缺口 898.1 – 2330.4 秒
#:     丢「时」整段（本次这三发）      n=3   缺口 3599 秒
#:
#: **58.4 秒到 898.1 秒之间一条都没有**——15 倍宽的一段空白，与
#: `report_wait.MIN_CREDIBLE_ATTACK_FLIGHT` 那 60–300 秒的空白是同一种指纹。
#: 5 分钟落在这段空白里：比最大的「丢秒」缺口大 5 倍，比最小的「丢分」缺口
#: 小 3 倍，也是 `BRIEFING_SKEW_TOLERANCE` 的 5 倍。
#:
#: 往低取比往高取安全：低了只会把一次**本来就该丢的**打架也判成截断，而那时
#: 采信的是到达时间（主来源，49 张实拍上 47/47 零读错）；高了则是漏掉一次
#: 截断，退回 90 分钟空占——正是这次要修的东西。
#:
#: 分类：**标定常量，不是偏好项**。它由 OCR 的失败形状定死，调了就是错。
TRUNCATED_SEGMENT_SHORTFALL = timedelta(minutes=5)

#: 拿距离公式当航线**兜底占用**时，往返秒数再乘这个系数。
#:
#: ⚠️ **宁可高估不要低估。** 高估只是晚一点把航线放出来（少派几发）；低估会让
#: 调度器以为有航线、派出去撞游戏的「同时派遣的舰队数量已达上限。」，白跑一整轮
#: （`repository.count_inflight` 的注释里写过同一条取舍，方向别弄反）。
#:
#: 取 1.3 的依据（生产库 `attack_dispatches`，2026-08-19 回测，n=355 跨恒星系
#: 攻击）：**实测÷公式** 的上端是 1.2662——2026-08-17 那批 BBB，用户口径
#: 「当时的 BBB 有其他舰艇 所以影响了参数」。1.3 盖住它并留 2.7% 余量。
#:
#: 比 1.2662 更大的样本只有一发（5.5506，2026-08-18 18:56 记在 9:250:8 名下的
#: 那一发），而那一发是**记账错**不是飞行慢：出发星球被游戏悄悄退回主星，
#: 整段经过写在 `tools.pirate_loop.PirateLoop._require_origin_before_dispatch`
#: 上，那道闸门就是为它加的。拿一条已知的坏记录去抬系数，等于把一个已经修掉的
#: 缺陷永久编进常数里。
#:
#: 分类：**标定常量，不是偏好项**。想调「航线放得快一点还是慢一点」的人要动的是
#: 攻击配置页上那个 `unknown_line_hold_minutes`，不是这里。
LINE_HOLD_SAFETY_FACTOR = 1.3

#: 两个 OCR 来源之间允许差多少。
#:
#: ⚠️ **这个常量原先住在 `vision.parsers`（名字一样），现在搬到这里，那边改成
#: 从这里 import 再 re-export。** 搬家的理由是分层：`domain` 不许 import
#: `vision`（`vision.parsers` 自己就反过来 import `domain.report_wait`），
#: 而这道容差现在两边都要用。**不许在这里另造一个数**——任务书里点名要求沿用
#: 那一个：它本来就是给「读完这一屏到点击出发之间还有耗时」留的。
#:
#: 分类：**标定常量，不是偏好项**。
BRIEFING_SKEW_TOLERANCE = timedelta(minutes=1)


@dataclass(frozen=True)
class FlightEstimate:
    """定下来的飞行时长，以及它是谁给的、当时另外两个来源是什么。

    `flight` 为 None 表示**三个来源没有一个可信**——那时调用方照旧派遣，
    只是回程闹钟留空。飞行时间是闹钟不是闸门（`tools.pirate_loop.
    _read_flight_time` 的注释记着这条链路已经因为「ROI 与放大倍数不配」
    白白拦下过四发）。
    """

    flight: timedelta | None
    source: FlightSource | None
    #: 落进 `system_log` 的现场：三个来源各自读到/算到什么，以及为什么这么判。
    reason: str
    arrival_flight: timedelta | None = None
    duration_flight: timedelta | None = None
    model_flight: timedelta | None = None
    #: 派出这一刻屏幕上的舰速原文。**落库**（`attack_dispatches.fleet_speed_raw`），
    #: 好让下一次学系数时能认出「编组换了」。不参与任何算术。
    fleet_speed: str | None = None
    #: 这一发用的是哪个系数、基于几发学出来的。**只进日志**，不落库——
    #: 它是从库里的样本现算出来的，存下来只会有两份说法。
    coefficient: FlightCoefficient | None = None

    @property
    def is_measured(self) -> bool:
        """这个数是**从屏幕上读出来的**吗（而不是算出来的）。"""
        return self.source in (FlightSource.BRIEFING_ARRIVAL, FlightSource.BRIEFING_DURATION)


@dataclass(frozen=True, slots=True)
class FlightSample:
    """一发历史实测：飞到哪、从哪起飞、屏幕上量到多少秒、当时的舰速与发次类型。

    `fleet_speed` 为 None 表示**那一发没记速度**（2026-08-19 之前的存量行）。
    """

    target: Coordinate
    origin: Coordinate
    flight_seconds: float
    mission_kind: str
    fleet_speed: str | None = None


@dataclass(frozen=True, slots=True)
class FlightCoefficient:
    """学出来的 `√距离单位 → 秒` 系数，以及它是**基于几发**学的。

    ⚠️ **样本数必须跟着系数一起走。** 这是个拟合参数，不是标定常量：出事时
    「为什么公式给出这个数」要答得上来，而那句话必须包含「用的是哪颗星球的 k、
    基于几发」（CLAUDE.md 那条「出事时能不能只靠库里的日志定位」）。
    """

    seconds_per_root_unit: float
    samples: int


def fit_seconds_per_root_unit(
    samples: Sequence[FlightSample],
    *,
    origin: Coordinate,
    mission_kind: str,
    fleet_speed: str | None,
) -> FlightCoefficient | None:
    """从这颗出发星球的历史实测里学出系数；学不出来返回 None（弃权）。

    ⚠️ **`samples` 必须按派出时刻从新到旧排好**：筛完之后只取最近
    `LEARNING_WINDOW` 发（先筛后截，不能反过来——2:137:18 最近 20 发里可能
    一发跨银河都没有，先截就永远学不出来）。

    ## 只用四种样本

    1. **同一颗出发星球**——用户口径（2026-08-19）：「每个球的速度都会有点
       不一样的」。回测里 9:250:8 的 k 是 26.3327、4:277:15 是 26.5165，
       混在一起学就两颗星球都不准。
    2. **同一种发次**——侦察艇快约 40 倍（回测里 SCOUT 的 k 是 0.35–0.64，
       ATTACK 是 26.5），拿侦察去标定攻击就是数量级错位。
    3. **跨银河那一档**——同银河与同恒星系的误差大到没法用（模块头那张表），
       而根子在 `distance_units` 的 `D` 本身，换个 k 救不了。
    4. **速度对得上的**——`fleet_speed` 记着那一发派出时屏幕上的速度。
       与眼下读到的不一样，就说明编组换了，那一发不算数。
       ⚠️ **速度为 None 的存量样本照收**：「没记过」不等于「不一样」，
       而 2026-08-19 之前所有样本都是这一档。它们靠 `LEARNING_WINDOW`
       随时间老去。

    ## 取中位数而不是平均

    `flight_seconds` 那一列里混着 OCR 截断的残骸（生产库里 66 发只剩秒段、
    9 发 `X分0秒`）。平均值会被一发 20 秒的残骸拽得很低，中位数只要坏样本
    不过半就纹丝不动——这正是「拿历史当样本」这条路早先被否掉的那个理由的解药。

    ⚠️ **速度不参与任何算术。** 它只做「一样 / 不一样」这个是非题。按速度比
    缩放系数是错的，实测：`9:250:8 的 k ÷ 4:277:15 的 k = 0.9931`，而速度比
    `14.520 / 14.720 = 0.98641`——差 0.7%，比不用它还糟。
    """
    usable = [
        (sample.flight_seconds - LAUNCH_OVERHEAD_SECONDS)
        / sqrt(distance_units(sample.target, sample.origin))
        for sample in samples
        if sample.origin == origin
        and sample.mission_kind == mission_kind
        and galaxy_gap(sample.target.galaxy, sample.origin.galaxy) != 0
        and sample.flight_seconds > LAUNCH_OVERHEAD_SECONDS
        and (sample.fleet_speed is None or sample.fleet_speed == fleet_speed)
    ][:LEARNING_WINDOW]
    if len(usable) < MIN_LEARNING_SAMPLES:
        return None
    return FlightCoefficient(seconds_per_root_unit=median(usable), samples=len(usable))


def predict_flight(
    target: Coordinate,
    origin: Coordinate,
    *,
    coefficient: FlightCoefficient | None,
) -> timedelta | None:
    """距离公式给出的单程飞行时长；不在适用域内就返回 None（弃权）。

    两处弃权，理由都在模块头：

    1. **这颗出发星球还没学出系数**（`coefficient` 为 None）——样本不够，
       或者速度刚变过、旧样本全被剔掉了。**不许拿全局那个 26.5165 顶上**：
       那个数是 4:277:15 的属性，用到 9:250:8 上就差 0.7%，用到换了编组的
       那一天上差 26%。
    2. **目标与出发星在同一个银河**——同银河那一档预测误差中位数到 28%，
       同恒星系那一档公式的 `1162` 反推只有约 520 且同档内实测还互不相同
       （480 / 600 / 616）。那不是系数的问题，是 `D` 的形状本身错了。
    """
    if coefficient is None:
        return None
    if galaxy_gap(target.galaxy, origin.galaxy) == 0:
        return None
    return timedelta(
        seconds=one_way_seconds(
            target, origin, seconds_per_root_unit=coefficient.seconds_per_root_unit
        )
    )


def line_hold_round_trip(target: Coordinate, origin: Coordinate) -> timedelta | None:
    """三个来源全军覆没时，这条航线**至少**还要占多久。算不出来就返回 None。

    ⚠️ **这不是「这一发飞多久」，是一个上界。** 两者要的东西正相反，所以判据
    也不一样，别把这个函数和 `predict_flight` 合并：

    - `predict_flight` 要给出一个**能落库当飞行时长**的值，所以没学出这颗星球
      的系数就宁可弃权——那个值会同时喂给战报闹钟；
    - 这里只回答「航线还占着吗」，而调用方拿它与
      `report_wait.UNKNOWN_LINE_HOLD`（或用户在攻击配置页上填的那个数）
      **取大**，于是它**只能把占用拉长、永远不会缩短**。

    ## 所以这里用全局那个标定系数就够，不必等学

    上界不需要准，只需要**够长**：生产库回测（2026-08-19，n=355 跨恒星系攻击）
    里，实测÷公式最慢的一套编组是 1.2662（2026-08-17 那批混编的 BBB），
    `LINE_HOLD_SAFETY_FACTOR` 就是照它取的，而 1.3 也顺带盖住了「用别的星球的
    系数」那 0.7% 偏差。

    反过来说，**这一路不能要求先学出系数**：那正是 2026-08-19 出事的形状——
    公式因为适用域闸对整颗星球一次都不生效，于是兜底永远退回那个常数。

    ## 同一个银河之内那一档弃权

    公式在那两档是 known-wrong 的（同银河预测误差中位数到 28%，同恒星系的
    `1162` 反推只有约 520，见模块头）。而它们算出来的往返都在 52 分钟以内，
    本来就压在 90 分钟的默认值底下、取大之后一步都动不了——弃权不损失任何
    东西，却省下一个日后有人把默认值调小时会突然生效的错值。
    """
    if galaxy_gap(target.galaxy, origin.galaxy) == 0:
        return None
    round_trip = ROUND_TRIP_MULTIPLIER * one_way_seconds(target, origin)
    return timedelta(seconds=round_trip * LINE_HOLD_SAFETY_FACTOR)


def reconcile_flight(
    *,
    arrival_flight: timedelta | None,
    duration_flight: timedelta | None,
    model_flight: timedelta | None,
) -> FlightEstimate:
    """三个来源合成一个结论。

    ## 判据

    | 情形 | 结论 |
    |---|---|
    | 两个 OCR 都有、彼此吻合 | 取**到达时间**（主来源），公式只做旁证 |
    | 两个 OCR 都有、彼此不合 | 让公式当裁判 |
    | 同上，公式弃权而飞行时间**小一整段** | 截断指纹，采信到达时间 |
    | 同上，其余 | **一个都不采信** |
    | 只有一个 OCR、公式有值 | 比公式小得离谱就丢掉（截断指纹），否则采信 |
    | 只有一个 OCR、公式弃权 | 采信，但在 `reason` 里写明**只有单一来源** |
    | 一个 OCR 都没有、公式有值 | 用公式的值，`source` 记 `DISTANCE_MODEL` |
    | 什么都没有 | `flight=None`，照派，闹钟留空 |

    ⚠️ **两个 OCR 对不上而公式又裁不了时，默认两个都不采信。** 挑一个信是最坏的
    选择：一个错的小数字会同时污染两个钟（战报到点时刻 + 航线空出时刻）
    且一声不响，而 None 只是多白跑一趟。这与 `report_wait.parse_game_duration`
    「部分匹配一律失败」是同一条道理。

    ## 唯一的例外：截断这一个方向

    ⚠️ **两个来源的错法方向是不对称的，所以处置也不能对称。**

    - 飞行时间那一行的失败是**截断**——丢掉最左那一段（`1时` 糊成 `IBY`）、
      或丢掉「分」那一段——它**只会把值变小**；
    - 到达时间是一个**绝对时刻**，它读错不会系统性偏小（实测的两次误读
      `09:26:27→03:26:27`、`21:13:21→20:13:21` 也都是偏小）。

    所以「飞行时间比到达时间少了一整段」是**截断的指纹**，那时采信到达时间。
    阈值与实测证据在 `TRUNCATED_SEGMENT_SHORTFALL`。

    ⚠️ **反方向不许照搬**：飞行时间比到达时间大很多时**不自动采信飞行时间**——
    那种情形本仓没有已知的机理，保持「两个都不采信」。

    ⚠️ 这一条只在**公式弃权**时才轮得到。公式在场时它才是指定的裁判，
    而「两个读数都不比公式小得离谱」这个结论本身就说明飞行时间那一行有旁证
    （它与公式吻合），这时再拿到达时间去压它就是无中生有。
    """

    def decided(
        flight: timedelta | None, source: FlightSource | None, reason: str
    ) -> FlightEstimate:
        """把结论和**当时另外两个来源各是什么**一起装起来。

        三个来源逐个写出来而不是 `**kwargs` 一把塞：塞进去之后类型检查看不出
        少写了哪个字段，而少写一个的后果是日志里那句「三个来源各读到什么」
        缺一角——出事时正是靠它定位的。
        """
        return FlightEstimate(
            flight=flight,
            source=source,
            reason=reason,
            arrival_flight=arrival_flight,
            duration_flight=duration_flight,
            model_flight=model_flight,
        )

    def undershoots(value: timedelta) -> bool:
        if model_flight is None:
            return False
        return value < model_flight * MODEL_UNDERSHOOT_REJECT_RATIO

    if arrival_flight is not None and duration_flight is not None:
        if abs(arrival_flight - duration_flight) <= BRIEFING_SKEW_TOLERANCE:
            return decided(
                arrival_flight,
                FlightSource.BRIEFING_ARRIVAL,
                (f"到达时间与飞行时间吻合（差 {abs(arrival_flight - duration_flight)}）"),
            )
        # 两个 OCR 打架。公式是唯一不依赖 OCR 的第三方，让它裁。
        arrival_ok = model_flight is not None and not undershoots(arrival_flight)
        duration_ok = model_flight is not None and not undershoots(duration_flight)
        if arrival_ok and not duration_ok:
            return decided(
                arrival_flight,
                FlightSource.BRIEFING_ARRIVAL,
                "两个读数打架；飞行时间那一行远小于公式，按截断丢掉，采信到达时间",
            )
        if duration_ok and not arrival_ok:
            return decided(
                duration_flight,
                FlightSource.BRIEFING_DURATION,
                "两个读数打架；到达时间远小于公式，按截断丢掉，采信飞行时间",
            )
        if model_flight is None and arrival_flight - duration_flight >= TRUNCATED_SEGMENT_SHORTFALL:
            # 公式弃权，但这一对读数自己就带着截断指纹：飞行时间比到达时间少了
            # 一整段（见 `TRUNCATED_SEGMENT_SHORTFALL`）。**只放行这一个方向。**
            return decided(
                arrival_flight,
                FlightSource.BRIEFING_ARRIVAL,
                (
                    f"两个读数打架且公式弃权；飞行时间比到达时间少了 "
                    f"{arrival_flight - duration_flight}（丢段指纹），采信到达时间"
                ),
            )
        return decided(None, None, "两个读数打架，公式裁不出来；两个都不采信，回程闹钟留空")

    single = arrival_flight if arrival_flight is not None else duration_flight
    if single is not None:
        source = (
            FlightSource.BRIEFING_ARRIVAL
            if arrival_flight is not None
            else FlightSource.BRIEFING_DURATION
        )
        if undershoots(single):
            if model_flight is not None:
                return decided(
                    model_flight,
                    FlightSource.DISTANCE_MODEL,
                    "唯一那个读数远小于公式（截断指纹），丢掉；改用公式算出来的值",
                )
        else:
            note = "只有这一个来源" if model_flight is None else "与公式不矛盾"
            return decided(single, source, f"{source.value}：{note}")

    if model_flight is not None:
        return decided(
            model_flight,
            FlightSource.DISTANCE_MODEL,
            "两个读数都读不出；用距离公式算出来的值（**不是读出来的**）",
        )
    return decided(None, None, "三个来源都没有值；这一发照派，回程闹钟留空")


__all__ = [
    "BRIEFING_SKEW_TOLERANCE",
    "LEARNING_WINDOW",
    "LINE_HOLD_SAFETY_FACTOR",
    "MIN_LEARNING_SAMPLES",
    "MODEL_UNDERSHOOT_REJECT_RATIO",
    "TRUNCATED_SEGMENT_SHORTFALL",
    "FlightCoefficient",
    "FlightEstimate",
    "FlightSample",
    "FlightSource",
    "fit_seconds_per_root_unit",
    "line_hold_round_trip",
    "predict_flight",
    "reconcile_flight",
]
