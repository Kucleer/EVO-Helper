"""三个来源怎么合成一个飞行时长，以及**每一处判据挡掉了什么**。

背景：到 2026-08-18 为止只有「简报页飞行时间那一行」一个来源，实机 24 小时
62 发里读不出 14 发（23%），每次白占约 44 分钟航线。判据本身与像素无关，
所以全在这里用假读数验；「像素上到底读不读得出」由
`tests/integration/vision/test_briefing_arrival_live.py` 拿实拍守。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from evo_helper.domain.flight_estimate import (
    BRIEFING_SKEW_TOLERANCE,
    CALIBRATED_FLEET_SPEED,
    MODEL_UNDERSHOOT_REJECT_RATIO,
    FlightSource,
    fleet_matches_calibration,
    predict_flight,
    reconcile_flight,
)
from evo_helper.domain.models import Coordinate

ORIGIN = Coordinate(4, 277, 15)
#: 隔壁恒星系。公式在这一档上生产库回测 n=106、中位误差 0.09%。
FAR = Coordinate(4, 206, 12)
#: **同一个恒星系**里的另一颗行星。公式在这一档上是错的，见下面那条豁免。
SAME_SYSTEM = Coordinate(4, 277, 14)


def _reconcile(arrival=None, duration=None, model=None):  # type: ignore[no-untyped-def]
    return reconcile_flight(arrival_flight=arrival, duration_flight=duration, model_flight=model)


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
    """⚠️ **两个来源打架而公式裁不了时，一个都不许采信。**

    挑一个信是最坏的选择：错值同时污染两个钟（战报到点时刻 + 航线空出时刻）
    且一声不响，而 None 只是多白跑一趟。这与
    `domain.report_wait.parse_game_duration`「部分匹配一律失败」是同一条道理。

    把这里改成「随便取一个」或「取小的那个」，这条就红。
    """
    estimate = _reconcile(arrival=timedelta(minutes=30), duration=timedelta(minutes=8))

    assert estimate.flight is None
    assert estimate.source is None


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


def test_the_model_abstains_when_the_fleet_is_not_the_calibrated_one() -> None:
    """⚠️ **屏幕上的速度不是标定那一组，公式就弃权。**

    `domain.flight_time.SECONDS_PER_ROOT_UNIT = 26.5165` 里裹着舰速，只在
    「速度 14.520 / 100%」那一套编组上标过。用户口径（2026-08-18）：
    「当时的 BBB 有其他舰艇 所以影响了参数」——编组是用户随时会改的东西，
    而 `preset_name` 与 `preset_signature` **都抓不住这次变化**。

    改成「读不到就当作是标定那一组」（也就是默认 ratio = 1.0），这条就红：
    08-17 那天公式会低估 26%，航线提前 26% 放出来、调度器以为空了就派，
    撞上游戏那句「同时派遣的舰队数量已达上限。」，白跑一轮。
    """
    assert predict_flight(FAR, ORIGIN, speed="11.480", percent="100%") is None
    assert predict_flight(FAR, ORIGIN, speed=None, percent=None) is None
    assert predict_flight(FAR, ORIGIN, speed=CALIBRATED_FLEET_SPEED, percent="50%") is None

    assert predict_flight(FAR, ORIGIN, speed=CALIBRATED_FLEET_SPEED, percent="100%") is not None


def test_the_speed_is_compared_character_by_character() -> None:
    """逐字比，不做任何归一化。

    这两个值是 OCR 的产物；任何「差不多」的放宽都会把一次误读放行成一次
    「还在适用域内」的误判，而那正是这道判据要挡的。同
    `game.system_navigator._reads_as`。首尾空白除外——那是取字函数自己带的。
    """
    assert fleet_matches_calibration("  14.520  ", " 100% ") is True
    assert fleet_matches_calibration("14.52", "100%") is False
    assert fleet_matches_calibration("14.520", "100") is False


def test_the_model_abstains_inside_a_single_star_system() -> None:
    """⚠️ **同一个恒星系内那一档必须豁免。**

    `distance_units` 在恒星系环距为 0 时取固定的 `SAME_GALAXY_BASE_UNITS = 1162`，
    而生产库（只读回测，2026-08-18）反推出来只有约 520：

        9:250:8  → 9:250:16     实测 600s   公式 906s
        4:277:15 → 4:277:14     实测 616s   公式 906s
        2:137:18 → 2:137:1/3/4  实测 480s   公式 906s

    更要命的是**同一档里实测还不一样**（480 / 600 / 616），说明行星位次也进
    算式，而公式里压根没有这一维。7 个点标不出一个新常数。

    不豁免的话，这一档每一发读出来的真值（比例 0.53–0.68）都会被当成
    「远小于公式」的截断指纹丢掉，然后拿一个偏大 47% 的公式值顶上去——
    航线白占 300 秒，而这一档本来是读得最准的。
    """
    assert predict_flight(SAME_SYSTEM, ORIGIN, speed=CALIBRATED_FLEET_SPEED, percent="100%") is None

    honest = _reconcile(
        arrival=timedelta(seconds=616),
        model=predict_flight(SAME_SYSTEM, ORIGIN, speed=CALIBRATED_FLEET_SPEED, percent="100%"),
    )
    assert honest.flight == timedelta(seconds=616)
    assert honest.source is FlightSource.BRIEFING_ARRIVAL


def test_the_model_matches_the_measured_flights_it_was_calibrated_against() -> None:
    """公式在它的适用域里确实对得上——不然上面那些判据都是空谈。

    两个样本点取自生产库（只读回测，2026-08-18，预设 BBB，出发星 4:277:15）：
    `4:206:12` 环距 71 实测 1551 秒、`5:279:14` 跨一个银河实测 3752 秒。
    """
    same_galaxy = predict_flight(FAR, ORIGIN, speed=CALIBRATED_FLEET_SPEED, percent="100%")
    cross_galaxy = predict_flight(
        Coordinate(5, 279, 14), ORIGIN, speed=CALIBRATED_FLEET_SPEED, percent="100%"
    )

    assert same_galaxy is not None and cross_galaxy is not None
    assert abs(same_galaxy - timedelta(seconds=1551)) <= timedelta(seconds=2)
    assert abs(cross_galaxy - timedelta(seconds=3752)) <= timedelta(seconds=2)


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
