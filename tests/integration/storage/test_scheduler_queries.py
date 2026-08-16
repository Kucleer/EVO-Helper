"""调度器要问数据库的四件事。

这些查询是调度判据的事实来源。它们和 `/logs` 页面读的是同一批表——
判据和页面分叉，是这套东西最容易悄悄出错的地方。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from evo_helper.domain.fleet_preset import PROBE_PRESET_NAME
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
from evo_helper.domain.report_wait import MAX_REPORT_AGE, UNKNOWN_LINE_HOLD

#: 宽限期取 `scheduler_config.report_grace_minutes` 的默认值。
GRACE = timedelta(minutes=30)

#: 用户的两颗星球（实机截图确认 2026-08-12）：主星奥格瑞玛与 2 号星风暴哨壁。
#: 航线上限是**按星球各一份**的，所以这几个查询全部要按出发星球分组。
HOME = Coordinate(2, 137, 18)
SECOND = Coordinate(9, 250, 8)


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


def test_inflight_counts_every_kind_on_the_same_planet(repository, run_id, session_factory) -> None:  # type: ignore[no-untyped-def]
    """**同一颗星球上**的航线不分海盗还是 bot，一起数。

    按 kind 分开数会把两条链路各自算成「还有位子」，于是两个 runner 一起起来，
    第二个到了游戏里才发现没航线——权威闸门拦得住，但一趟导航全白跑。
    """
    now = datetime.now(UTC)
    for index, kind in enumerate((TARGET_KIND_PIRATE, TARGET_KIND_BOT)):
        dispatch_id = _dispatch(
            repository, run_id, kind, position=20 + index, dispatched_at=now - timedelta(minutes=5)
        )
        repository.record_flight_time(dispatch_id, timedelta(hours=1), now - timedelta(minutes=5))

    assert repository.count_inflight(now_utc=now, origin=HOME) == 2


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

    assert repository.count_inflight(now_utc=now, origin=HOME) == 0


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

    assert repository.count_inflight(now_utc=now, origin=HOME) == 1


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

    assert repository.count_inflight(now_utc=now, origin=HOME) == 1


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

    assert repository.count_inflight(now_utc=now, origin=HOME) == 0


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

    assert repository.count_inflight(now_utc=now, origin=HOME) == 1


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

    assert repository.count_inflight(now_utc=now, origin=HOME) == 0


def test_a_dispatch_with_no_flight_time_still_holds_a_line(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """飞行时间读不到照样占航线。

    NULL 的意思是「不知道它什么时候回来」，不是「它没占位」——被游戏接受的那一发
    舰队一定占着一条位子，简报上读没读到那一行和这件事毫无关系。

    此前这一档按「不占」记，理由是「估高了最坏也只是 runner 空跑一轮就退」。
    实机推翻了那个「最坏」：错估没有回写路径，同一轮会每隔一个 `RESTART_COOLDOWN`
    原样再来，每次都要几十秒导航并一直占着鼠标。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=26,
        dispatched_at=now - timedelta(minutes=5),
    )

    assert repository.count_inflight(now_utc=now, origin=HOME) == 1


def test_a_dispatch_with_no_flight_time_lets_go_after_the_hold_expires(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """NULL 那一档占航线要封顶，否则一发读不出飞行时间的派遣就永久吃掉一条航线。

    封顶取 `UNKNOWN_LINE_HOLD`，与放弃等它战报的阈值同一个时刻：过了这个点，
    两边一起放手。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=27,
        dispatched_at=now - UNKNOWN_LINE_HOLD - timedelta(minutes=1),
    )

    assert repository.count_inflight(now_utc=now, origin=HOME) == 0


def test_the_next_free_line_ignores_dispatches_with_no_flight_time(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """航线钟为 NULL 的那些不给「下一条航线什么时候空」当闹钟。

    它们的 `UNKNOWN_LINE_HOLD` 是「等到这里就放弃」的上界，不是对返航时刻的预测。
    拿它当闹钟，调度器会一睡 6 小时。全场只剩这种派遣时宁可答不上来（None），
    让调用方走自己那条退避。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=28,
        dispatched_at=now - timedelta(minutes=5),
    )

    assert repository.count_inflight(now_utc=now, origin=HOME) == 1
    assert repository.next_line_free_at(now_utc=now, origin=HOME) is None


