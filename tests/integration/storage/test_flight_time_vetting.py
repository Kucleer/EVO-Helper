"""飞行时长的下限关设在**写入这一处**，因为这里绕不过去。

`record_flight_time` 是三列（`flight_seconds` / `expected_report_at_utc` /
`line_free_at_utc`）唯一的出口，而 `row.mission_kind` 就在手边。搁在调用方就等于
每新增一条派遣路径都要记得再关一次门，漏关的后果是**一个错值同时污染两个钟**，
而且一声不响。

判据本身在 `domain.report_wait.vet_flight_time`，取值理由在
`MIN_CREDIBLE_ATTACK_FLIGHT`：生产库 2026-08-13 的 209 发攻击里，66 发落在
0–59 秒，而 59 正好是一个「秒」字段能装下的最大数——那是解析截断留下的残骸，
不是任何物理量。真值那一簇最小 300 秒，中间 60–300 秒**一发都没有**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)

AT = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
ORIGIN = Coordinate(2, 137, 18)
#: 同一个恒星系里的另一颗星球。
NEAR = Coordinate(2, 137, 2)
#: 隔壁恒星系。
TARGET = Coordinate(2, 138, 2)

#: 实机上真实出现过的截断值：`2:55:9` 那一发读作 9 秒（真值远不止）。
TRUNCATED = timedelta(seconds=9)
#: **同星系**那一趟，也就是攻击飞行时间出现过的最低值（用户口径 2026-08-13）。
CREDIBLE = timedelta(minutes=5)
#: 跨恒星系的一趟真值。用户实拍：跨 50 个恒星系是 23 分 13 秒。
FAR_FLIGHT = timedelta(minutes=23, seconds=13)


def _dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id: UUID,
    *,
    flight: timedelta | None,
    mission: str = MISSION_KIND_ATTACK,
    target: Coordinate = TARGET,
    source: object = None,
) -> UUID:
    intent_id, dispatch_id = uuid4(), uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=ORIGIN,
            target=target,
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=AT,
            created_at_utc=AT,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=AT,
            accepted=True,
            mission_kind=mission,
        )
    )
    repository.record_flight_time(dispatch_id, flight, AT, source=source)
    return dispatch_id


def _row(repository, dispatch_id: UUID):  # type: ignore[no-untyped-def]
    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001 - 这条断言要看三列原值
        return session.get(orm.AttackDispatchRow, dispatch_id)


def test_an_attack_flight_under_the_floor_lands_as_null(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """9 秒的攻击飞行 → 三列全空，而不是存下一个小而合理的错值。

    **三列一起断言**，因为这正是这个缺陷贵的地方：同一个错值派生两个钟，
    一个让战报一产生就被判「到点了」、赖在到期单子上白烧开封预算，
    另一个让调度器以为航线十几秒后就空出来、接着派、撞上游戏的舰队数上限。
    """
    row = _row(repository, _dispatch(repository, run_id, flight=TRUNCATED))

    assert row.flight_seconds is None
    assert row.expected_report_at_utc is None
    assert row.line_free_at_utc is None


def test_the_fastest_credible_attack_is_stored_as_read(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """同星系那一趟 5 分钟——出现过的最低值——必须原样存下。

    没有这条对照，「一律返回 None」也能让上面那条变绿。
    """
    row = _row(repository, _dispatch(repository, run_id, flight=CREDIBLE, target=NEAR))

    assert row.flight_seconds == int(CREDIBLE.total_seconds())
    assert row.expected_report_at_utc == AT + CREDIBLE
    # 攻击是 ×2：打完还要飞回来。
    assert row.line_free_at_utc == AT + CREDIBLE * 2


def test_a_real_cross_system_flight_is_stored_as_read(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """跨恒星系的真值（用户实拍：跨 50 系 = 23 分 13 秒）照常存下。

    跨系那道门槛只是「不能比同星系还快」，不是把跨系的都拒掉。
    """
    row = _row(repository, _dispatch(repository, run_id, flight=FAR_FLIGHT))

    assert row.flight_seconds == int(FAR_FLIGHT.total_seconds())


def test_a_cross_system_flight_no_faster_than_the_same_system_one_is_rejected(  # type: ignore[no-untyped-def]
    repository, run_id
) -> None:
    """**更远不可能更快。** 跨了恒星系却只用同星系那个最低值 → 判为没读出来。

    实机三发（生产库 2026-08-13）：探路 → `2:320:11`，出发 `2:137:18`，跨 183 个
    恒星系，飞行读作 300 秒。而用户对同一条路线实测是 **25 分 1 秒**，库里同预设
    在 184–186 系也正好是 1501–1505 秒。三次同一个目标、同一个错值，是那份简报上
    **可重复**的失手。

    ⚠️ **这一档 `parse_game_duration` 的读全校验永远看不出来。** 错法是段内部的
    数字丢了（`25分1秒` 的 `2` 没了 → `5分1秒`），剩下的字符串完全合法、没有任何
    残骸。而 300 秒在 3 分钟那道绝对下限那里也稳稳过关。**只有拿距离才拦得住。**
    """
    row = _row(repository, _dispatch(repository, run_id, flight=CREDIBLE))

    assert row.flight_seconds is None
    assert row.expected_report_at_utc is None
    assert row.line_free_at_utc is None


def test_the_floor_does_not_reach_scout_dispatches(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """同样是 9 秒，侦察发原样放行。

    ⚠️ **这条守的是「别把攻击的下限推广到所有发次」，不是「侦察那些值是对的」。**
    用户口径（2026-08-13）：侦查本来就是秒级，基本不上分钟级。生产库里 371 发
    侦察落在 14–135 秒；那批数字里有多少本身也是截断产物（135 = 2 分 15 秒、
    121 = 2 分 1 秒，都是「分+秒」两段的形状，而飞最久的偏偏打的是主星系内
    最近的目标）无从判断——所以侦察这一侧**量不出下限，也就不设**，
    靠 `parse_game_duration` 那道读全校验防。
    """
    dispatch_id = _dispatch(repository, run_id, flight=TRUNCATED, mission=MISSION_KIND_SCOUT)
    row = _row(repository, dispatch_id)

    assert row.flight_seconds == int(TRUNCATED.total_seconds())
    assert row.expected_report_at_utc == AT + TRUNCATED


def test_a_flight_that_was_never_read_still_lands_as_null(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """读不出来那一档的行为不变：三列留空，交给 NULL 那条既定降级。"""
    row = _row(repository, _dispatch(repository, run_id, flight=None))

    assert row.flight_seconds is None
    assert row.expected_report_at_utc is None
    assert row.line_free_at_utc is None


# -- 这个数是读出来的还是算出来的 -------------------------------------------


def test_a_computed_flight_sets_the_clocks_but_never_flight_seconds(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **`domain.flight_time` 公式算出来的值不许进 `flight_seconds`。**

    两个钟照常按它推——省下的正是那 90 分钟空占（`UNKNOWN_LINE_HOLD`）——但
    「这一发飞了多久」这个**观测记录**仍然留 NULL，来源写在 `flight_source` 上。

    理由不是洁癖：`flight_seconds` 正是 `vet_flight_time` 那道下限赖以标定的
    样本池（`MIN_CREDIBLE_ATTACK_FLIGHT` 的取值就是从这一列的分布量出来的）。
    掺进 `domain.flight_time` 自己的输出，下一次标定就成了拿模型的输出去标定
    模型。`docs/预计战报时间-估算方案.md` 第 2 条早就写着这一条。

    把 `record_flight_time` 里那个三元改成无条件写值，这条就红。
    """
    from evo_helper.domain.flight_estimate import FlightSource

    row = _row(
        repository,
        _dispatch(repository, run_id, flight=FAR_FLIGHT, source=FlightSource.DISTANCE_MODEL),
    )

    assert row.flight_seconds is None, "算出来的值混进了标定样本池"
    assert row.expected_report_at_utc == AT + FAR_FLIGHT
    assert row.line_free_at_utc == AT + FAR_FLIGHT * 2
    assert row.flight_source == "distance_model"


def test_a_measured_flight_records_which_line_it_was_read_from(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """读出来的那两种照常写 `flight_seconds`，并记下是哪一行读的。

    没有这条对照，「一律不写 `flight_seconds`」也能让上面那条变绿。
    """
    from evo_helper.domain.flight_estimate import FlightSource

    row = _row(
        repository,
        _dispatch(repository, run_id, flight=FAR_FLIGHT, source=FlightSource.BRIEFING_ARRIVAL),
    )

    assert row.flight_seconds == int(FAR_FLIGHT.total_seconds())
    assert row.flight_source == "briefing_arrival"


def test_a_vetoed_flight_leaves_no_source_behind(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """下限关把值降级成 NULL 时，来源也要一起清掉。

    留着来源而值是 NULL，等于库里写着「这一发的时长是从到达时间读出来的」，
    而那一列却是空的——日后查账时那是一句假话，比不说更糟。
    """
    from evo_helper.domain.flight_estimate import FlightSource

    row = _row(
        repository,
        _dispatch(repository, run_id, flight=TRUNCATED, source=FlightSource.BRIEFING_ARRIVAL),
    )

    assert row.flight_seconds is None
    assert row.flight_source is None
