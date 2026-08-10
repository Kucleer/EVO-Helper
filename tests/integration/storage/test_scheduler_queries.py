"""调度器要问数据库的四件事。

这些查询是调度判据的事实来源。它们和 `/logs` 页面读的是同一批表——
判据和页面分叉，是这套东西最容易悄悄出错的地方。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from evo_helper.domain.bot_round import PROBE_PRESET_NAME
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)
from evo_helper.domain.report_wait import MAX_REPORT_AGE

#: 宽限期取 `scheduler_config.report_grace_minutes` 的默认值。
GRACE = timedelta(minutes=30)


def test_todays_pirate_dispatches_are_counted_from_utc_midnight(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """重置点是 UTC 00:00，也就是本地 UTC+8 的每天早上 8 点。"""
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    for dispatched_at in (yesterday, now):
        intent_id = uuid4()
        repository.save_attack_intent(
            AttackIntent(
                intent_id=intent_id,
                run_id=run_id,
                origin=Coordinate(2, 137, 18),
                target=Coordinate(2, 137, 1),
                preset=FleetPresetRef(name="AAA", signature="sig"),
                cycle_start_utc=dispatched_at,
                created_at_utc=dispatched_at,
                target_kind=TARGET_KIND_PIRATE,
            )
        )
        repository.save_dispatch(
            AttackDispatch(
                dispatch_id=uuid4(),
                intent_id=intent_id,
                dispatched_at_utc=dispatched_at,
                accepted=True,
            )
        )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=_utc_midnight(now)) == 1


def test_bot_dispatches_do_not_count_towards_the_pirate_quota(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """标错就白飞一趟舰队——这条测试守的就是 Task 1 修的那个 bug。"""
    now = datetime.now(UTC)
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 140, 3),
            preset=FleetPresetRef(name="BBB", signature="sig"),
            cycle_start_utc=now,
            created_at_utc=now,
            target_kind=TARGET_KIND_BOT,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=now,
            accepted=True,
        )
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=_utc_midnight(now)) == 0
    assert repository.count_dispatches_since(TARGET_KIND_BOT, since=_utc_midnight(now)) == 1


def test_pending_reports_are_scoped_by_target_kind(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """海盗和 bot 各等各的报告。混在一起，一条链路会替另一条判「该回去收了」。"""
    now = datetime.now(UTC)
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 137, 2),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=now,
            created_at_utc=now,
            target_kind=TARGET_KIND_PIRATE,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=now,
            accepted=True,
        )
    )

    assert len(_pending(repository, TARGET_KIND_PIRATE, now)) == 1
    assert _pending(repository, TARGET_KIND_BOT, now) == []


def test_a_dispatch_with_no_flight_time_is_reported_as_unknown(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """读不到飞行时间时 expected 为 None，等待调度器据此立即尝试收取。"""
    now = datetime.now(UTC)
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 137, 3),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=now,
            created_at_utc=now,
            target_kind=TARGET_KIND_PIRATE,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=now,
            accepted=True,
        )
    )

    pending = _pending(repository, TARGET_KIND_PIRATE, now)

    assert pending[0].expected_report_at_utc is None


# -- 放弃规则：不放弃就是永久卡死 --------------------------------------------


def test_an_old_dispatch_with_no_expected_time_is_abandoned(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**这一条守的是调度器不空转。**

    `ReportWaitPlanner` 见到任何一条 `expected_report_at_utc` 为 NULL 的派遣就
    无条件返回 `COLLECT`，而库里现存的派遣**全是 NULL**（飞行时间从来没人读过，
    历史也不回填）。不按 `dispatched_at_utc` 判老，「有到期未收的战报」就永久为真：
    调度器每个 tick 都去起一次 runner，收一封永远不会到的战报，扫描永远抢不到空隙。
    防卡死机制会原样变成卡死机制。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=4,
        dispatched_at=now - MAX_REPORT_AGE - timedelta(minutes=1),
    )

    assert _pending(repository, TARGET_KIND_PIRATE, now) == []


def test_a_fresh_dispatch_with_no_expected_time_is_still_pending(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """刚派出去的那发不能跟着一起排掉——它的战报本来就还没到。"""
    now = datetime.now(UTC)
    _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=5, dispatched_at=now - timedelta(minutes=3)
    )

    assert len(_pending(repository, TARGET_KIND_PIRATE, now)) == 1


def test_a_dispatch_past_its_grace_period_is_abandoned(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """预计时间之后再等一个宽限期还读不到，就判缺失并排除。

    留着的话它同样会把 `COLLECT` 钉死——只是钉死的理由从「未知」换成「过期」。
    """
    now = datetime.now(UTC)
    dispatch_id = _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=6, dispatched_at=now - timedelta(hours=2)
    )
    repository.record_flight_time(dispatch_id, timedelta(minutes=10), now - timedelta(hours=2))

    assert _pending(repository, TARGET_KIND_PIRATE, now) == []


def test_a_dispatch_within_its_grace_period_is_still_pending(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """刚过预计时间、还在宽限期内的，仍然该去收。"""
    now = datetime.now(UTC)
    dispatched_at = now - timedelta(minutes=15)
    dispatch_id = _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=7, dispatched_at=dispatched_at
    )
    repository.record_flight_time(dispatch_id, timedelta(minutes=5), dispatched_at)

    assert len(_pending(repository, TARGET_KIND_PIRATE, now)) == 1


def test_a_long_flight_is_not_abandoned_for_being_old(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """`MAX_REPORT_AGE` **只管 NULL 那一档**。

    飞行时间读到了的那些，老不老由它自己的预计时间说了算。拿派出时刻一起卡，
    会把一发飞十小时、还没到的远征当成缺失排掉。
    """
    now = datetime.now(UTC)
    dispatched_at = now - MAX_REPORT_AGE - timedelta(hours=1)
    dispatch_id = _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=8, dispatched_at=dispatched_at
    )
    repository.record_flight_time(dispatch_id, timedelta(hours=10), dispatched_at)

    assert len(_pending(repository, TARGET_KIND_PIRATE, now)) == 1


# -- 在飞数：航线估算的分子 --------------------------------------------------


def test_inflight_counts_every_kind_together(repository, run_id, session_factory) -> None:  # type: ignore[no-untyped-def]
    """航线是全局资源，不分海盗还是 bot。

    按 kind 分开数会把两条链路各自算成「还有位子」，于是两个 runner 一起起来，
    第二个到了游戏里才发现没航线——权威闸门拦得住，但一趟导航全白跑。
    """
    now = datetime.now(UTC)
    for index, kind in enumerate((TARGET_KIND_PIRATE, TARGET_KIND_BOT)):
        dispatch_id = _dispatch(
            repository, run_id, kind, position=20 + index, dispatched_at=now - timedelta(minutes=5)
        )
        repository.record_flight_time(dispatch_id, timedelta(hours=1), now - timedelta(minutes=5))

    assert repository.count_inflight(now_utc=now) == 2


def test_a_returned_fleet_no_longer_occupies_a_line(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """返航时刻已过就不再占航线——舰队回来了，位子空出来了。

    这正是 `count_inflight` 与 `pending_reports_for_kind` 的分界：那边要的是
    「还没收的战报」（到点了才更该收），这边要的是「还在天上飞的舰队」。
    """
    now = datetime.now(UTC)
    dispatched_at = now - timedelta(hours=2)
    dispatch_id = _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=22, dispatched_at=dispatched_at
    )
    repository.record_flight_time(dispatch_id, timedelta(minutes=10), dispatched_at)

    assert repository.count_inflight(now_utc=now) == 0


def test_an_attack_still_flying_home_keeps_its_line(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**在飞数问的是舰队回来没有，不是战报出来没有。**

    出发 40 分钟、飞行时长 30 分钟：战报早在第 30 分钟就产生了，可舰队要到
    第 60 分钟才落地。按战报那个钟判，这里会算出「航线空着」，调度器于是去派——
    撞上游戏的「同时派遣的舰队数量已达上限。」，一整轮导航白跑。
    """
    now = datetime.now(UTC)
    dispatched_at = now - timedelta(minutes=40)
    dispatch_id = _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=27, dispatched_at=dispatched_at
    )
    repository.record_flight_time(dispatch_id, timedelta(minutes=30), dispatched_at)

    assert repository.count_inflight(now_utc=now) == 1