def test_the_next_free_line_is_the_earliest_one_still_out(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """有几支在飞就取最早回来的那个时刻——那是「局面会变」的最近一个锚点。"""
    now = datetime.now(UTC)
    for position, flight in ((21, timedelta(hours=2)), (22, timedelta(minutes=40))):
        dispatch_id = _dispatch(
            repository, run_id, TARGET_KIND_PIRATE, position=position, dispatched_at=now
        )
        repository.record_flight_time(dispatch_id, flight, now)

    # 攻击发按 2× 算（打完还要飞回来），所以最早的是 40 分钟那发的 80 分钟。
    assert repository.next_line_free_at(now_utc=now, origin=HOME) == now + timedelta(minutes=80)


def test_the_next_free_line_is_none_when_nothing_is_out(repository) -> None:  # type: ignore[no-untyped-def]
    """一支在飞的都没有：这一层对「航线满不满」没有任何证据，不许瞎猜一个时刻。"""
    assert repository.next_line_free_at(now_utc=datetime.now(UTC), origin=HOME) is None


def test_the_last_dispatch_time_is_per_target_kind(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """调度器拿它和「上一次启动」比大小，判上一轮是不是空手而归。

    按目标分开：bot 那轮派出去了，不该让海盗看起来也派出去了。
    """
    now = datetime.now(UTC)
    _dispatch(repository, run_id, TARGET_KIND_BOT, position=11, dispatched_at=now)

    assert repository.last_dispatch_at(TARGET_KIND_BOT, origin=HOME) == now
    assert repository.last_dispatch_at(TARGET_KIND_PIRATE, origin=HOME) is None


def test_the_last_dispatch_time_counts_scouts_too(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """一轮只派了侦察不算空手而归——侦察一样占航线。

    漏掉它的话，「侦察派满、攻击没派」的那一轮会被读成空手而归，链路白等一趟。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=12,
        dispatched_at=now,
        preset_name="侦察",
        mission_kind=MISSION_KIND_SCOUT,
    )

    assert repository.last_dispatch_at(TARGET_KIND_PIRATE, origin=HOME) == now


def test_a_refused_dispatch_is_not_a_dispatch(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """被游戏拒掉的那一发根本没飞出去。

    算进来就是把「撞上航线上限」读成「派成功了」——而那恰恰是要认出来的那件事，
    认错了调度器就照旧一轮轮地起。
    """
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=13,
        dispatched_at=datetime.now(UTC),
        accepted=False,
    )

    assert repository.last_dispatch_at(TARGET_KIND_PIRATE, origin=HOME) is None


# -- 人工清理航线占用 -----------------------------------------------------------
#
# 库里那两个钟都是**推算**（出发时刻 + 派出时读到的飞行时长 × 倍数）。舰队真回港
# 了它们也不会自己改口，读不出飞行时间的那一档更是按 90 分钟的上界硬占——于是
# 任务卡在「等航线」，而真实航线是空的。用户口径 2026-08-16：「时间到了，自然就
# 释放了航线，我会手动 check 后清理。」


def test_releasing_lines_frees_the_ones_still_out(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """按下清理之后，这一发不再计入在飞数。"""
    now = datetime.now(UTC)
    dispatch_id = _dispatch(repository, run_id, TARGET_KIND_BOT, position=51, dispatched_at=now)
    repository.record_flight_time(dispatch_id, timedelta(hours=1), now)
    assert repository.count_inflight(now_utc=now, origin=HOME) == 1

    assert repository.release_held_lines(now_utc=now) == 1

    assert repository.count_inflight(now_utc=now, origin=HOME) == 0


def test_releasing_lines_keeps_the_return_clock_as_a_record(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**`line_free_at_utc` 一个字都不许改。**

    那一列是观测：派出时读到的飞行时长推算出来的返航时刻。把它改写成「现在」
    确实也能让这一发不再计入在飞数，但同时抹掉了「这一发飞了多久」——而
    `domain.report_wait.vet_flight_time` 那道下限正是靠这批样本校准出来的
    （生产库 209 发攻击里有 66 发落在 0–59 秒，是解析截断的残骸）。

    所以放手写在另一列上，两句话各说各的：「舰队几点回来」与「人几点说它回来了」。
    """
    now = datetime.now(UTC)
    dispatch_id = _dispatch(repository, run_id, TARGET_KIND_BOT, position=52, dispatched_at=now)
    repository.record_flight_time(dispatch_id, timedelta(hours=1), now)

    repository.release_held_lines(now_utc=now)

    row = _dispatch_row(repository, dispatch_id)
    # 攻击发按 2× 算（打完还要飞回来）。
    assert row.line_free_at_utc == now + timedelta(hours=2)
    assert row.flight_seconds == 3600
    assert row.line_released_at_utc == now


def test_releasing_lines_also_lets_go_of_the_unknown_flight_time_ones(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """读不出飞行时间的那一档也要放开。

    它按 `UNKNOWN_LINE_HOLD`（90 分钟）硬占，是实机上最容易把任务钉死在
    「等航线」上的一批。只对有航线钟的那些生效的话，用户按下按钮会一点动静
    都没有——而那正是他最需要这个按钮的时候。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        position=53,
        dispatched_at=now - timedelta(minutes=5),
    )
    assert repository.count_inflight(now_utc=now, origin=HOME) == 1

    assert repository.release_held_lines(now_utc=now) == 1

    assert repository.count_inflight(now_utc=now, origin=HOME) == 0


def test_releasing_lines_silences_the_wait_for_a_line_alarm(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """闹钟也得跟着灭。

    `next_line_free_at` 是调度器把「等航线」压住多久的锚点。放手之后它还答得
    出一个两小时后的时刻，页面就会继续写「等航线」并把链路压到那时——那句话
    既是假的，也正好是用户按这个按钮想消掉的东西。
    """
    now = datetime.now(UTC)
    dispatch_id = _dispatch(repository, run_id, TARGET_KIND_BOT, position=54, dispatched_at=now)
    repository.record_flight_time(dispatch_id, timedelta(hours=1), now)
    assert repository.next_line_free_at(now_utc=now, origin=HOME) == now + timedelta(hours=2)

    repository.release_held_lines(now_utc=now)

    assert repository.next_line_free_at(now_utc=now, origin=HOME) is None


def test_releasing_lines_covers_every_planet(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """一下清干净所有出发星球。

    航线上限按星球各一份，但这个按钮说的是「我刚在游戏里数过，航线都空着」——
    那是对整个账号说的一句话。只清一颗星球等于让用户逐颗点，而漏点的那颗会
    继续把任务钉在「等航线」上。
    """
    now = datetime.now(UTC)
    for position, origin in ((55, HOME), (56, SECOND)):
        dispatch_id = _dispatch(
            repository, run_id, TARGET_KIND_BOT, position=position, dispatched_at=now, origin=origin
        )
        repository.record_flight_time(dispatch_id, timedelta(hours=1), now)

    assert repository.release_held_lines(now_utc=now) == 2

    assert repository.count_inflight(now_utc=now, origin=HOME) == 0
    assert repository.count_inflight(now_utc=now, origin=SECOND) == 0


def test_releasing_lines_only_stamps_the_ones_still_holding(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """早就自然到点的、以及被游戏拒掉的，都不打这个戳。

    打了的话，日后想问「哪些航线是人手动清掉的」，答案里会混进一整库跟这次
    按钮毫无关系的派遣；被拒的那些更是压根没飞出去，没有航线可放。
    """
    now = datetime.now(UTC)
    landed = _dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        position=57,
        dispatched_at=now - timedelta(hours=5),
    )
    repository.record_flight_time(landed, timedelta(hours=1), now - timedelta(hours=5))
    refused = _dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        position=58,
        dispatched_at=now,
        accepted=False,
    )
    repository.record_flight_time(refused, timedelta(hours=1), now)

    assert repository.release_held_lines(now_utc=now) == 0

    assert _dispatch_row(repository, landed).line_released_at_utc is None
    assert _dispatch_row(repository, refused).line_released_at_utc is None


def test_releasing_lines_twice_does_not_restamp(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """第二下没有可放的了，回执必须是 0。

    重复计数会让页面对着一个什么都没做的按钮说「已放开 1 条」——用户据此以为
    库里又攒出了一条占用。
    """
    now = datetime.now(UTC)
    dispatch_id = _dispatch(repository, run_id, TARGET_KIND_BOT, position=59, dispatched_at=now)
    repository.record_flight_time(dispatch_id, timedelta(hours=1), now)

    assert repository.release_held_lines(now_utc=now) == 1
    assert repository.release_held_lines(now_utc=now + timedelta(minutes=1)) == 0

    # 戳记停在第一次那一下，不被第二次覆盖。
    assert _dispatch_row(repository, dispatch_id).line_released_at_utc == now


def _dispatch_row(repository, dispatch_id):  # type: ignore[no-untyped-def]
    """直接把那一行派遣捞出来。放手是否改写了别的列，只有这样才看得见。"""
    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001 - 测试直接读库
        row = session.get(orm.AttackDispatchRow, dispatch_id)
        assert row is not None
        session.expunge(row)
        return row


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


def _pending(repository, target_kind: str, now: datetime, origin: Coordinate = HOME):  # type: ignore[no-untyped-def]
    return repository.pending_reports_for_kind(
        target_kind, now_utc=now, grace=GRACE, max_age=MAX_REPORT_AGE, origin=origin
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
    origin: Coordinate = HOME,
):
    """一条意图 + 一条派遣。返回派遣 id，好让调用方补写飞行时间。"""
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=origin,
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


# -- 按出发星球分：主星与 2 号星各占各的 --------------------------------------
#
# 用户口径（2026-08-13，追问确认）：「航线上限是按星球各一份的，不是账号共享」。
# 这几条盯的是「全库一起数」那个旧口径会不会从哪个角落回来。


def test_inflight_only_counts_fleets_that_left_this_planet(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**不同出发星球互不影响。**

    主星派出去两支、2 号星派出去一支：主星那颗看到 2，2 号星那颗看到 1。
    全库一起数的话两边都会看到 3，于是主星打满之后 2 号星也不敢派了。

    ⚠️ 两颗星的在飞数**故意不同**（2 与 1）：填成一样的话，把过滤条件整个删掉
    也未必露馅。
    """
    now = datetime.now(UTC)
    dispatched_at = now - timedelta(minutes=5)
    for position in (41, 42):
        dispatch_id = _dispatch(
            repository, run_id, TARGET_KIND_BOT, position=position, dispatched_at=dispatched_at
        )
        repository.record_flight_time(dispatch_id, timedelta(hours=1), dispatched_at)
    elsewhere = _dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        position=43,
        dispatched_at=dispatched_at,
        origin=SECOND,
    )
    repository.record_flight_time(elsewhere, timedelta(hours=1), dispatched_at)

    assert repository.count_inflight(now_utc=now, origin=HOME) == 2
    assert repository.count_inflight(now_utc=now, origin=SECOND) == 1


def test_the_next_free_line_is_the_earliest_one_on_that_same_planet(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """闹钟也按星球分。

    拿别的星球的返航时刻当闹钟，压住的那段时间与这个任务能不能派毫无关系
    ——2 号星那个任务会为主星的一支远征白等两小时。
    """
    now = datetime.now(UTC)
    dispatched_at = now - timedelta(minutes=5)
    far = _dispatch(repository, run_id, TARGET_KIND_BOT, position=44, dispatched_at=dispatched_at)
    repository.record_flight_time(far, timedelta(hours=2), dispatched_at)
    near = _dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        position=45,
        dispatched_at=dispatched_at,
        origin=SECOND,
    )
    repository.record_flight_time(near, timedelta(minutes=20), dispatched_at)

    assert repository.next_line_free_at(now_utc=now, origin=HOME) == dispatched_at + timedelta(
        hours=4
    )
    assert repository.next_line_free_at(now_utc=now, origin=SECOND) == dispatched_at + timedelta(
        minutes=40
    )


def test_the_last_dispatch_time_is_per_origin_too(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """「上一轮空手而归」按出发星球判。

    不分的话，主星那个任务派出去的一发会让 2 号星那个任务看起来「上一轮有派
    出去」，于是它撞满航线之后照样每五分钟白跑一轮——而 `waiting_for_a_line`
    存在的全部意义就是不让这件事重复发生。
    """
    now = datetime.now(UTC)
    _dispatch(repository, run_id, TARGET_KIND_BOT, position=46, dispatched_at=now)

    assert repository.last_dispatch_at(TARGET_KIND_BOT, origin=HOME) == now
    assert repository.last_dispatch_at(TARGET_KIND_BOT, origin=SECOND) is None


def test_pending_reports_are_scoped_by_origin_as_well_as_kind(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """战报也按出发星球分。

    两个 bot 任务各自只该为自己派出去的那些回信箱：不分的话，2 号星那个任务会
    因为主星那些还没到的战报而一直判「该去收」，每五分钟进一趟信箱扑空。
    """
    now = datetime.now(UTC)
    _dispatch(repository, run_id, TARGET_KIND_BOT, position=47, dispatched_at=now)

    assert len(_pending(repository, TARGET_KIND_BOT, now, HOME)) == 1
    assert _pending(repository, TARGET_KIND_BOT, now, SECOND) == []
