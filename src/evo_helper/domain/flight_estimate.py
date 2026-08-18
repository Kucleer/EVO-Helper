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

## 距离公式只在**标定过的那套编组**上有效，所以要先问屏幕

`domain.flight_time` 的模块头写死了这条限制：`SECONDS_PER_ROOT_UNIT = 26.5165`
里裹着舰速，只在「速度 14.520 / 100%」那一套编组上标过。生产库（只读回测，
2026-08-18）把这条限制变成了可量的东西：

    预设 AAA  n=88   实测÷公式 的比例 0.9641 – 1.0018，跨 08-11 → 08-18 一周不动
    预设 BBB  08-14/15/18 比例 1.0000
             08-17 那一天 13 发里 9 发精确地挤在 1.2648 – 1.2662  ← 一个固定倍数
    侦察      n=482  比例约 0.026   ← 侦察艇快约 40 倍，公式对它完全不成立

用户口径（2026-08-18）：「当时的 BBB 有其他舰艇 所以影响了参数」。也就是说
**`26.5165` 不是宇宙常数，是「当前编组」的属性**，而编组是用户随时会改的东西——
改完 `preset_name` 还叫 BBB、`preset_signature` 还是那句 `预设:BBB`，
**两者都抓不住这次变化**。（`docs/预计战报时间-估算方案.md` 里那句「BBB 反解
k = 33.6」正是在 08-17 那天量的：26.5165 × 1.2648 = 33.54。同一件事的另一半。）

于是这个模块**不去猜编组变没变，而是问屏幕**：简报页上就写着「速度 14.520 /
100%」，那正是公式标定时的那一组数。读到的不是这一组，就说明这一发不在公式的
适用域里，**公式这一路直接弃权**——不外推、不缩放、不拟合。

为什么不做「按预设滚动学一个比例」（这是被认真考虑过、然后放弃的方案）：

- 那要拿历史 `flight_seconds` 当样本，而**那一列里已经混着读错的值**：生产库里
  有 9 发 `X分0秒`，公式一算全是 `X分51~58秒`——是「秒」那一段被 OCR 丢掉、
  而 `report_wait._reads_the_whole_duration` 看不出来（剩下的 `eat)` 既没数字
  也没单位字）。拿脏样本学比例，学出来的偏差会自我印证。
- 它有冷启动问题：没有历史时比例未知，而**假定 1.0 正是 08-17 会犯的错**。
- 它要等好几发同向偏离才敢重新学，而屏幕上那个数**第一发就变了**。
- 而这个数能不能读，是量过的：同样那 49 张实拍上，默认配方读出 `'14.520'` /
  `'100%'` **47/47**，剩下 2 张正是面板压根没铺开的那两张。

⚠️ **速度只当「在不在适用域内」这个是非题用，不参与任何算术。**
「飞行时间与速度成反比」这件事本仓**没有数据**（库里从来没记过速度），
把 `14.520 / 读到的速度` 当倍数乘上去就是编数——那正是本仓禁止的
「猜出来的数长得像量出来的」。读到别的值，公式弃权，仅此而已。

## 同一个恒星系内那一档，公式弃权

    9:250:8  → 9:250:16   实测 600s   公式 906s
    4:277:15 → 4:277:14   实测 616s   公式 906s
    2:137:18 → 2:137:1/3/4  实测 480~495s   公式 906s

`distance_units` 在恒星系环距为 0 时取固定的 `SAME_GALAXY_BASE_UNITS = 1162`，
而反推出来只有约 520。更要命的是**同一档里实测还不一样**（480 / 600 / 616），
说明行星位次也进算式，而公式里压根没有这一维。7 个点标不出一个新常数，
所以这一档**先弃权**，别顺手去改那个 1162。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from evo_helper.domain.distance import galaxy_gap, system_gap
from evo_helper.domain.flight_time import one_way_seconds
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


#: 公式标定时那套编组在简报页上显示的速度与航速百分比。
#:
#: ⚠️ **这是标定常量，不是偏好项**，而且它是 `domain.flight_time.
#: SECONDS_PER_ROOT_UNIT` 的**孪生记录**：那个系数就是在这一组数上标出来的。
#: 改这里不会让结果「更适合我」，只会让公式被用到它没验过的编组上。
#: 哪天用真的换了编组并重新标定了那个系数，这两个值要一起改。
#:
#: 存成字符串而不是 float：它是拿来**逐字比**的，不参与算术（见模块头）。
#: 比成数值就会诱使下一个人写 `14.520 / speed` 那个乘法。
CALIBRATED_FLEET_SPEED = "14.520"
CALIBRATED_SPEED_PERCENT = "100%"

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

    @property
    def is_measured(self) -> bool:
        """这个数是**从屏幕上读出来的**吗（而不是算出来的）。"""
        return self.source in (FlightSource.BRIEFING_ARRIVAL, FlightSource.BRIEFING_DURATION)


def fleet_matches_calibration(speed: str | None, percent: str | None) -> bool:
    """简报页上的速度是不是公式标定时的那一组。

    **逐字比，不做任何归一化**（除了去掉首尾空白）。理由与
    `game.system_navigator._reads_as` 的「三个读数逐字就是这个坐标吗」同形：
    这两个值是 OCR 的产物，任何「差不多」的放宽都会把一次误读放行成一次
    「还在适用域内」的误判，而那正是这道判据要挡的东西。判不出来就弃权，
    弃权只是少一个来源。
    """
    return (speed or "").strip() == CALIBRATED_FLEET_SPEED and (
        percent or ""
    ).strip() == CALIBRATED_SPEED_PERCENT


