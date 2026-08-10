"""航线什么时候空出来——派出之后的**第二个钟**。

第一个钟是 `expected_report_at_utc`（出发 + 飞行时长 × 1，战报在抵达时产生），
第二个钟才是航线。用错一个就白飞一趟舰队：调度器以为航线空了就去派，
撞上游戏的「同时派遣的舰队数量已达上限。」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.bot_round import PROBE_PRESET_NAME
from evo_helper.domain.records import MISSION_KIND_ATTACK, MISSION_KIND_SCOUT
from evo_helper.domain.report_wait import line_free_at

DISPATCHED = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
FLIGHT = timedelta(minutes=30)


def test_an_attack_holds_the_line_for_the_round_trip() -> None:
    """打完还要飞回来，所以是 2×。"""
    assert (
        line_free_at(
            DISPATCHED, FLIGHT, mission_kind=MISSION_KIND_ATTACK, preset_name="AAA"
        )
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
        line_free_at(
            DISPATCHED, FLIGHT, mission_kind=MISSION_KIND_SCOUT, preset_name="侦察"
        )
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

    NULL 不计入在飞数——宁可估高空闲航线：估高了 runner 起来空跑一轮，
    有 `LineCapacityGate` 兜底；估低则是航线空着不派，没人兜。
    """
    assert (
        line_free_at(DISPATCHED, None, mission_kind=MISSION_KIND_ATTACK, preset_name="AAA")
        is None
    )
