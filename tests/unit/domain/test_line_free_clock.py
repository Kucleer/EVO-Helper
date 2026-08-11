"""航线什么时候空出来——派出之后的**第二个钟**。

第一个钟是 `expected_report_at_utc`（出发 + 飞行时长 × 1，战报在抵达时产生），
第二个钟才是航线。用错一个就白飞一趟舰队：调度器以为航线空了就去派，
撞上游戏的「同时派遣的舰队数量已达上限。」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.bot_round import PROBE_PRESET_NAME
from evo_helper.domain.records import MISSION_KIND_ATTACK, MISSION_KIND_SCOUT
from evo_helper.domain.report_wait import MAX_REPORT_AGE, UNKNOWN_LINE_HOLD, line_free_at

DISPATCHED = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
FLIGHT = timedelta(minutes=30)


def test_an_attack_holds_the_line_for_the_round_trip() -> None:
    """打完还要飞回来，所以是 2×。"""
    assert (
        line_free_at(DISPATCHED, FLIGHT, mission_kind=MISSION_KIND_ATTACK, preset_name="AAA")
        == DISPATCHED + FLIGHT * 2
    )


def test_a_probe_is_one_way() -> None:
    """探路舰队会在攻击中损失，没有返程——1× 就该释放。

    按 2× 算的后果不是撞弹窗，是反过来：航线明明空着，调度器却以为还占着，
    于是不派。那一侧没有闸门兜底（闸门只拦「派不出去」，拦不住「不去派」）。
    """
    assert (
        line_free_at(
            DISPATCHED, FLIGHT, mission_kind=MISSION_KIND_ATTACK, preset_name=PROBE_PRESET_NAME
        )
        == DISPATCHED + FLIGHT
    )


def test_a_scout_flies_home() -> None:
    """侦察探测器会飞回来，2×。

    判据是 `mission_kind`，不是预设名——侦察根本不选预设。
    """
    assert (
        line_free_at(DISPATCHED, FLIGHT, mission_kind=MISSION_KIND_SCOUT, preset_name="侦察")
        == DISPATCHED + FLIGHT * 2
    )


def test_a_scout_is_never_mistaken_for_a_probe() -> None:
    """哪怕侦察发的预设名恰好写成了探路，它仍然要飞回来。

    `mission_kind` 先判：探路的 1× 是「舰队会损失」这个事实推出来的，
    而侦察发不会损失。两条判据撞在一起时，认发次类型的那条说了算。
    """
    assert (
        line_free_at(
            DISPATCHED,
            FLIGHT,
            mission_kind=MISSION_KIND_SCOUT,
            preset_name=PROBE_PRESET_NAME,
        )
        == DISPATCHED + FLIGHT * 2
    )


def test_an_unknown_flight_time_leaves_the_clock_unset() -> None:
    """读不到飞行时长就没有钟可言。

    **None 不表示「它没占航线」**：被游戏接受的那一发舰队一定占着一条位子。
    那一档改按 `UNKNOWN_LINE_HOLD` 从派出时刻起算（见
    `repository._still_holding_a_line`），这里只是说「算不出确切时刻」。
    """
    assert (
        line_free_at(DISPATCHED, None, mission_kind=MISSION_KIND_ATTACK, preset_name="AAA") is None
    )


def test_the_unknown_hold_is_not_the_report_age_ceiling() -> None:
    """**这两个常量必须分开，同值就是一次读不到换一次停摆。**

    `MAX_REPORT_AGE`（6 小时）是「等一封战报等到什么时候死心」的上界，和
    `pirate_loop.MAX_CREDIBLE_FLIGHT` 一样是**离谱值的天花板**；而这里问的是
    「一支不知道何时回来的舰队该占多久航线」，那是个往返时长。这两条链路打的
    是同系目标：生产库 236 条有航线钟的派遣，实际占用中位数 48 秒、最长 62.6
    分钟。

    实机 2026-08-11：08:48–10:07 之间 6 发 bot 攻击都没读到飞行时间，正好等于
    `fleet_line_limit`，于是 `free_lines` 从 10:07 起恒为 0，直到第一发满 6 小时
    （14:48）才松一格——而那 6 支舰队 11:10 前就全回来了。中间三个多小时里两条
    攻击链路一齐显示「等航线」，调度器「空转中」。

    上界断言是有牙的那一半：把它调回 6 小时（或任何 ≥2 小时的值），下面这条
    就会红。
    """
    assert UNKNOWN_LINE_HOLD < MAX_REPORT_AGE
    # 覆盖得住实测最长往返（62.6 分钟），又短到一次读不到不至于压死一条航线半天。
    assert timedelta(minutes=63) < UNKNOWN_LINE_HOLD <= timedelta(hours=2)