def predict_flight(
    target: Coordinate,
    origin: Coordinate,
    *,
    speed: str | None,
    percent: str | None,
) -> timedelta | None:
    """距离公式给出的单程飞行时长；不在适用域内就返回 None（弃权）。

    两处弃权，理由都在模块头：

    1. 简报页上的速度不是标定那一组——编组换了，系数就是另一个数；
    2. 目标与出发星在**同一个恒星系**——那一档公式的 `1162` 反推只有约 520，
       而且实测同档内还不一样（480 / 600 / 616），说明行星位次也进算式。
    """
    if not fleet_matches_calibration(speed, percent):
        return None
    if galaxy_gap(target.galaxy, origin.galaxy) == 0 and (
        system_gap(target.system, origin.system) == 0
    ):
        return None
    return timedelta(seconds=one_way_seconds(target, origin))


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
    | 两个 OCR 都有、彼此不合 | 让公式当裁判，裁不出来 → **一个都不采信** |
    | 只有一个 OCR、公式有值 | 比公式小得离谱就丢掉（截断指纹），否则采信 |
    | 只有一个 OCR、公式弃权 | 采信，但在 `reason` 里写明**只有单一来源** |
    | 一个 OCR 都没有、公式有值 | 用公式的值，`source` 记 `DISTANCE_MODEL` |
    | 什么都没有 | `flight=None`，照派，闹钟留空 |

    ⚠️ **两个 OCR 对不上而公式又裁不了时，两个都不采信。** 挑一个信是最坏的
    选择：一个错的小数字会同时污染两个钟（战报到点时刻 + 航线空出时刻）
    且一声不响，而 None 只是多白跑一趟。这与 `report_wait.parse_game_duration`
    「部分匹配一律失败」是同一条道理。
    """
    kwargs = {
        "arrival_flight": arrival_flight,
        "duration_flight": duration_flight,
        "model_flight": model_flight,
    }

    def undershoots(value: timedelta) -> bool:
        if model_flight is None:
            return False
        return value < model_flight * MODEL_UNDERSHOOT_REJECT_RATIO

    if arrival_flight is not None and duration_flight is not None:
        if abs(arrival_flight - duration_flight) <= BRIEFING_SKEW_TOLERANCE:
            return FlightEstimate(
                flight=arrival_flight,
                source=FlightSource.BRIEFING_ARRIVAL,
                reason=(f"到达时间与飞行时间吻合（差 {abs(arrival_flight - duration_flight)}）"),
                **kwargs,
            )
        # 两个 OCR 打架。公式是唯一不依赖 OCR 的第三方，让它裁。
        arrival_ok = model_flight is not None and not undershoots(arrival_flight)
        duration_ok = model_flight is not None and not undershoots(duration_flight)
        if arrival_ok and not duration_ok:
            return FlightEstimate(
                flight=arrival_flight,
                source=FlightSource.BRIEFING_ARRIVAL,
                reason="两个读数打架；飞行时间那一行远小于公式，按截断丢掉，采信到达时间",
                **kwargs,
            )
        if duration_ok and not arrival_ok:
            return FlightEstimate(
                flight=duration_flight,
                source=FlightSource.BRIEFING_DURATION,
                reason="两个读数打架；到达时间远小于公式，按截断丢掉，采信飞行时间",
                **kwargs,
            )
        return FlightEstimate(
            flight=None,
            source=None,
            reason="两个读数打架，公式裁不出来；两个都不采信，回程闹钟留空",
            **kwargs,
        )

    single = arrival_flight if arrival_flight is not None else duration_flight
    if single is not None:
        source = (
            FlightSource.BRIEFING_ARRIVAL
            if arrival_flight is not None
            else FlightSource.BRIEFING_DURATION
        )
        if undershoots(single):
            if model_flight is not None:
                return FlightEstimate(
                    flight=model_flight,
                    source=FlightSource.DISTANCE_MODEL,
                    reason="唯一那个读数远小于公式（截断指纹），丢掉；改用公式算出来的值",
                    **kwargs,
                )
        else:
            note = "只有这一个来源" if model_flight is None else "与公式不矛盾"
            return FlightEstimate(
                flight=single, source=source, reason=f"{source.value}：{note}", **kwargs
            )

    if model_flight is not None:
        return FlightEstimate(
            flight=model_flight,
            source=FlightSource.DISTANCE_MODEL,
            reason="两个读数都读不出；用距离公式算出来的值（**不是读出来的**）",
            **kwargs,
        )
    return FlightEstimate(
        flight=None,
        source=None,
        reason="三个来源都没有值；这一发照派，回程闹钟留空",
        **kwargs,
    )


__all__ = [
    "BRIEFING_SKEW_TOLERANCE",
    "CALIBRATED_FLEET_SPEED",
    "CALIBRATED_SPEED_PERCENT",
    "MODEL_UNDERSHOOT_REJECT_RATIO",
    "FlightEstimate",
    "FlightSource",
    "fleet_matches_calibration",
    "predict_flight",
    "reconcile_flight",
]
