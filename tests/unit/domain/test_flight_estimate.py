"""三个来源怎么合成一个飞行时长，以及**每一处判据挡掉了什么**。

背景：到 2026-08-18 为止只有「简报页飞行时间那一行」一个来源，实机 24 小时
62 发里读不出 14 发（23%），每次白占约 44 分钟航线。判据本身与像素无关，
所以全在这里用假读数验；「像素上到底读不读得出」由
`tests/integration/vision/test_briefing_arrival_live.py` 拿实拍守。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from math import sqrt

import pytest

from evo_helper.domain.flight_estimate import (
    BRIEFING_SKEW_TOLERANCE,
    LEARNING_WINDOW,
    LINE_HOLD_SAFETY_FACTOR,
    MIN_LEARNING_SAMPLES,
    MODEL_UNDERSHOOT_REJECT_RATIO,
    TRUNCATED_SEGMENT_SHORTFALL,
    FlightSample,
    FlightSource,
    fit_seconds_per_root_unit,
    line_hold_round_trip,
    predict_flight,
    reconcile_flight,
)
from evo_helper.domain.flight_time import (
    LAUNCH_OVERHEAD_SECONDS,
    distance_units,
    one_way_seconds,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import MISSION_KIND_ATTACK, MISSION_KIND_SCOUT

#: 用户的主星（回测里 n=56、k=26.5165）。
ORIGIN = Coordinate(4, 277, 15)
#: 用户的 2 号星（回测里 n=19、k=26.3327）。**它的 k 与主星不一样**，
#: 用户口径（2026-08-19）：「每个球的速度都会有点不一样的」。
SECOND = Coordinate(9, 250, 8)
#: **同一个银河**里的另一个恒星系。公式在这一档上是错的，见下面那条豁免。
FAR = Coordinate(4, 206, 12)
#: **同一个恒星系**里的另一颗行星。公式在这一档上错得更厉害。
SAME_SYSTEM = Coordinate(4, 277, 14)
#: 跨银河：公式唯一站得住的那一档。
CROSS_GALAXY = Coordinate(5, 279, 14)

KIND = MISSION_KIND_ATTACK


def _reconcile(arrival=None, duration=None, model=None):  # type: ignore[no-untyped-def]
    return reconcile_flight(arrival_flight=arrival, duration_flight=duration, model_flight=model)


def _samples(origin: Coordinate, k: float, count: int) -> list[FlightSample]:
    """`count` 发**跨银河**实测，每一发都正好落在 `单程秒 = 2 + k·√D` 上。

    目标一发一个银河地换，好让它们的 `D` 各不相同——全都同一个 `D` 的话，
    「反解出来的 k 一致」就成了同义反复。
    """
    made: list[FlightSample] = []
    galaxies = [galaxy for galaxy in range(1, 10) if galaxy != origin.galaxy]
    for index in range(count):
        target = Coordinate(galaxies[index % len(galaxies)], 100 + index, 5)
        assert target.galaxy != origin.galaxy
        seconds = LAUNCH_OVERHEAD_SECONDS + k * sqrt(distance_units(target, origin))
        made.append(
            FlightSample(target=target, origin=origin, flight_seconds=seconds, mission_kind=KIND)
        )
    return made


def _fit(samples, *, origin=ORIGIN, fleet_speed=None):  # type: ignore[no-untyped-def]
    return fit_seconds_per_root_unit(
        samples, origin=origin, mission_kind=KIND, fleet_speed=fleet_speed
    )


# -- 两个 OCR 来源 -----------------------------------------------------------


def test_the_arrival_time_wins_when_both_readings_agree() -> None:
    """两个都读到且吻合时，取**到达时间**。

    这不是随手挑的：`vision.parsers.parse_dispatch_briefing` 的口径是「到达时间
    是主来源：它不依赖本机时钟与游戏时钟同步，也不会因为『读完到点击出发』
    之间的耗时而漂移」，而 49 张实拍把它变成了可量的东西——到达时间 47/47，
    飞行时间那一行 0/47。
    """
    estimate = _reconcile(arrival=timedelta(minutes=30, seconds=29), duration=timedelta(minutes=30))

    assert estimate.flight == timedelta(minutes=30, seconds=29)
    assert estimate.source is FlightSource.BRIEFING_ARRIVAL


def test_two_readings_that_disagree_are_both_thrown_away() -> None:
    """⚠️ **两个来源打架而公式裁不了时，默认一个都不许采信。**

    挑一个信是最坏的选择：错值同时污染两个钟（战报到点时刻 + 航线空出时刻）
    且一声不响，而 None 只是多白跑一趟。这与
    `domain.report_wait.parse_game_duration`「部分匹配一律失败」是同一条道理。

    这里取的差是 2 分钟：大过容差（1 分钟）所以确实算打架，又够不着
    `TRUNCATED_SEGMENT_SHORTFALL`（5 分钟）那条截断线，于是两个都丢。
    把这里改成「随便取一个」或「取小的那个」，这条就红。
    """
    estimate = _reconcile(arrival=timedelta(minutes=30), duration=timedelta(minutes=28))

    assert estimate.flight is None
    assert estimate.source is None


# -- 截断这一个方向：飞行时间小一整段时采信到达时间 --------------------------


def test_a_duration_short_by_a_whole_segment_is_read_as_a_truncation() -> None:
    """⚠️ **本次修法的落点（实机 2026-08-19 08:55–08:58）。**

    从 9:250:8 打三个跨银河 bot，三发一模一样：

        ROI(1050, 452, 1210, 482) 读到 'IBY 2分 6秒'
        到达时间 1:02:05／飞行时间 0:02:06／公式（无）
        → 两个读数打架，公式裁不出来；两个都不采信，回程闹钟留空

    真值 1 时 2 分 6 秒（同一批目标别的发实测 `flight_seconds = 3726`）——
    **`1时` 被 OCR 糊成了 `IBY`**，而 `report_wait._reads_the_whole_duration`
    的三条判据对 `IBY ` 一条都不成立（既没剩数字，紧邻左边也没杵着单位字）。

    代价不是白跑一趟：那三发按 `UNKNOWN_LINE_HOLD`（90 分钟）占航线，而真实
    往返 124.2 分钟——中间那 34 分钟里，调度器与首页都以为有空闲航线。
    """
    estimate = _reconcile(arrival=timedelta(seconds=3725), duration=timedelta(minutes=2, seconds=6))

    assert estimate.flight == timedelta(seconds=3725)
    assert estimate.source is FlightSource.BRIEFING_ARRIVAL
    assert "丢段指纹" in estimate.reason


def test_the_truncation_rule_does_not_fire_in_the_other_direction() -> None:
    """⚠️ **反方向不许照搬：飞行时间比到达时间大很多时，两个都不采信。**

    截断只会把值**变小**，所以「飞行时间小一整段」才是指纹。反过来（飞行时间
    大一整段）本仓**没有已知的机理**——那时到达时间可能是被读小了，也可能是
    飞行时间被读大了，没有依据挑一个。

    把那个判据写成 `abs(arrival - duration) >= 阈值`，这条就红。
    """
    estimate = _reconcile(arrival=timedelta(minutes=2, seconds=6), duration=timedelta(seconds=3725))

    assert estimate.flight is None
    assert estimate.source is None


def test_the_truncation_rule_stands_down_while_the_model_is_on_the_bench() -> None:
    """⚠️ **公式在场时它才是裁判，截断这条规则不许越过它。**

    这里飞行时间与公式吻合（1551 秒），到达时间却大出一整段。公式已经说了
    「两个都不比我小得离谱」，那正是「飞行时间有旁证」的意思；这时再拿到达
    时间去压它，等于凭一条更弱的判据推翻一条更强的。

    把新那条判据挪到公式那两支前面、或去掉 `model_flight is None` 这个前提，
    这条就红。
    """
    estimate = _reconcile(
        arrival=timedelta(seconds=2400),
        duration=timedelta(seconds=1551),
        model=timedelta(seconds=1551),
    )

    assert estimate.flight is None
    assert estimate.source is None


def test_the_truncation_threshold_sits_inside_the_measured_gap() -> None:
    """阈值落在实测那段空白里——两侧都是量出来的，不是拍的。

    生产库 `attack_dispatches` 回测（2026-08-19，跨恒星系攻击 n=355），拿距离
    公式当尺子量「丢一段」丢掉多少：

        丢「秒」那一段（读成 X分0秒）  n=10  缺口 22.1 – 58.4 秒
        丢「分」整段（读数 <180 秒）   n=66  缺口 898.1 – 2330.4 秒

    58.4 秒到 898.1 秒之间**一条都没有**。阈值必须落在这段空白里：低于 58.4 秒
    会把「丢秒」那一族也判成大截断（它们其实已经被 1 分钟的容差吸收掉了），
    高于 898.1 秒会漏掉整个「丢分」族。
    """
    assert timedelta(seconds=58.4) < TRUNCATED_SEGMENT_SHORTFALL < timedelta(seconds=898.1)
    assert TRUNCATED_SEGMENT_SHORTFALL > BRIEFING_SKEW_TOLERANCE


def test_the_tolerance_is_the_one_the_repository_already_had() -> None:
    """容差沿用 `vision.parsers.BRIEFING_SKEW_TOLERANCE`，**不另造一个**。

    两处各调各的，就等于这道交叉校验在两条链路上说着不同的话。
    """
    from evo_helper.vision.parsers import BRIEFING_SKEW_TOLERANCE as FROM_VISION

    assert FROM_VISION is BRIEFING_SKEW_TOLERANCE

    inside = _reconcile(
        arrival=timedelta(minutes=30), duration=timedelta(minutes=30) - BRIEFING_SKEW_TOLERANCE
    )
    outside = _reconcile(
        arrival=timedelta(minutes=30),
        duration=timedelta(minutes=30) - BRIEFING_SKEW_TOLERANCE - timedelta(seconds=1),
    )

    assert inside.flight == timedelta(minutes=30)
    assert outside.flight is None


def test_the_model_breaks_the_tie_by_throwing_away_the_truncated_one() -> None:
    """两个读数打架时让公式当裁判——**只丢掉小得离谱的那个**。

    `225) 48秒` 那一族（真值 22 分 48 秒）会被读成 48 秒：量级小两三个数量级，
    而公式一眼就能认出来。
    """
    estimate = _reconcile(
        arrival=timedelta(minutes=22, seconds=48),
        duration=timedelta(seconds=48),
        model=timedelta(seconds=1370),
    )

    assert estimate.flight == timedelta(minutes=22, seconds=48)
    assert estimate.source is FlightSource.BRIEFING_ARRIVAL


def test_the_model_can_also_rule_against_the_arrival_time() -> None:
    """裁判不偏袒主来源：到达时间读错时同样丢掉它。

    同一张网格里 `3×/None` 就把 `09:26:27` 读成过 `03:26:27`——差六小时，
    而那会让「还要飞多久」直接变成一个负数或一个小得多的数。
    """
    estimate = _reconcile(
        arrival=timedelta(minutes=1),
        duration=timedelta(minutes=25, seconds=51),
        model=timedelta(seconds=1551),
    )

    assert estimate.flight == timedelta(minutes=25, seconds=51)
    assert estimate.source is FlightSource.BRIEFING_DURATION


# -- 方向：只拒小的，不拒大的 ------------------------------------------------


def test_a_reading_far_below_the_model_is_rejected_as_a_truncation() -> None:
    """比公式小得离谱 = 截断指纹，丢掉。

    `report_wait.parse_game_duration` 的 docstring 记着这条路径的指纹：
    生产库 197 发里 66 发落在 0–60 秒、最大值正好 59——59 是「秒」字段能装下的
    最大数。截断**只会把值变小**，所以这一侧才敢拒。
    """
    estimate = _reconcile(arrival=timedelta(seconds=9), model=timedelta(seconds=1628))

    assert estimate.source is FlightSource.DISTANCE_MODEL
    assert estimate.flight == timedelta(seconds=1628)


def test_a_reading_far_above_the_model_is_believed_not_rejected() -> None:
    """⚠️ **比公式大得多的读数必须采信，不许拦。**

    实机 2026-08-17：预设 BBB 那一整天 13 发的实测 ÷ 公式精确地挤在
    1.2580–1.2662。用户口径（2026-08-18）：「当时的 BBB 有其他舰艇 所以影响了
    参数」——**那 13 发是真的飞了那么久**。把它们当读错拦下就是误杀，
    而误杀的代价是回到 90 分钟空占。

    方向是不对称的、有依据的：解析截断只会把值变小，编组变慢只会把值变大。
    把 `MODEL_UNDERSHOOT_REJECT_RATIO` 那道关做成双边（`abs(...) > x%`），
    这条就红。
    """
    slow = timedelta(seconds=1801)
    estimate = _reconcile(arrival=slow, model=timedelta(seconds=1424))

    assert estimate.flight == slow
    assert estimate.source is FlightSource.BRIEFING_ARRIVAL


def test_the_reject_threshold_sits_below_every_healthy_reading() -> None:
    """0.95 这条线的依据：生产库回测里正常那一峰的下沿是 0.9931。

    低于 0.95 的全是可辨认的坏读数（9 发 `X分0秒`，比例 0.9497–0.9712——
    「秒」那一段被 OCR 丢掉，而 `_reads_the_whole_duration` 看不出来）。
    """
    assert 0.9497 < MODEL_UNDERSHOOT_REJECT_RATIO < 0.9931


# -- 单一来源 ---------------------------------------------------------------


def test_a_single_reading_is_believed_when_the_model_abstains() -> None:
    """只读到一个、公式又弃权时照样采信，但 `reason` 要说清只有单一来源。"""
    estimate = _reconcile(duration=timedelta(minutes=8, seconds=3))

    assert estimate.flight == timedelta(minutes=8, seconds=3)
    assert estimate.source is FlightSource.BRIEFING_DURATION
    assert "只有这一个来源" in estimate.reason


# -- 公式那一路 -------------------------------------------------------------


def test_the_model_fills_in_when_neither_line_can_be_read() -> None:
    """两个读数都读不出时用公式的值——**这一条就是那 23% 的出口**。

    读不出来的代价不是白跑一趟，是那一发按 `report_wait.UNKNOWN_LINE_HOLD`
    （90 分钟）占着航线，而实测往返只有约 46 分钟。
    """
    estimate = _reconcile(model=timedelta(seconds=1551))

    assert estimate.flight == timedelta(seconds=1551)
    assert estimate.source is FlightSource.DISTANCE_MODEL


def test_a_computed_value_never_passes_itself_off_as_a_measured_one() -> None:
    """⚠️ **算出来的数不许长得像量出来的。**

    本仓硬规矩（`docs/预计战报时间-估算方案.md` 第 2 条、`storage.models` 里
    `target_military_score_estimated` 那一段）。把 `DISTANCE_MODEL` 也算进
    `is_measured`，这条就红——而真实后果是：`flight_seconds` 那一列
    （`vet_flight_time` 那道下限赖以标定的样本池）会被公式自己的输出污染，
    下一次标定就成了拿模型的输出去标定模型。
    """
    computed = _reconcile(model=timedelta(seconds=1551))
    measured = _reconcile(arrival=timedelta(seconds=1551))

    assert computed.is_measured is False
    assert measured.is_measured is True


def test_nothing_at_all_is_not_a_reason_to_refuse_the_dispatch() -> None:
    """三个来源全空时给出 `flight=None`，**而不是抛异常或拦下这一发**。

    飞行时间只是闹钟，不是闸门（`tools.pirate_loop._read_flight_time`）。
    这条链路已经因为「ROI 与放大倍数不配」白白拦下过四发完全正常的攻击。
    """
    estimate = _reconcile()

    assert estimate.flight is None
    assert estimate.source is None


# -- 公式的适用域 -----------------------------------------------------------


def test_the_coefficient_is_learned_per_origin_planet_not_shared_globally() -> None:
    """⚠️ **系数按出发星球各学一个，不许全局共用。**

    用户口径（2026-08-19）：**「每个球的速度都会有点不一样的」**。生产库回测
    （跨银河那一档）反解 `单程秒 = 2 + k·√D`：

        4:277:15  n=56  k = 26.5165
        9:250:8   n=19  k = 26.3327

    把两颗星球的样本混在一起学、或者退回 `flight_time.SECONDS_PER_ROOT_UNIT`
    那个全局常数，这条就红：9:250:8 那一档会差 0.7%（3752 对 3726）。
    """
    home = _fit(_samples(ORIGIN, 26.5165, 5))
    second = _fit(_samples(SECOND, 26.3327, 5), origin=SECOND)

    assert home is not None and second is not None
    assert round(home.seconds_per_root_unit, 4) == 26.5165
    assert round(second.seconds_per_root_unit, 4) == 26.3327

    # 别的星球的样本一发都不许进来：混在一起学，两颗星球都不准。
    mixed = _fit(_samples(SECOND, 26.3327, 5) + _samples(ORIGIN, 26.5165, 5))
    assert mixed is not None
    assert round(mixed.seconds_per_root_unit, 4) == 26.5165


def test_the_learned_coefficient_predicts_the_dispatch_that_broke_it() -> None:
    """⚠️ **本次修法的第一个落点：那三发本来是送分题。**

    实机 2026-08-19 从 9:250:8 打 1:338:14（跨银河），三个来源分别是
    到达时间 3725 秒、飞行时间 126 秒、公式**弃权**——因为上一版拿
    「屏幕上的速度逐字等于 14.520」当准入闸，而那颗星球读到的是 `14.720`。

    用这颗星球自己学出来的 k 一算就是 3726 秒：三方对照下飞行时间是唯一的
    离群值（差 29 倍），一眼就该丢掉。
    """
    coefficient = _fit(_samples(SECOND, 26.3327, 5), origin=SECOND)
    model = predict_flight(Coordinate(1, 338, 14), SECOND, coefficient=coefficient)

    assert model is not None
    assert abs(model - timedelta(seconds=3726)) <= timedelta(seconds=2)

    estimate = _reconcile(
        arrival=timedelta(seconds=3725), duration=timedelta(seconds=126), model=model
    )
    assert estimate.flight == timedelta(seconds=3725)
    assert estimate.source is FlightSource.BRIEFING_ARRIVAL


def test_the_model_abstains_until_it_has_learned_this_planets_coefficient() -> None:
    """没学出系数就弃权，**不许拿全局那个常数顶上**。

    那个 26.5165 是 4:277:15 的属性。顶上去的代价：用到 9:250:8 上差 0.7%，
    用到换了编组的那一天（2026-08-17）上差 26%——而 08-17 那次正是航线提前
    26% 放出来、调度器以为空了就派、撞上「同时派遣的舰队数量已达上限。」。
    """
    assert predict_flight(Coordinate(5, 279, 14), ORIGIN, coefficient=None) is None


def test_too_few_samples_is_not_a_coefficient() -> None:
    """样本不足就弃权，**不给任何默认比例**。

    门限挡的不是精度（回测里 1 发就能预测到 0.00%），是**单点被污染**：
    `flight_seconds` 那一列里混着 OCR 截断的残骸，而中位数要「坏样本不过半」
    才免疫。3 是能容忍一个坏样本的最小奇数。
    """
    assert MIN_LEARNING_SAMPLES >= 3
    assert _fit(_samples(ORIGIN, 26.5165, MIN_LEARNING_SAMPLES - 1)) is None
    assert _fit(_samples(ORIGIN, 26.5165, MIN_LEARNING_SAMPLES)) is not None


def test_one_truncated_sample_does_not_move_the_coefficient() -> None:
    """⚠️ **中位数，不是平均数。**

    `flight_seconds` 那一列里混着 OCR 截断的残骸（生产库里 66 发只剩秒段、
    9 发 `X分0秒`）。这正是「拿历史当样本」这条路早先被否掉的那个理由——
    中位数是它的解药。改成 `mean()`，这条就红。
    """
    poisoned = _samples(ORIGIN, 26.5165, 4)
    poisoned.append(replace(poisoned[0], flight_seconds=20.0))

    learned = _fit(poisoned)

    assert learned is not None
    assert round(learned.seconds_per_root_unit, 4) == 26.5165


def test_samples_flown_at_another_speed_do_not_count() -> None:
    """⚠️ **速度是「编组变了」的探测器**——速度一变，旧样本立刻不算数。

    这是「要等好几发同向偏离才敢重新学」那条否决理由的答案：屏幕上那个数
    第一发就变了。`preset_name` 与 `preset_signature` 都抓不住那次变化，
    2026-08-17 那天 13 发慢了 26% 就是这么错过去的。

    把这一条筛选去掉，这条就红。
    """
    stale = [replace(sample, fleet_speed="11.480") for sample in _samples(ORIGIN, 33.5400, 5)]
    fresh = [replace(sample, fleet_speed="14.520") for sample in _samples(ORIGIN, 26.5165, 3)]

    assert _fit(stale, fleet_speed="14.520") is None

    learned = _fit(stale + fresh, fleet_speed="14.520")
    assert learned is not None
    assert round(learned.seconds_per_root_unit, 4) == 26.5165


def test_samples_with_no_recorded_speed_still_count() -> None:
    """⚠️ **「没记过速度」不等于「速度不一样」。**

    `fleet_speed_raw` 那一列 2026-08-19 才加，在它之前的样本一律为 NULL——
    把 NULL 也当成「对不上」剔掉，冷启动那天一颗星球都学不出系数，
    而那正是这次要修的东西。这些样本靠 `LEARNING_WINDOW` 随时间老去。
    """
    legacy = _samples(ORIGIN, 26.5165, 5)

    assert all(sample.fleet_speed is None for sample in legacy)
    assert _fit(legacy, fleet_speed="14.720") is not None


def test_the_speed_is_never_used_as_a_conversion_factor() -> None:
    """⚠️ **速度只做是非题，不参与算术。**

    实测：9:250:8 的 k ÷ 4:277:15 的 k = 0.9931，而速度比
    14.520 / 14.720 = 0.98641——**差 0.7%，比不用它还糟**。谁要写
    `14.520 / speed` 那个乘法，这条就是拦他的。
    """
    assert 26.3327 / 26.5165 == pytest.approx(0.99307, abs=1e-5)
    assert 14.520 / 14.720 == pytest.approx(0.98641, abs=1e-5)

    learned = _fit(_samples(SECOND, 26.3327, 5), origin=SECOND, fleet_speed="14.720")

    assert learned is not None
    assert round(learned.seconds_per_root_unit, 4) == 26.3327
    assert learned.seconds_per_root_unit != pytest.approx(26.5165 * (14.520 / 14.720), abs=1e-3)


def test_the_coefficient_carries_how_many_samples_it_was_learned_from() -> None:
    """⚠️ **样本数必须跟着系数一起走。**

    k 是拟合参数不是标定常量。出事时「公式为什么给出这个数」要答得上来，
    而那句话必须包含「哪颗星球的 k、基于几发」——CLAUDE.md 那条判据是
    「出事时能不能只靠库里的日志定位」。
    """
    learned = _fit(_samples(ORIGIN, 26.5165, 7))

    assert learned is not None
    assert learned.samples == 7


def test_only_the_most_recent_window_of_samples_is_learned_from() -> None:
    """只学最近 `LEARNING_WINDOW` 发——**而且是筛完之后才截**。

    先截就可能一发跨银河样本都剩不下（2:137:18 最近 20 发几乎全是同银河），
    于是那颗星球永远学不出系数。样本按从新到旧传进来。
    """
    fresh = _samples(ORIGIN, 26.3327, LEARNING_WINDOW)
    ancient = _samples(ORIGIN, 33.5400, 50)

    learned = _fit(fresh + ancient)

    assert learned is not None
    assert learned.samples == LEARNING_WINDOW
    assert round(learned.seconds_per_root_unit, 4) == 26.3327


def test_scout_samples_never_calibrate_an_attack() -> None:
    """侦察艇快约 40 倍（回测里 k 是 0.35–0.64，攻击是 26.5）。

    混进来就是数量级错位。把发次那一条筛选去掉，这条就红。
    """
    probes = [
        replace(sample, mission_kind=MISSION_KIND_SCOUT) for sample in _samples(ORIGIN, 0.6368, 9)
    ]
    attacks = _samples(ORIGIN, 26.5165, 3)

    assert _fit(probes) is None

    learned = _fit(probes + attacks)
    assert learned is not None
    assert round(learned.seconds_per_root_unit, 4) == 26.5165


# -- 只学跨银河那一档 --------------------------------------------------------


def test_nothing_inside_one_galaxy_is_learned_from_or_predicted() -> None:
    """⚠️ **同银河与同恒星系那两档一律弃权——学也不学，裁也不裁。**

    同一批回测（2026-08-19）：

        ATTACK 2:137:18 同银河  n=119  预测误差中位 28.3%  p90 33.0%
        ATTACK 2:137:18 同系    n=35   预测误差中位 2.24%  最大 44.4%

        9:250:8  → 9:250:16     实测 600s   公式 906s
        4:277:15 → 4:277:14     实测 616s   公式 906s
        2:137:18 → 2:137:1/3/4  实测 480s   公式 906s

    根子在 `distance_units` 自己：恒星系环距为 0 时它取固定的
    `SAME_GALAXY_BASE_UNITS = 1162`，反解出来只有约 520；同一档里实测还互不
    相同（480 / 600 / 616），说明行星位次也进算式，而公式里没有这一维。
    **换一个 k 救不了一个形状本身就错的 D。**

    把同银河那一档放进来（不管是拿去学，还是拿学出来的 k 去裁），这条就红：
    那一档每一发读出来的真值都会被当成「远小于公式」的截断指纹丢掉。
    """
    inside = [
        FlightSample(target=FAR, origin=ORIGIN, flight_seconds=1551.0, mission_kind=KIND),
        FlightSample(target=FAR, origin=ORIGIN, flight_seconds=1551.0, mission_kind=KIND),
        FlightSample(target=SAME_SYSTEM, origin=ORIGIN, flight_seconds=616.0, mission_kind=KIND),
        FlightSample(target=SAME_SYSTEM, origin=ORIGIN, flight_seconds=616.0, mission_kind=KIND),
    ]

    assert _fit(inside) is None

    learned = _fit(_samples(ORIGIN, 26.5165, 5))
    assert predict_flight(FAR, ORIGIN, coefficient=learned) is None
    assert predict_flight(SAME_SYSTEM, ORIGIN, coefficient=learned) is None

    honest = _reconcile(
        arrival=timedelta(seconds=616),
        model=predict_flight(SAME_SYSTEM, ORIGIN, coefficient=learned),
    )
    assert honest.flight == timedelta(seconds=616)
    assert honest.source is FlightSource.BRIEFING_ARRIVAL


def test_the_model_matches_the_measured_flights_it_was_learned_from() -> None:
    """公式在它的适用域里确实对得上——不然上面那些判据都是空谈。

    样本点取自生产库（只读回测，2026-08-19，出发星 4:277:15）：跨一个银河的
    目标实测 3752 秒，n=56 学出来的 k 是 26.5165。
    """
    learned = _fit(_samples(ORIGIN, 26.5165, 5))
    cross_galaxy = predict_flight(Coordinate(5, 279, 14), ORIGIN, coefficient=learned)

    assert cross_galaxy is not None
    assert abs(cross_galaxy - timedelta(seconds=3752)) <= timedelta(seconds=2)


# -- 航线兜底占用（三个来源全空时） ------------------------------------------


def test_the_line_hold_covers_the_real_round_trip_of_the_dispatch_that_broke_it() -> None:
    """⚠️ **本次修法的第二个落点。**

    实机 2026-08-19 从 9:250:8 打 8:486:12，单程 3726 秒、往返 **124.2 分钟**，
    而兜底占用是与目标无关的常数 90 分钟：

        派出后第 90 分钟  →  航线被判为空出来了
        实际第 124 分钟   →  舰队才回港
        中间那 34 分钟    →  调度器与首页都以为有空闲航线，而实际没有

    用户看到的「星球 2 在等航线」就是这么来的。把这里换回常数、或者把系数
    调到 1 以下，这条就红。
    """
    hold = line_hold_round_trip(Coordinate(8, 486, 12), Coordinate(9, 250, 8))

    assert hold is not None
    assert hold > timedelta(minutes=124.2)


def test_the_line_hold_never_underestimates_the_slowest_fleet_on_record() -> None:
    """⚠️ **宁可高估不要低估。**

    高估只是晚一点把航线放出来（少派几发）；低估会让调度器以为有航线、派出去
    撞游戏的「同时派遣的舰队数量已达上限。」，白跑一整轮
    （`repository.count_inflight` 的注释里写过同一条取舍）。

    生产库回测（2026-08-19，跨恒星系攻击 n=355）里**实测÷公式**的上端是
    1.2662（2026-08-17 那批混编的 BBB，用户口径「当时的 BBB 有其他舰艇
    所以影响了参数」）。系数必须盖得住它。

    把 `LINE_HOLD_SAFETY_FACTOR` 调到 1.2662 以下（含调成 1.0 那种「就用公式
    本身」），这条就红。
    """
    assert LINE_HOLD_SAFETY_FACTOR > 1.2662

    slowest_on_record = 2 * one_way_seconds(CROSS_GALAXY, ORIGIN) * 1.2662
    hold = line_hold_round_trip(CROSS_GALAXY, ORIGIN)

    assert hold is not None
    assert hold.total_seconds() >= slowest_on_record


def test_the_line_hold_abstains_inside_one_galaxy() -> None:
    """同一个银河之内那两档**弃权**，调用方回落到那个常数。

    理由与 `predict_flight` 同一条：公式在那两档是 known-wrong 的
    （同银河预测误差中位数到 28%；同恒星系的 `1162` 反推只有约 520，实测
    480/600/616 还互不相同）。而它们算出来的往返都在 52 分钟以内，本来就压在
    90 分钟的默认值底下、取大之后一步都动不了——弃权不损失任何东西，却省下一个
    日后有人把默认值调小时会突然生效的错值。
    """
    assert line_hold_round_trip(SAME_SYSTEM, ORIGIN) is None
    assert line_hold_round_trip(FAR, ORIGIN) is None


def test_the_line_hold_does_not_wait_until_a_coefficient_has_been_learned() -> None:
    """⚠️ **兜底这一路不吃系数，而 `predict_flight` 吃——这是刻意的分歧。**

    `predict_flight` 要给出一个**能落库当飞行时长**的值，所以没学出这颗星球的
    系数就宁可弃权。这里只回答「航线还占着吗」，而调用方拿它与
    `report_wait.UNKNOWN_LINE_HOLD`（或用户填的那个数）**取大**，于是它只能把
    占用拉长、永远不会缩短；`LINE_HOLD_SAFETY_FACTOR` 那 1.3 早就盖住了
    「用别的星球的系数」那 0.7% 偏差。

    反过来说，要求它先学出系数就正好复刻了 2026-08-19 那次故障的形状：公式因为
    适用域闸对整颗星球一次都不生效，于是兜底永远退回那个常数。
    """
    target, origin = Coordinate(8, 486, 12), Coordinate(9, 250, 8)

    assert predict_flight(target, origin, coefficient=None) is None
    assert line_hold_round_trip(target, origin) is not None


# -- 解析那一侧不许放宽 ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["225) 48秒", "245} 15秒", "265) 41%", "285) 48秒", "15分 20%)"],
)
def test_the_duration_parser_still_refuses_every_garbled_line(text: str) -> None:
    """⚠️ **修法在 OCR 那一侧，不在解析那一侧。**

    这些是生产 `system_log` 里的原始读数（`分` 被读成 `5)` / `5}`，
    `秒` 被读成 `%` / `%)`）。放宽 `parse_game_duration` 让它们过去，
    `225) 48秒` 会被读成 **48 秒**（真值 22 分 48 秒）——一个看起来完全合理、
    只是小了两三个数量级的值。生产库 197 发里 66 发落在 0–60 秒、最大值正好
    59，就是那条路径的指纹。
    """
    from evo_helper.domain.report_wait import parse_game_duration

    assert parse_game_duration(text) is None