def test_a_collected_report_does_not_free_the_line_early(
    repository, run_id, session_factory
) -> None:  # type: ignore[no-untyped-def]
    """**战报收到了，舰队还在往回飞。**

    这一条曾经是反过来写的（「战报回来了，那条航线就空了」）——在
    `expected_report_at_utc` 一个钟的年代两者同时成立，所以看不出问题。
    分成两个钟之后它就是个后门：攻击发的战报在 1× 到达，照它释放航线，
    等于把刚修掉的 1× 判据从侧门放回来。
    """
    now = datetime.now(UTC)
    dispatched_at = now - timedelta(minutes=5)
    dispatch_id = _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=23,
        dispatched_at=dispatched_at,
    )
    repository.record_flight_time(dispatch_id, timedelta(hours=1), dispatched_at)
    _attach_report(session_factory, dispatch_id, now)

    assert repository.count_inflight(now_utc=now) == 1


def test_a_probe_frees_its_line_on_arrival(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """探路发是**单程**的：舰队在攻击中损失，没有返程，1× 就该释放。

    按 2× 算不会撞弹窗，会反过来——航线明明空着，调度器却以为还占着而不去派。
    那一侧没有闸门兜底：闸门只拦「派不出去」，拦不住「不去派」。
    """
    now = datetime.now(UTC)
    dispatched_at = now - timedelta(minutes=40)
    dispatch_id = _dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        position=28,
        dispatched_at=dispatched_at,
        preset_name=PROBE_PRESET_NAME,
    )
    repository.record_flight_time(dispatch_id, timedelta(minutes=30), dispatched_at)

    assert repository.count_inflight(now_utc=now) == 0


# -- 侦察发：占航线，但不占配额、也不产生战报 --------------------------------


def test_a_scout_occupies_a_line_until_it_flies_home(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """探测器会飞回来，2×。

    海盗一轮最多派 4 发侦察。它们对调度器隐形的话，估算出来的空闲航线凭空
    多出 4 条——「同时派遣的舰队数量已达上限」多半就是这么来的。
    """
    now = datetime.now(UTC)
    dispatched_at = now - timedelta(minutes=40)
    dispatch_id = _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=29,
        dispatched_at=dispatched_at,
        preset_name="侦察",
        mission_kind=MISSION_KIND_SCOUT,
    )
    repository.record_flight_time(dispatch_id, timedelta(minutes=30), dispatched_at)

    assert repository.count_inflight(now_utc=now) == 1


def test_scouts_do_not_eat_the_daily_attack_quota(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**日配额只数攻击发。**

    配额查询只按 `target_kind` 过滤，而侦察也是打向海盗的。照 `PIRATE` 记进去
    却不区分发次，一轮 4 发侦察就吃掉 4 次攻击额度——当天 32 次以 4 倍速度消失，
    而且完全静默，不报任何错。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=30,
        dispatched_at=now,
        preset_name="侦察",
        mission_kind=MISSION_KIND_SCOUT,
    )
    _dispatch(repository, run_id, TARGET_KIND_PIRATE, position=31, dispatched_at=now)

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=_utc_midnight(now)) == 1


def test_scouts_are_not_waited_for_as_battle_reports(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**侦察不产生战报，喂进 `ReportWaitPlanner` 就是永远等一份不存在的战报。**

    侦察报告走的是信箱里另一条路，`battle_reports` 永远不会闭合这一行。留在
    待收集合里，`plan()` 会一直判「该去收」，海盗链路的「有活干」右半边被钉死为真，
    扫描永远抢不到空隙——本仓库刚因为同一个形状（防卡死反转成永久卡死）栽过一次。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=32,
        dispatched_at=now,
        preset_name="侦察",
        mission_kind=MISSION_KIND_SCOUT,
    )

    assert _pending(repository, TARGET_KIND_PIRATE, now) == []


def test_refused_dispatches_do_not_occupy_a_line(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """被游戏拒掉的那一发没有舰队飞出去，占不到航线。

    把它数进在飞数，估算出来的空闲航线会偏少，调度器于是不肯起攻击任务——
    航线明明空着却一直只跑扫描。
    """
    now = datetime.now(UTC)
    dispatch_id = _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=24,
        dispatched_at=now - timedelta(minutes=5),
        accepted=False,
    )
    repository.record_flight_time(dispatch_id, timedelta(hours=1), now - timedelta(minutes=5))

    assert repository.count_inflight(now_utc=now) == 0


def test_a_dispatch_with_no_flight_time_is_not_counted_as_inflight(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """飞行时间读不到的，估算里当作不占航线——**这是一个自觉的乐观口径**。

    估高了空闲航线，最坏结果是 runner 起来发现没位子、空跑一轮就退；
    估低了则是航线空着不派。权威闸门在 runner 里看屏复核，兜得住前者。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=26,
        dispatched_at=now - timedelta(minutes=5),
    )

    assert repository.count_inflight(now_utc=now) == 0


def _attach_report(session_factory, dispatch_id, reported_at: datetime) -> None:  # type: ignore[no-untyped-def]
    """直接挂一份战报到指定派遣上。

    不走 `append_report`：那条路要靠出发/目标坐标加时间容差去认领派遣，
    在这里等于让测试依赖匹配算法，而这几条测的是「有没有战报」这一个事实。
    """
    from evo_helper.storage import models as orm

    with session_factory() as session:
        session.add(
            orm.BattleReportRow(
                id=uuid4(),
                dispatch_id=dispatch_id,
                reported_at_utc=reported_at,
                attacker_origin_galaxy=2,
                attacker_origin_system=137,
                attacker_origin_position=18,
                defender_target_galaxy=2,
                defender_target_system=137,
                defender_target_position=23,
            )
        )
        session.commit()


def _pending(repository, target_kind: str, now: datetime):  # type: ignore[no-untyped-def]
    return repository.pending_reports_for_kind(
        target_kind, now_utc=now, grace=GRACE, max_age=MAX_REPORT_AGE
    )


def _dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id,
    target_kind: str,
    *,
    position: int,
    dispatched_at: datetime,
    accepted: bool = True,
    preset_name: str = "AAA",
    mission_kind: str = MISSION_KIND_ATTACK,
):
    """一条意图 + 一条派遣。返回派遣 id，好让调用方补写飞行时间。"""
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 137, position),
            preset=FleetPresetRef(name=preset_name, signature="sig"),
            cycle_start_utc=dispatched_at,
            created_at_utc=dispatched_at,
            target_kind=target_kind,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=dispatched_at,
            accepted=accepted,
            mission_kind=mission_kind,
        )
    )
    return dispatch_id


def _utc_midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)
