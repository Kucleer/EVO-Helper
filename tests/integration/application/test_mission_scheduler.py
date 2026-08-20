"""常驻调度循环：把纯判据、子进程管理、数据库粘起来。

判据本身在 `tests/unit/domain/test_scheduler.py` 里已经钉死，这里守的是**接线**：
事实从库里读对了没有、参数换算成了什么命令行、起停有没有落进 `mission_runs`。
接线错了不会报错，只会让调度器静默地空转或者永久卡死——那正是这整条修复要防的。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from evo_helper.application.mission_progress import STALL_TIMEOUT, ProgressReading
from evo_helper.application.mission_scheduler import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_ENVIRONMENT_EXEMPTIONS,
    STALE_POOL_WARNING_AFTER,
    MissionScheduler,
)
from evo_helper.application.mission_supervisor import MissionExit, StopReason
from evo_helper.domain.bot_round import BOT_ATTACK_PRESET
from evo_helper.domain.missions import MissionIdle, MissionParamError
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
from evo_helper.domain.scheduler import (
    EXIT_ENVIRONMENT_BUSY,
    GAP_FILLERS,
    RESTART_COOLDOWN,
    MissionKind,
)
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
BOT_RANGE = '{"galaxy": 2, "first_system": 100, "last_system": 200}'
#: 种子任务的出发星球（`domain.missions.ORIGIN`）。派遣默认记在它头上。
DEFAULT_ORIGIN = Coordinate(2, 137, 18)


# -- 夹具与小工具 --------------------------------------------------------------


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


def task_id(repository: SqlAlchemyRepository, kind: MissionKind) -> int:
    """这条链路那一行的 id。

    任务的身份是 `id` 而不是 `kind`（同一 kind 可以有多行），写库的入口全部按 id
    寻址，测试也就得先把 id 捞出来。种子行每条链路各一个，所以这里取第一个。
    """
    return next(row.id for row in repository.mission_tasks() if row.kind == kind.value)


def enable(repository: SqlAlchemyRepository, kind: MissionKind, **fields: object) -> None:
    repository.update_mission_task(task_id(repository, kind), enabled=True, **fields)  # type: ignore[arg-type]


def only_gap_filler(repository: SqlAlchemyRepository, kept: MissionKind | None = None) -> None:
    """把填空隙的那几种（扫描 / 军力榜）全关掉，只留 `kept`。

    2026-08-15 加军力榜之前，填空隙的只有扫描一种，所以这些用例里到处写着
    `disable(repository, MissionKind.SCAN)`。加了第二种之后那样写就不够了——
    另一种会顶上来把空隙填掉，于是「只该起一次」的断言看到两次。
    """
    for kind in GAP_FILLERS:
        if kind is not kept:
            disable(repository, kind)


def disable(repository: SqlAlchemyRepository, kind: MissionKind) -> None:
    repository.update_mission_task(task_id(repository, kind), enabled=False)


def add_bot_target(  # type: ignore[no-untyped-def]
    session_factory,
    coordinate: Coordinate,
    *,
    military_score: float | None = None,
    scanned_at: datetime | None = NOW,
) -> None:
    """往 `bot_targets` 里放一颗已记录的 bot。

    `scanned_at` 就是库里那一列 `military_score_at_utc`（页面上叫「更新时间」），
    **默认给「刚读到」**：军力优先那一支按它划有效期窗口，不给的话每条用例都要为
    一个与它无关的理由写读取时刻。要验「读数很旧」就显式传一个旧时刻，
    要验「从没上过榜」就 `military_score=None, scanned_at=None`。

    ⚠️ **`military_score` 默认是 `None`，也就是「从没上过军力榜」——那一档
    2026-08-18 起根本不参与攻击**（`domain.target_order.has_a_military_reading`）。
    所以凡是要「这颗能被打出去」的用例，军力分数必须显式给一个数。
    """
    with session_factory() as session:
        session.add(
            orm.BotTargetRow(
                id=uuid4(),
                galaxy=coordinate.galaxy,
                system=coordinate.system,
                position=coordinate.position,
                is_bot=True,
                military_score=military_score,
                military_score_at_utc=scanned_at,
            )
        )
        session.commit()


def rescan_bot_target(  # type: ignore[no-untyped-def]
    session_factory, coordinate: Coordinate, *, scanned_at: datetime
) -> None:
    """模拟军力榜又把这一颗读了一遍：只动读取时刻那一列。"""
    with session_factory() as session:
        row = session.scalars(
            select(orm.BotTargetRow).where(
                orm.BotTargetRow.galaxy == coordinate.galaxy,
                orm.BotTargetRow.system == coordinate.system,
                orm.BotTargetRow.position == coordinate.position,
            )
        ).one()
        row.military_score_at_utc = scanned_at
        session.commit()


def score_bot_target(  # type: ignore[no-untyped-def]
    session_factory,
    coordinate: Coordinate,
    *,
    military_score: float | None,
    scanned_at: datetime | None,
) -> None:
    """模拟军力榜给这一颗写上（或清掉）军力读数：**分数和时刻一起动**。

    和 `rescan_bot_target` 分开：那一个只动时刻，用来验「读数变新了」；
    这一个改的是「有没有读数」，也就是这颗目标进不进得了候选池
    （`domain.target_order.has_a_military_reading`）。
    """
    with session_factory() as session:
        row = session.scalars(
            select(orm.BotTargetRow).where(
                orm.BotTargetRow.galaxy == coordinate.galaxy,
                orm.BotTargetRow.system == coordinate.system,
                orm.BotTargetRow.position == coordinate.position,
            )
        ).one()
        row.military_score = military_score
        row.military_score_at_utc = scanned_at
        session.commit()


def dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id,
    target_kind: str,
    *,
    target: Coordinate,
    dispatched_at: datetime,
    preset_name: str = "AAA",
    flight: timedelta | None = None,
    mission_kind: str = MISSION_KIND_ATTACK,
    origin: Coordinate = DEFAULT_ORIGIN,
):
    """记一发被游戏接受的派遣。

    `flight` 不传就留空航线钟，那一档按 `UNKNOWN_LINE_HOLD`（90 分钟）算**仍然
    占着航线**——测试若只想验别的事，就得把飞行时间给上，否则这一发会一直压着
    航线让链路起不来。

    `origin` 默认是全局主星，也就是绝大多数用例里那颗。**多出发点的用例必须显式
    传**：航线记账按出发星球分（`repository.count_inflight`），派遣记在哪颗星球上
    决定了这一发压住的是谁的预算。
    """
    intent_id, dispatch_id = uuid4(), uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=origin,
            target=target,
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
            accepted=True,
            mission_kind=mission_kind,
        )
    )
    if flight is not None:
        repository.record_flight_time(dispatch_id, flight, dispatched_at)
    return dispatch_id


def attach_report(  # type: ignore[no-untyped-def]
    session_factory,
    dispatch_id,
    target: Coordinate,
    reported_at: datetime,
    outcome: str | None = None,
) -> None:
    """直接挂一份战报，不走 `append_report` 的坐标+时间容差匹配。

    那条路等于让测试依赖匹配算法，而这里要验的只是「有没有战报、战果是什么」
    这两个事实。
    """
    with session_factory() as session:
        session.add(
            orm.BattleReportRow(
                id=uuid4(),
                dispatch_id=dispatch_id,
                reported_at_utc=reported_at,
                attacker_origin_galaxy=2,
                attacker_origin_system=137,
                attacker_origin_position=18,
                defender_target_galaxy=target.galaxy,
                defender_target_system=target.system,
                defender_target_position=target.position,
                outcome=outcome,
            )
        )
        session.commit()


def set_config(session_factory, **fields: int) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        row = session.get(orm.SchedulerConfigRow, 1)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        session.commit()


def task(repository: SqlAlchemyRepository, kind: MissionKind) -> orm.MissionTaskRow:
    return next(row for row in repository.mission_tasks() if row.kind == kind.value)


# -- 开关与基本流转 ------------------------------------------------------------


def test_a_stopped_scheduler_never_starts_anything(scheduler, launcher) -> None:  # type: ignore[no-untyped-def]
    """没点「开始」就什么都不干。tick 每秒都在跑，它不能自己决定开工。"""
    scheduler.tick()

    assert launcher.spawned == []


def test_the_switch_is_not_persisted_across_a_restart(repository, launcher, clock) -> None:  # type: ignore[no-untyped-def]
    """控制台重启后一律停在「已停止」。

    重启多半意味着出了事，自动接着派舰队不是好默认。
    """
    first = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    first.prepare()
    first.start()

    restarted = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    restarted.prepare()

    assert not restarted.enabled


def test_the_top_priority_chain_with_work_is_launched(scheduler, repository, launcher) -> None:  # type: ignore[no-untyped-def]
    enable(repository, MissionKind.PIRATE)
    scheduler.start()

    scheduler.tick()

    assert launcher.kinds == [MissionKind.PIRATE]


def test_a_launch_is_written_into_the_run_ledger(scheduler, repository, launcher) -> None:  # type: ignore[no-untyped-def]
    """`mission_runs` 是调度循环唯一的记忆，也是页面上那段历史。"""
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    scheduler.tick()

    row = repository.mission_runs(limit=1)[0]
    assert row.kind == "PIRATE"
    assert row.pid == launcher.latest.pid
    assert row.ended_at_utc is None
    assert "pirate_loop" in row.command


def test_only_one_child_runs_at_a_time(scheduler, repository, launcher) -> None:  # type: ignore[no-untyped-def]
    """一个游戏窗口，一个鼠标。这是整套东西最硬的一条不变量。"""
    enable(repository, MissionKind.PIRATE)
    scheduler.start()

    for _ in range(5):
        scheduler.tick()

    assert len(launcher.spawned) == 1


def test_stopping_kills_the_child_and_closes_its_run(scheduler, repository, launcher) -> None:  # type: ignore[no-untyped-def]
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    scheduler.tick()

    scheduler.stop()

    assert launcher.latest.terminated
    row = repository.mission_runs(limit=1)[0]
    assert row.stopped_by == "USER"
    assert row.ended_at_utc == NOW


class SlowRepository(SqlAlchemyRepository):
    """把「读事实」拖住，模拟一次真实的 `_facts()`。

    生产库里 bot 范围有 4237 个目标，一次 `_facts()` 要按目标逐个问库，实测
    0.32 秒；而 tick 每秒一次、页面每 2 秒问一次状态，多开几个浏览器标签就是
    几份。这里只是把那段时间拉长成一把测试握得住的闸门。
    """

    def __init__(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session_factory)
        self.entered = threading.Event()
        self.release = threading.Event()

    def last_mission_starts(self):  # type: ignore[no-untyped-def, override]
        self.entered.set()
        self.release.wait(timeout=10)
        return super().last_mission_starts()


def test_stopping_does_not_queue_behind_a_tick_that_is_reading_facts(  # type: ignore[no-untyped-def]
    session_factory, launcher, clock
) -> None:
    """**用户口径：「控制台无法结束任务」。**

    实机 2026-08-11：页面显示「运行中，已运行 2:29:08」，点「结束」毫无反应、
    秒表照走。成因是「结束」和「读事实」共用同一把锁：读事实没有上界（4237 个
    bot 目标逐个问库），而 `RLock` 没有公平性，排在一群反复重取的线程后面可以
    饿任意久；FastAPI 的同步接口又跑在容量 40 的线程池里，状态轮询全卡在锁上
    之后，那个 POST 连线程都分不到——页面上就是「点了没反应」。

    这里把 tick 卡在读事实中间，然后要求「结束」照样立刻杀掉子进程。
    锁一旦重新护住读事实，`join(2)` 会超时，这条就红。
    """
    repository = SlowRepository(session_factory)
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    repository.release.set()
    scheduler.tick()  # 起一个子进程；这一趟不拦
    assert launcher.kinds == [MissionKind.PIRATE]

    repository.release.clear()
    ticking = threading.Thread(target=scheduler.tick, daemon=True)
    ticking.start()
    assert repository.entered.wait(timeout=5), "tick 没有走到读事实这一步"

    stopping = threading.Thread(target=scheduler.stop, daemon=True)
    stopping.start()
    stopping.join(timeout=2)

    assert not stopping.is_alive(), "「结束」被读事实堵住了"
    assert launcher.latest.terminated
    assert not scheduler.enabled
    repository.release.set()
    ticking.join(timeout=5)
    # 那一轮 tick 拿的是「结束」之前的事实。它醒来之后绝不能照着旧决策再起一个
    # ——否则控制台以为已经停了，实际还有一个 runner 在点鼠标。
    assert len(launcher.spawned) == 1
    assert scheduler.current is None


def test_shutdown_clears_the_child_so_it_does_not_outlive_the_console(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """lifespan 关闭时主动清场，覆盖「正常重启」这条最常见的路径。"""
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    scheduler.tick()

    scheduler.shutdown()

    assert launcher.latest.terminated
    assert repository.mission_runs(limit=1)[0].stopped_by == "SHUTDOWN"


# -- 参数换算 ------------------------------------------------------------------


def test_the_pirate_command_carries_the_systems_the_radius_covers(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    enable(repository, MissionKind.PIRATE, params_json='{"radius": 2}')
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert "2:137" in command and "2:139" in command
    assert "2:140" not in command
    # 这两个开关是「真的动鼠标派舰队」的意思，漏掉不报错、看着一切正常，
    # 代价是当天配额白白流失。
    assert command[-2:] == ["--scout", "--attack"]


def test_the_bot_command_only_carries_targets_inside_the_range(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    add_bot_target(session_factory, Coordinate(2, 150, 5))
    add_bot_target(session_factory, Coordinate(2, 900, 6))
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert "2:150:5" in command
    assert "2:900:6" not in command
    # `--attack` 是「真的动鼠标派舰队」的意思，漏掉不报错、看着一切正常，
    # 代价是这一轮一发都没打。⚠️ `--probe` / `--tier-thresholds` 必须**不在**：
    # runner 已经不认识它们，多传一个就是 `SystemExit(2)`，而调度器只看得到
    # 「这条链路又崩了一次」。
    assert command[-1] == "--attack"
    assert "--probe" not in command
    assert "--tier-thresholds" not in command


def test_bad_parameters_disable_the_chain_instead_of_killing_the_loop(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """`MissionParamError` 必须被接住。让它冒出来就是整个调度循环停摆。"""
    enable(
        repository,
        MissionKind.BOT,
        params_json='{"galaxy": 2, "first_system": 200, "last_system": 100}',
    )
    scheduler.start()

    scheduler.tick()

    assert task(repository, MissionKind.BOT).disabled_reason is not None
    # 顺位让给下一个，而不是整轮空转。
    assert launcher.kinds == [MissionKind.SCAN]


def test_a_bad_radius_yields_its_turn_in_the_same_tick(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """海盗的参数要到组命令行才校验得出来，所以停用发生在决策之后。

    停完就得重算一次，否则这一秒谁都不跑——每秒一次 tick，看起来只是慢，
    但配额窗口是按天算的，白丢的次数补不回来。
    """
    enable(repository, MissionKind.PIRATE, params_json='{"radius": 0}')
    scheduler.start()

    scheduler.tick()

    assert task(repository, MissionKind.PIRATE).disabled_reason is not None
    assert launcher.kinds == [MissionKind.SCAN]


# -- 日配额：起算点是 UTC 午夜 --------------------------------------------------


def test_todays_quota_is_counted_from_utc_midnight_not_local_midnight(  # type: ignore[no-untyped-def]
    repository, launcher, run_id, session_factory
) -> None:
    """**这条守的是「本地是 UTC+8，重置点在早上 8 点」。**

    现在是本地 8 月 9 日早上 11 点（= UTC 8 月 9 日 03:00），当日配额从 UTC
    8 月 9 日 00:00 起算。UTC 8 月 8 日 20:00 那一发属于**昨天**，不该占今天
    的额度。按本地日历天数（起算点 = 本地 8 月 9 日 0 点 = UTC 8 月 8 日 16:00）
    会把它数进来，配额提前判成用尽，海盗白白少打一整天。
    """
    now = datetime(2026, 8, 9, 3, 0, tzinfo=UTC)
    clock = Clock(now)
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    set_config(session_factory, pirate_daily_quota=1)
    enable(repository, MissionKind.PIRATE)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        target=Coordinate(2, 137, 1),
        dispatched_at=datetime(2026, 8, 8, 20, 0, tzinfo=UTC),
    )
    scheduler.start()

    scheduler.tick()

    assert launcher.kinds == [MissionKind.PIRATE]


def test_a_dispatch_made_today_does_use_up_the_quota(  # type: ignore[no-untyped-def]
    repository, launcher, run_id, session_factory
) -> None:
    """同一组设置的另一半：真正落在当日 UTC 里的那一发要数进来，否则超限。"""
    now = datetime(2026, 8, 9, 3, 0, tzinfo=UTC)
    clock = Clock(now)
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    set_config(session_factory, pirate_daily_quota=1)
    enable(repository, MissionKind.PIRATE)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        target=Coordinate(2, 137, 1),
        dispatched_at=datetime(2026, 8, 9, 1, 0, tzinfo=UTC),
    )
    scheduler.start()

    scheduler.tick()

    assert launcher.kinds == [MissionKind.SCAN]


# -- 战报判据的接线：grace 与 max_age ------------------------------------------


class SpyRepository(SqlAlchemyRepository):
    """只记一笔 `pending_reports_for_kind` 收到的实参，其余照常。"""

    def __init__(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        super().__init__(session_factory)
        self.pending_calls: list[tuple[str, timedelta, timedelta, Coordinate]] = []

    def pending_reports_for_kind(  # type: ignore[override]
        self, target_kind, *, now_utc, grace, max_age, origin
    ):
        self.pending_calls.append((target_kind, grace, max_age, origin))
        return super().pending_reports_for_kind(
            target_kind, now_utc=now_utc, grace=grace, max_age=max_age, origin=origin
        )


def test_the_pending_report_query_gets_the_configured_grace_and_the_domain_max_age(  # type: ignore[no-untyped-def]
    session_factory, launcher, clock
) -> None:
    """**这两个实参没有默认值，传错了不会报错。**

    `grace` 来自 `scheduler_config.report_grace_minutes`，`max_age` 是
    `domain.report_wait.MAX_REPORT_AGE`。两者管的是完全不同的一档：
    前者管「读到了飞行时间」的，后者管「没读到」的。互换或者传成同一个值，
    结果只是调度器静默地空转或者永久卡死。
    """
    repository = SpyRepository(session_factory)
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    set_config(session_factory, report_grace_minutes=45)
    enable(repository, MissionKind.PIRATE)
    scheduler.start()

    scheduler.tick()

    # 出发星球也在这条查询上：它没有默认值，漏传同样不会报错，只会让两个任务
    # 互相替对方判「该回去收了」。
    assert (
        TARGET_KIND_PIRATE,
        timedelta(minutes=45),
        MAX_REPORT_AGE,
        Coordinate(2, 137, 18),
    ) in repository.pending_calls


def test_an_old_unknown_flight_still_counts_as_work_until_max_age(  # type: ignore[no-untyped-def]
    repository, launcher, clock, run_id, session_factory
) -> None:
    """飞行时间读不到的那一发，两小时后仍然该回去收。

    把 `max_age` 错传成 `grace`（30 分钟）的话它会被判成缺失排掉，海盗于是
    在航线占满时无事可做——一份其实还会到的战报再没人去收。
    """
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    set_config(session_factory, fleet_line_limit=0)
    enable(repository, MissionKind.PIRATE)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        target=Coordinate(2, 137, 1),
        dispatched_at=NOW - timedelta(hours=2),
    )
    scheduler.start()

    scheduler.tick()

    assert launcher.kinds == [MissionKind.PIRATE]


def test_a_dispatch_past_its_grace_period_stops_counting_as_work(  # type: ignore[no-untyped-def]
    repository, launcher, clock, run_id, session_factory
) -> None:
    """预计时间之后再等一个宽限期还读不到，就判缺失、不再钉住「有活干」。

    把 `grace` 错传成 `MAX_REPORT_AGE`（6 小时）的话它会一直算数，海盗每个
    tick 都去收一封永远不会到的战报，扫描永远抢不到空隙。
    """
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    set_config(session_factory, fleet_line_limit=0)
    enable(repository, MissionKind.PIRATE)
    dispatched_at = NOW - timedelta(hours=2)
    dispatch_id = dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        target=Coordinate(2, 137, 1),
        dispatched_at=dispatched_at,
    )
    repository.record_flight_time(dispatch_id, timedelta(minutes=10), dispatched_at)
    scheduler.start()

    scheduler.tick()

    assert launcher.kinds == [MissionKind.SCAN]


# -- 重启冷却 ------------------------------------------------------------------


def test_a_chain_that_just_ran_waits_out_the_cooldown(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """runner 扑空退出后立刻再起一次，几十秒的导航全白费，还占着鼠标。"""
    enable(repository, MissionKind.PIRATE)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()
    launcher.latest.exit_code = 0
    clock.now = NOW + timedelta(seconds=30)

    scheduler.tick()

    assert len(launcher.spawned) == 1


def test_the_chain_comes_back_once_the_cooldown_expires(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    scheduler.tick()
    launcher.latest.exit_code = 0
    clock.now = NOW + RESTART_COOLDOWN + timedelta(seconds=1)

    scheduler.tick()

    assert launcher.kinds == [MissionKind.PIRATE, MissionKind.PIRATE]


# -- 航线占满之后不要再一轮轮地起 ----------------------------------------------


def _scout_still_out(repository, run_id, *, dispatched_at, flight):  # type: ignore[no-untyped-def]
    """派一发还在外面没回来的侦察，好让「下一条航线什么时候空」有个真实答案。

    **用侦察而不是攻击**：侦察不产生 `battle_reports`，也就不会顺带把
    `pirate_reports_due` 点亮。用攻击发的话，等航线那一档刚解除，战报判据也
    到期了，测出来分不清链路到底是为哪一边起来的。侦察一样占航线（这正是这里
    要的），只是不进配额、不进战报。
    """
    return dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        target=Coordinate(2, 137, 1),
        dispatched_at=dispatched_at,
        preset_name="侦察",
        mission_kind=MISSION_KIND_SCOUT,
        flight=flight,
    )


def test_a_round_that_dispatched_nothing_does_not_restart_while_a_fleet_is_out(  # type: ignore[no-untyped-def]
    repository, launcher, clock, run_id, session_factory
) -> None:
    """**用户口径：「航路上限到达后，不应继续海盗任务。」**

    实机 2026-08-11 01:12–01:34 UTC（本地 09:12–09:34）：估算的空闲航线一路报 3，
    游戏那边 6 条全满，海盗与 bot 交替起了九轮，每轮几十秒导航之后撞上
    「同时派遣的舰队数量已达上限。」退出、冷却五分钟、再来。

    这里守的是接线：库里读出来的「上一轮空手而归」+「还有舰队在外面」必须真的
    落到「这一轮不起」。**注意空闲航线是 5**——只看 `free_lines` 的话它一定会起，
    所以这条断言是有牙的。
    """
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    set_config(session_factory, fleet_line_limit=6)
    enable(repository, MissionKind.PIRATE)
    only_gap_filler(repository)
    _scout_still_out(
        repository, run_id, dispatched_at=NOW - timedelta(minutes=10), flight=timedelta(minutes=25)
    )
    scheduler.start()

    # 第一轮照常起：这条链路还没有「上一轮」可言。
    scheduler.tick()
    assert launcher.kinds == [MissionKind.PIRATE]

    # 它跑完了、退出码 0（撞上航线上限也是这个码），但一发都没派出去。
    launcher.latest.exit_code = 0
    clock.now = NOW + RESTART_COOLDOWN + timedelta(seconds=1)

    scheduler.tick()

    assert launcher.kinds == [MissionKind.PIRATE]


def test_the_chain_restarts_once_a_line_actually_frees_up(  # type: ignore[no-untyped-def]
    repository, launcher, clock, run_id, session_factory
) -> None:
    """**不许做成永久不起。** 那发侦察一回来，链路就该照常开工。

    等到的是库里查得出来的那个时刻（出发 + 飞行时长 × 2），不是又一段拍脑袋的
    固定间隔。
    """
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    set_config(session_factory, fleet_line_limit=6)
    enable(repository, MissionKind.PIRATE)
    only_gap_filler(repository)
    _scout_still_out(
        repository, run_id, dispatched_at=NOW - timedelta(minutes=10), flight=timedelta(minutes=25)
    )
    scheduler.start()
    scheduler.tick()
    launcher.latest.exit_code = 0

    # 出发后 10 分钟派出，飞 25 分钟、往返 50 分钟 → 航线在 NOW + 40 分钟空出来。
    clock.now = NOW + timedelta(minutes=41)
    scheduler.tick()

    assert launcher.kinds == [MissionKind.PIRATE, MissionKind.PIRATE]


# -- 连续失败自停 --------------------------------------------------------------


def test_three_consecutive_crashes_disable_the_chain(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """没有这条，调度循环会在一个坏掉的任务上变成满速空转的重启循环。"""
    enable(repository, MissionKind.PIRATE)
    only_gap_filler(repository)
    scheduler.start()

    for index in range(MAX_CONSECUTIVE_FAILURES):
        clock.now = NOW + (RESTART_COOLDOWN + timedelta(minutes=1)) * index
        scheduler.tick()
        launcher.latest.exit_code = 1
        scheduler.tick()

    assert task(repository, MissionKind.PIRATE).disabled_reason is not None

    clock.now = NOW + timedelta(days=1)
    scheduler.tick()
    assert len(launcher.spawned) == MAX_CONSECUTIVE_FAILURES


def _crash_scan(scheduler, launcher, clock, *, at: datetime) -> bool:  # type: ignore[no-untyped-def]
    """让扫描在 `at` 起来、14 秒后崩掉。返回它这一趟到底起没起来。

    收退出码的那一 tick **不许顺手再起一个**：那正是「崩了就立刻重来」的样子，
    43 秒连崩三次就是这么来的。
    """
    before = len(launcher.spawned)
    clock.now = at
    scheduler.tick()
    if len(launcher.spawned) == before:
        return False
    launcher.latest.exit_code = 1
    clock.now = at + timedelta(seconds=14)
    scheduler.tick()
    assert len(launcher.spawned) == before + 1, "刚崩完就又起了一个"
    return True


def test_a_burst_of_scan_crashes_does_not_disable_the_chain(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """**实机 2026-08-11 08:40:30 / 08:40:45 / 08:40:59。**

    同一个「游戏窗口抢不到前台」（用户正在用别的窗口）把扫描连崩三次，每次
    14 秒，`consecutive_failures` 到 3，整条链路被停用——而扫描的定位恰恰是
    「始终填空隙」。43 秒里的三次是**同一阵故障**，不是三次独立的证据。

    冷却之后它一趟只起得来一次，所以这 43 秒里只该有一次失败记录。
    """
    only_gap_filler(repository, MissionKind.SCAN)
    disable(repository, MissionKind.PIRATE)
    disable(repository, MissionKind.BOT)
    scheduler.start()

    started = [
        _crash_scan(scheduler, launcher, clock, at=NOW + timedelta(seconds=offset))
        for offset in (0, 15, 29)
    ]

    assert started == [True, False, False]
    assert len(launcher.spawned) == 1
    assert task(repository, MissionKind.SCAN).consecutive_failures == 1
    assert task(repository, MissionKind.SCAN).disabled_reason is None


def test_a_scan_that_keeps_crashing_for_ten_minutes_still_gets_disabled(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """**冷却是节流，不是豁免。** 真坏了还得数到三，否则调度循环会在一个坏掉的
    任务上满速空转——而扫描没有别的闸门拦着它。
    """
    only_gap_filler(repository, MissionKind.SCAN)
    disable(repository, MissionKind.PIRATE)
    disable(repository, MissionKind.BOT)
    scheduler.start()

    # 扫描的冷却从**崩掉那一刻**起算（不是启动那一刻），所以每一轮要多留出
    # 它跑那 14 秒。
    for index in range(MAX_CONSECUTIVE_FAILURES):
        moment = NOW + (RESTART_COOLDOWN + timedelta(seconds=20)) * index
        assert _crash_scan(scheduler, launcher, clock, at=moment)

    assert task(repository, MissionKind.SCAN).disabled_reason is not None


def test_the_environment_busy_code_never_reaches_the_failure_counter(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """runner 说「这会儿轮不到我」时，连撞多少次都不该把链路停用。

    但它照样要吃冷却：用户正在用别的窗口，十几秒后再起一次还是抢不到前台。
    """
    only_gap_filler(repository, MissionKind.SCAN)
    disable(repository, MissionKind.PIRATE)
    disable(repository, MissionKind.BOT)
    scheduler.start()

    # 冷却从崩掉那一刻起算，所以每轮要多留出它跑的那 14 秒。
    for index in range(MAX_CONSECUTIVE_FAILURES + 2):
        clock.now = NOW + (RESTART_COOLDOWN + timedelta(seconds=20)) * index
        scheduler.tick()
        launcher.latest.exit_code = EXIT_ENVIRONMENT_BUSY
        clock.now += timedelta(seconds=14)
        scheduler.tick()

    assert len(launcher.spawned) == MAX_CONSECUTIVE_FAILURES + 2
    assert task(repository, MissionKind.SCAN).consecutive_failures == 0
    assert task(repository, MissionKind.SCAN).disabled_reason is None


def test_the_environment_busy_code_does_not_clear_a_real_failure_streak(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """「轮不到我」不是「跑成功了」。

    当成成功去清零的话，崩一次、轮不到一次、再崩一次……的链路永远数不到三，
    自动停用就等于没有。清零只认退出码 0。
    """
    disable(repository, MissionKind.PIRATE)
    disable(repository, MissionKind.BOT)
    scheduler.start()

    for index, code in enumerate((1, EXIT_ENVIRONMENT_BUSY)):
        clock.now = NOW + (RESTART_COOLDOWN + timedelta(seconds=20)) * index
        scheduler.tick()
        launcher.latest.exit_code = code
        clock.now += timedelta(seconds=14)
        scheduler.tick()

    assert task(repository, MissionKind.SCAN).consecutive_failures == 1


def test_a_clean_round_clears_the_failure_streak(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """「连续」是连续。中间成功过一次，之前那两次就不该再算数。"""
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    for index, code in enumerate((1, 1, 0)):
        clock.now = NOW + (RESTART_COOLDOWN + timedelta(minutes=1)) * index
        scheduler.tick()
        launcher.latest.exit_code = code
        scheduler.tick()

    assert task(repository, MissionKind.PIRATE).consecutive_failures == 0
    assert task(repository, MissionKind.PIRATE).disabled_reason is None


def test_being_preempted_is_not_a_failure(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """抢占是我们自己动的手，扫描本身没毛病。

    算进去的话，一个被频繁抢占的扫描三次就会被自动停用——而它恰恰是唯一
    永远有活干的那条链路。
    """
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    scheduler.tick()  # 起扫描？不一定——海盗优先，所以先让海盗跑完并进入冷却
    launcher.latest.exit_code = 0
    scheduler.tick()  # 收退出码；海盗进冷却，扫描顶上
    scheduler.tick()

    assert launcher.kinds[-1] is MissionKind.SCAN
    clock.now = NOW + RESTART_COOLDOWN + timedelta(seconds=1)
    scheduler.tick()  # 海盗冷却结束，抢占扫描

    assert launcher.kinds[-1] is MissionKind.PIRATE
    assert task(repository, MissionKind.SCAN).consecutive_failures == 0
    preempted = [row for row in repository.mission_runs(limit=10) if row.stopped_by == "PREEMPTED"]
    assert len(preempted) == 1


def test_a_user_stop_is_not_a_failure(scheduler, repository, launcher) -> None:  # type: ignore[no-untyped-def]
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    scheduler.tick()

    scheduler.stop()

    assert task(repository, MissionKind.PIRATE).consecutive_failures == 0


# -- bot 的完成判据 ------------------------------------------------------------


def test_a_target_that_got_its_attack_report_no_longer_counts_as_remaining(  # type: ignore[no-untyped-def]
    repository, launcher, clock, run_id, session_factory
) -> None:
    """范围内每个目标都收到**攻击发**的战报，这一轮就算走完了。"""
    target = Coordinate(2, 150, 5)
    add_bot_target(session_factory, target)
    dispatch_id = dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=target,
        dispatched_at=NOW - timedelta(hours=1),
        preset_name="BBB",
    )
    attach_report(session_factory, dispatch_id, target, NOW - timedelta(minutes=30))
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    scheduler.start()

    scheduler.tick()

    assert launcher.kinds == [MissionKind.SCAN]


def test_a_target_whose_shot_was_a_draw_no_longer_counts_as_remaining(  # type: ignore[no-untyped-def]
    repository, launcher, clock, run_id, session_factory
) -> None:
    """平局的战报说明这一轮**走完了**，和打赢打输一样。

    这条原先叫 `test_a_target_whose_last_shot_was_a_draw_still_counts_as_remaining`，
    钉的是相反的口径（平局 → 还欠一发补刀 → 调度器要再起 bot）。用户口径
    （2026-08-17）：「bot 攻击移除平局再打一次机制」，于是改钉新口径而不是删掉
    ——删掉的话，重打被接回去时调度器这一层没有任何守卫拦得住，而复发的样子是
    这条链路每个平局目标都多烧一条航线。

    ⚠️ 这里验的是**范围模式**（`BOT_RANGE`）。军力优先那一侧走
    `_military_candidates`，同一条判据但另一段代码，守卫是本文件的
    `test_the_military_pool_does_not_re_pick_a_target_that_drew`。
    """
    from evo_helper.domain.battle_outcome import OUTCOME_DRAW

    target = Coordinate(2, 150, 5)
    add_bot_target(session_factory, target)
    dispatch_id = dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=target,
        dispatched_at=NOW - timedelta(hours=1),
        preset_name=BOT_ATTACK_PRESET,
        # 这一发早该回来了。飞行时间不给的话航线钟留空，那一档算「还占着」，
        # 唯一那条航线被压住，验的就不再是「目标还剩几个」了。
        flight=timedelta(minutes=20),
    )
    attach_report(session_factory, dispatch_id, target, NOW - timedelta(minutes=30), OUTCOME_DRAW)
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    only_gap_filler(repository)
    scheduler.start()

    scheduler.tick()

    # 这个范围里就这一个 bot，它走完了 → bot 没有剩余目标，调度器不该再起它。
    assert launcher.kinds == []


# -- 孤儿 ----------------------------------------------------------------------


def test_prepare_marks_orphans_rather_than_shooting_at_a_recycled_pid(  # type: ignore[no-untyped-def]
    repository, launcher, clock
) -> None:
    """pid 会被系统回收复用，照着一个可能已经换了主人的号码开枪比留个警告更糟。"""
    repository.ensure_mission_rows(now_utc=NOW)
    repository.begin_mission_run(
        MissionKind.SCAN,
        task_id=task_id(repository, MissionKind.SCAN),
        command=["python"],
        pid=31337,
        started_at_utc=NOW - timedelta(hours=1),
        log_path="var/logs/mission-scan.log",
    )

    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)

    assert scheduler.prepare() == 1
    assert repository.mission_runs(limit=1)[0].stopped_by == "UNKNOWN"


def test_a_hard_killed_runner_does_not_leave_its_row_hanging(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """用户在任务管理器里把 runner 结束掉（或 `taskkill /F /T`），控制台还活着。

    这一行不能永远挂在「运行中」：挂着的话，页面上那段历史永远显示它在跑，
    而调度器的连续失败也就永远数不到三。收退出码这件事必须由 tick 自己做，
    不能等到有人打开页面才做。
    """
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    scheduler.tick()
    assert repository.mission_runs(limit=1)[0].ended_at_utc is None

    launcher.latest.exit_code = 1  # 被外部强杀，进程没了
    clock.now = NOW + timedelta(seconds=5)
    scheduler.tick()

    closed = next(row for row in repository.mission_runs(limit=10) if row.kind == "PIRATE")
    assert closed.ended_at_utc == NOW + timedelta(seconds=5)
    assert closed.stopped_by == "SELF"


def test_a_row_left_open_by_a_dead_console_is_closed_on_the_next_start(  # type: ignore[no-untyped-def]
    repository, launcher, clock
) -> None:
    """控制台自己被强杀时没人收退出码，那一行会留在库里没有 `ended_at_utc`。

    **「上一轮没能正常收尾」是常态不是意外**：断电、强制重启、任务管理器。
    下一次开机必须认得出并收尾——否则它永远显示成「运行中」，而那恰恰是
    「我们已经不知道它死活了」的意思。
    """
    repository.ensure_mission_rows(now_utc=NOW)
    repository.begin_mission_run(
        MissionKind.PIRATE,
        task_id=task_id(repository, MissionKind.PIRATE),
        command=["python", "-m", "evo_helper.tools.pirate_loop"],
        pid=31337,
        started_at_utc=NOW - timedelta(hours=3),
        log_path="var/logs/mission-pirate.log",
    )

    restarted = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    restarted.prepare()

    row = repository.mission_runs(limit=1)[0]
    assert row.ended_at_utc == NOW
    assert row.stopped_by == "UNKNOWN"
    assert repository.open_mission_runs() == []


def test_prepare_seeds_every_chain_and_the_config(repository, launcher, clock) -> None:  # type: ignore[no-untyped-def]
    """迁移里没有 `bulk_insert`，这几行得有人保证存在。"""
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)

    scheduler.prepare()

    assert len(repository.mission_tasks()) == len(MissionKind)
    assert repository.scheduler_config().pirate_daily_quota == 32


# -- 跑着不动 ------------------------------------------------------------------
#
# **实机 `var/logs/overnight-0812.log` 最后 1.5 小时**（心跳每半小时一行）：
#
#     05:14:51 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...
#     06:45:59 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...
#
# 六次心跳、七个计数一个没变，而状态一直是「运行中」。调度器只知道子进程还活着。
# 判据本身在 `tests/unit/application/test_mission_progress.py` 钉死，这里守的是
# **接线**：判死之后有没有真的把子进程收掉、有没有落进台账、算不算故障。


class _Counts:
    """一份可以现改的进展读数，替掉真的去数那四张表。"""

    def __init__(self) -> None:
        self.reading = ProgressReading(
            dispatches=580, battle_reports=92, scout_reports=84, coordinate_scans=4536
        )

    def read(self) -> ProgressReading:
        return self.reading

    def moved(self) -> None:
        self.reading = ProgressReading(
            dispatches=self.reading.dispatches + 1,
            battle_reports=self.reading.battle_reports,
            scout_reports=self.reading.scout_reports,
            coordinate_scans=self.reading.coordinate_scans,
        )


def _watched(repository, launcher, clock):  # type: ignore[no-untyped-def]
    counts = _Counts()
    scheduler = MissionScheduler(
        repository, make_supervisor(launcher, clock), clock=clock, progress=counts
    )
    scheduler.prepare()
    return scheduler, counts


def test_a_round_that_stops_producing_anything_is_cut_off(  # type: ignore[no-untyped-def]
    repository, launcher, clock
) -> None:
    """**这条就是那一个半小时。**

    子进程活着、日志照打，但库里一行都没多出来。原先没有任何超时把它掐掉。
    """
    scheduler, _counts = _watched(repository, launcher, clock)
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    scheduler.tick()
    stalled = launcher.latest

    clock.now = NOW + STALL_TIMEOUT
    scheduler.tick()

    assert stalled.terminated
    closed = next(row for row in repository.mission_runs(limit=10) if row.ended_at_utc is not None)
    assert closed.stopped_by == "STALLED"
    assert closed.ended_at_utc == NOW + STALL_TIMEOUT


def test_a_round_that_keeps_working_is_never_cut_off(  # type: ignore[no-untyped-def]
    repository, launcher, clock
) -> None:
    """**方向相反的另一半，同样要紧。**

    一轮里合法的长等待是存在的（侦察报告等 45 秒、翻一趟信箱实测 83 秒），
    误杀丢的是真实的舰队和当日配额。这里让它每隔一会儿就多派一发。
    """
    scheduler, counts = _watched(repository, launcher, clock)
    enable(repository, MissionKind.PIRATE)
    scheduler.start()
    scheduler.tick()
    running = launcher.latest

    for minutes in range(1, int(STALL_TIMEOUT.total_seconds() // 60) * 3, 10):
        counts.moved()
        clock.now = NOW + timedelta(minutes=minutes)
        scheduler.tick()

    assert not running.terminated
    assert len(launcher.spawned) == 1


def test_a_stalled_round_is_charged_as_a_failure(  # type: ignore[no-untyped-def]
    repository, launcher, clock
) -> None:
    """手是我们动的，毛病却是这条链路自己的。

    不算故障的话，同一个卡死会一轮接一轮地复现，每轮白烧一个阈值那么久。
    """
    scheduler, _counts = _watched(repository, launcher, clock)
    enable(repository, MissionKind.PIRATE)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    clock.now = NOW + STALL_TIMEOUT
    scheduler.tick()

    assert task(repository, MissionKind.PIRATE).consecutive_failures == 1


# -- 环境故障：多条链路一起倒，不记到任何一条头上 ------------------------------
#
# **实机 2026-08-12。** 01:55「BOT 已停用（连续 3 次异常退出，退出码 1）」，
# 04:37 三条**全部**已停用。BOT 从 01:55 停到 04:37，近三个小时一发没派。
# 三条链路共用一个游戏窗口、一个鼠标、一份连接和一台机器；同时坏掉几乎必然是
# 那些共用的东西坏了，而不是三处互不相干的代码在同一晚一起长出 bug。


def _launch(scheduler, launcher, clock, *, at: datetime, expect: MissionKind) -> None:  # type: ignore[no-untyped-def]
    before = len(launcher.spawned)
    clock.now = at
    scheduler.tick()
    assert len(launcher.spawned) == before + 1, f"{at} 这一刻没有起任何东西"
    assert launcher.latest.kind is expect


def _crash(scheduler, launcher, clock, *, at: datetime, expect: MissionKind) -> None:  # type: ignore[no-untyped-def]
    """让**正在跑的**那一个在 `at` 以退出码 1 崩掉，并钉住它确实是 `expect`。

    收退出码那一 tick 顺带会把顺位让给下一条链路（前一条进了冷却）——那正是
    环境坏掉时三条链路接连倒下的样子，所以这里不去拦它。
    """
    assert launcher.latest.kind is expect, f"{at} 在跑的是 {launcher.latest.kind}"
    launcher.latest.exit_code = 1
    clock.now = at
    scheduler.tick()


def _crash_whatever_runs(scheduler, launcher, clock, *, at: datetime, limit: int = 6) -> None:  # type: ignore[no-untyped-def]
    """从 `at` 起，把这一阵里起得来的链路挨个崩掉。

    不指定是谁：链路被逐个停用之后，谁还起得来是会变的，写死顺序只会让用例在
    「停用生效」的那一刻假红。
    """
    moment = at
    for _ in range(limit):
        if scheduler.current is None:
            before = len(launcher.spawned)
            clock.now = moment
            scheduler.tick()
            if len(launcher.spawned) == before:
                return
        launcher.latest.exit_code = 1
        moment += timedelta(seconds=14)
        clock.now = moment
        scheduler.tick()


def test_two_chains_crashing_together_are_not_charged_to_either(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """一起倒 = 环境坏了，两条的连续失败计数都要清干净。

    原先这两次会各记一笔，撞满三次就把两条链路都自动停用——而环境早就好了。
    """
    enable(repository, MissionKind.PIRATE)
    disable(repository, MissionKind.BOT)
    scheduler.start()

    _launch(scheduler, launcher, clock, at=NOW, expect=MissionKind.PIRATE)
    # 海盗崩了就进冷却，顺位当场让给扫描；两次崩塌相隔不到一分钟。
    _crash(scheduler, launcher, clock, at=NOW + timedelta(seconds=14), expect=MissionKind.PIRATE)
    _crash(scheduler, launcher, clock, at=NOW + timedelta(seconds=30), expect=MissionKind.SCAN)

    assert task(repository, MissionKind.PIRATE).consecutive_failures == 0
    assert task(repository, MissionKind.SCAN).consecutive_failures == 0
    assert task(repository, MissionKind.PIRATE).disabled_reason is None
    assert task(repository, MissionKind.SCAN).disabled_reason is None


def test_a_chain_failing_on_its_own_is_still_charged(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """**豁免不能退化成「所有失败都不算失败」。**

    只有它一条在倒的时候，那就是它自己的毛病，照记。没有这条，自动停用整个失效，
    调度循环会在一个坏掉的任务上一轮轮空转。
    """
    enable(repository, MissionKind.PIRATE)
    disable(repository, MissionKind.BOT)
    only_gap_filler(repository)
    scheduler.start()

    for index in range(MAX_CONSECUTIVE_FAILURES):
        at = NOW + (RESTART_COOLDOWN + timedelta(minutes=1)) * index
        _launch(scheduler, launcher, clock, at=at, expect=MissionKind.PIRATE)
        _crash(scheduler, launcher, clock, at=at + timedelta(seconds=14), expect=MissionKind.PIRATE)

    assert task(repository, MissionKind.PIRATE).disabled_reason is not None


def test_the_environment_busy_code_never_corroborates_a_real_crash(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """**「轮不到我」不能拿去佐证别人的崩溃。**

    `EXIT_ENVIRONMENT_BUSY` 本来就不计失败（用户正在用别的窗口）。把它也算进
    「一起倒」的证据里，等于让最常见的一档正常情况变成万能豁免——真坏了的那条
    链路从此永远数不到三。
    """
    enable(repository, MissionKind.PIRATE)
    disable(repository, MissionKind.BOT)
    scheduler.start()

    _launch(scheduler, launcher, clock, at=NOW, expect=MissionKind.PIRATE)
    launcher.latest.exit_code = EXIT_ENVIRONMENT_BUSY
    clock.now = NOW + timedelta(seconds=14)
    scheduler.tick()
    # 扫描顶上，然后真的崩了——只有它一条在倒。
    _crash(scheduler, launcher, clock, at=NOW + timedelta(seconds=30), expect=MissionKind.SCAN)

    assert task(repository, MissionKind.SCAN).consecutive_failures == 1


def test_the_exemption_runs_out_when_nothing_ever_runs_clean(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """**豁免必须有尽头。**

    两条各自高频复发的真故障会一直互相佐证，判据永远说「像是环境坏了」——那就
    退回到「一个坏掉的任务上空转」，正是自动停用当初要防的。所以豁免按
    `MAX_ENVIRONMENT_EXEMPTIONS` 记账：一次都跑不通的话它会用尽，停用照旧生效，
    只是来得晚一些（约 45 分钟，而不是原先的约 10 分钟）。
    """
    enable(repository, MissionKind.PIRATE)
    disable(repository, MissionKind.BOT)
    scheduler.start()

    at = NOW
    # 圈数上限**写死**，好让「永远停不掉」这种回归表现成断言失败而不是死循环。
    # ⚠️ 不许写成由 `MAX_ENVIRONMENT_EXEMPTIONS` 算出来的数：变异验证要把那个常量
    # 改成一个很大的值，跟着算的话这个循环会先跑上几十亿圈——用例不是变红，是挂死。
    for _round in range(40):
        if task(repository, MissionKind.PIRATE).disabled_reason is not None:
            break
        _crash_whatever_runs(scheduler, launcher, clock, at=at)
        at += RESTART_COOLDOWN + timedelta(minutes=1)
    else:  # pragma: no cover - 只有回归时才走到
        raise AssertionError("一直没跑通，链路却永远没被停用：豁免成了无限的")

    assert task(repository, MissionKind.PIRATE).disabled_reason is not None
    # 而且它确实比「连撞三次」宽得多——否则这条豁免等于没做。
    assert len(launcher.spawned) > MAX_CONSECUTIVE_FAILURES * 2


def test_one_clean_round_hands_the_whole_exemption_budget_back(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """任何一条链路跑出一次退出码 0，就证明环境是好的：窗口在、会话在、鼠标是我们的。

    那一刻之前的几次豁免各自成立，不该再占着谁的额度——否则一台偶尔掉一次线的
    机器，跑上几天照样会把额度耗光，然后又回到「环境一抖就停用一条链路」。
    """
    only_gap_filler(repository, MissionKind.SCAN)
    enable(repository, MissionKind.PIRATE)
    disable(repository, MissionKind.BOT)
    scheduler.start()

    at = NOW
    for _round in range(MAX_ENVIRONMENT_EXEMPTIONS):
        _launch(scheduler, launcher, clock, at=at, expect=MissionKind.PIRATE)
        _crash(scheduler, launcher, clock, at=at + timedelta(seconds=14), expect=MissionKind.PIRATE)
        _crash(scheduler, launcher, clock, at=at + timedelta(seconds=30), expect=MissionKind.SCAN)
        at += RESTART_COOLDOWN + timedelta(minutes=1)

    # 一轮干净的收尾。
    _launch(scheduler, launcher, clock, at=at, expect=MissionKind.PIRATE)
    launcher.latest.exit_code = 0
    clock.now = at + timedelta(minutes=2)
    scheduler.tick()

    at += RESTART_COOLDOWN + timedelta(minutes=10)
    _launch(scheduler, launcher, clock, at=at, expect=MissionKind.PIRATE)
    _crash(scheduler, launcher, clock, at=at + timedelta(seconds=14), expect=MissionKind.PIRATE)
    _crash(scheduler, launcher, clock, at=at + timedelta(seconds=30), expect=MissionKind.SCAN)

    assert task(repository, MissionKind.PIRATE).consecutive_failures == 0
    assert task(repository, MissionKind.SCAN).consecutive_failures == 0


# -- 多个 bot 任务：各自的出发星球、各自的航线账 --------------------------------
#
# 用户口径（2026-08-13）：「可能会新增多个同一个类型的任务，比如 2 个 bot 攻击，
# 从主星出发 5 条航线，从 2 号线出发 2 条航线」。追问确认：**航线上限是按星球各
# 一份的**，只有 bot 攻击需要多任务。
#
# 这一段验的是**接线**：判据本身在 `tests/unit/domain/test_scheduler.py` 里已经
# 钉死，这里守的是「事实有没有按出发星球分组读出来」。

SECOND_PLANET = Coordinate(9, 250, 8)


def _second_bot_task(  # type: ignore[no-untyped-def]
    repository,
    *,
    origin: Coordinate = SECOND_PLANET,
    fleet_lines: int = 2,
    params_json: str = BOT_RANGE,
) -> int:
    """再建一个 bot 任务，启用并填好范围。返回它的 id。"""
    new_id = repository.create_mission_task(
        MissionKind.BOT,
        name="2 号星",
        priority=5,
        params_json=params_json,
        origin=origin,
        fleet_lines=fleet_lines,
        now_utc=NOW,
    )
    repository.update_mission_task(new_id, enabled=True)
    return new_id


def _free_lines(scheduler, wanted: int) -> int:  # type: ignore[no-untyped-def]
    return scheduler.snapshot().facts.per_task[wanted].free_lines


def test_each_bot_task_gets_the_lines_of_its_own_planet(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id
) -> None:
    """**不同出发星球互不影响。**

    主星那个任务配 5 条、已经派满 5 支；2 号星那个配 2 条、一支都没派。
    全库一起数的话两边都会看到「5 支在飞」，于是 2 号星那个也不敢派了——
    而它那颗星球上一条航线都没被占。

    ⚠️ 两个任务的航线数**故意不同**（5 与 2）：填成一样的话，把出发星球那道
    过滤整个删掉也未必露馅。
    """
    add_bot_target(session_factory, Coordinate(2, 150, 5))
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    main = task_id(repository, MissionKind.BOT)
    repository.update_mission_task(main, fleet_lines=5)
    second = _second_bot_task(repository)
    for index in range(5):
        # 每发错开一分钟：意图按 (run_id, 目标, cycle_start) 去重，同一刻的五发
        # 会被当成同一发挡回来。
        moment = NOW - timedelta(minutes=5 + index)
        dispatch(
            repository,
            run_id,
            TARGET_KIND_BOT,
            target=Coordinate(2, 150, 5),
            dispatched_at=moment,
            flight=timedelta(hours=1),
        )

    assert _free_lines(scheduler, main) == 0
    assert _free_lines(scheduler, second) == 2


def test_two_bot_tasks_on_the_same_planet_share_that_planets_lines(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id
) -> None:
    """反过来的一半：**同一颗星球上**的在飞数两个任务都要看得见。

    只有这一条成立，「按星球各一份」才不是「谁也不管谁」——同一颗星球上两个
    任务抢的确实是同一批位子。
    """
    add_bot_target(session_factory, Coordinate(2, 150, 5))
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    main = task_id(repository, MissionKind.BOT)
    repository.update_mission_task(main, fleet_lines=3)
    # 同一颗主星，只是范围不同。
    same_planet = _second_bot_task(repository, origin=Coordinate(2, 137, 18), fleet_lines=3)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(2, 150, 5),
        dispatched_at=NOW - timedelta(minutes=5),
        flight=timedelta(hours=1),
    )

    assert _free_lines(scheduler, main) == 2
    assert _free_lines(scheduler, same_planet) == 2


def test_a_second_bot_task_keeps_its_own_round(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """**「重开一轮」只推这一个任务的轮。**

    两个 bot 任务各打各的范围，一起推等于把另一个还没打完的那一轮也归零：
    它已经收到的战报会被当成上一轮的，目标全部重来。
    """
    add_bot_target(session_factory, Coordinate(2, 150, 5))
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    main = task_id(repository, MissionKind.BOT)
    second = _second_bot_task(repository)

    scheduler.begin_bot_round(second)

    rows = {row.id: row for row in repository.mission_tasks()}
    assert rows[second].round_started_at_utc == NOW
    assert rows[main].round_started_at_utc is None


def test_a_task_on_another_planet_is_now_dispatched_with_its_own_origin(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, launcher
) -> None:
    """**配在别的星球上的任务现在派得出去了。**

    这条用例原先钉的是反面：助手还不会切星球，所以 9:250:8 会被
    `check_origin_dispatchable` 当场拒掉、任务被停用。那道临时闸门随「切换星球」
    实装一起删了——runner 开工时会真的把当前星球切过去
    （`tools.pirate_loop.ensure_origin_planet`），切不成就一发都不派并报
    `EXIT_ENVIRONMENT_BUSY`。

    所以现在要钉的是：**任务照常起得来，而且 `--origin` 带的是它自己那颗**。
    退回到「拒掉」的话，多出发星球这整件事就等于没做。
    """
    add_bot_target(session_factory, Coordinate(2, 150, 5))
    only_gap_filler(repository)
    disable(repository, MissionKind.BOT)
    second = _second_bot_task(repository)
    scheduler.start()

    scheduler.tick()

    row = {item.id: item for item in repository.mission_tasks()}[second]
    assert row.disabled_reason is None
    assert launcher.spawned, "配在 9:250:8 上的任务应该照常起得来"
    command = list(launcher.latest.command)
    assert command[command.index("--origin") + 1] == "9:250:8"


def test_the_bot_command_carries_the_task_origin(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """`--origin` 必须原样出现在 argv 里。

    漏掉的话 runner 会回落到 `EVO_HELPER_ORIGIN`，于是两个任务写进
    `attack_intents` 的出发坐标可能是同一颗——而多任务的整个记账就建立在
    这一个坐标上。
    """
    add_bot_target(session_factory, Coordinate(2, 150, 5))
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert command[command.index("--origin") + 1] == "2:137:18"


def test_a_run_records_which_task_started_it(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """台账要记得住是**哪一个任务**跑的那一轮。

    只记 `kind` 的话，两个 bot 任务的历史混成一片，而重启冷却正是按任务算的
    ——认不出人就等于那个任务永远没有冷却记录。
    """
    add_bot_target(session_factory, Coordinate(2, 150, 5))
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert repository.mission_runs(limit=1)[0].task_id == task_id(repository, MissionKind.BOT)


# -- 军力优先那一支 ------------------------------------------------------------

BOT_BY_MILITARY = '{"by_military": true, "top_n": 2}'


def test_military_ranking_batch_finishes_before_its_bot_attack_is_started(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """榜单刚写出一屏候选时不能被抢；采满后 bot 也不能让给下一条任务。"""
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY)
    only_gap_filler(repository, MissionKind.RANKING)
    scheduler.start()

    scheduler.tick()
    assert launcher.kinds == [MissionKind.RANKING]
    assert launcher.latest.command[-2:] == ["--bot-limit", "2"]

    # 榜单采集尚未结束，即便第一屏已写出了候选，也必须继续采到配置的 2 个。
    add_bot_target(session_factory, Coordinate(2, 140, 5), military_score=9_000.0)
    scheduler.tick()
    assert launcher.kinds == [MissionKind.RANKING]

    launcher.latest.exit_code = 0
    add_bot_target(session_factory, Coordinate(2, 141, 6), military_score=8_000.0)
    enable(repository, MissionKind.PIRATE, params_json='{"radius": 10}', priority=-1)
    scheduler.tick()

    assert launcher.kinds == [MissionKind.RANKING, MissionKind.BOT]
    assert "2:140:5=BBB" in launcher.latest.command


def test_a_ranking_run_without_an_exit_code_is_never_read_as_a_full_batch(  # type: ignore[no-untyped-def]
    scheduler, repository
) -> None:
    """⚠️ **`exit_code is None` 必须落在「没采满」那一侧。**

    手动停掉的那几档现在一律记 None（`MissionSupervisor.stop`：`terminate()` 之后
    拿到的那个码是内核参数，不是 runner 的表态）。判据只认一件事：**只有 runner
    自己报的 0 才算采满**。写成 `(exit_code or 0) != 0` 之类「None 当 0 看」的形状，
    就等于把一趟半截的榜单当成采满了，接着照它去派攻击。

    两个 `stopped_by` 都要钉：`SELF` 那一支单独钉住第二个子句，否则
    「非 SELF」那半句会替它把用例撑过去，坑还在。
    """
    ranking = task_id(repository, MissionKind.RANKING)
    for stopped_by in (StopReason.SELF, StopReason.USER):
        scheduler._military_ranking_batch_task_id = ranking

        scheduler._finish(
            MissionExit(
                task_id=ranking,
                kind=MissionKind.RANKING,
                command=(),
                exit_code=None,
                stopped_by=stopped_by,
                started_at_utc=NOW,
                ended_at_utc=NOW,
            )
        )

        assert scheduler._military_ranking_batch_task_id is None, stopped_by


def test_the_military_pool_dispatches_the_best_value_first(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **判据 2026-08-18 换成了 `军力 ÷ 往返小时`，这条用例跟着换了名字。**

    从前它叫 `..._takes_the_strongest_then_orders_them_by_distance`，钉的是
    「先按军力截断，再按距离排」那两步。现在只有一条判据，三个目标的得分
    （从 `2:137` 出发）是：

    | 目标 | 军力 | 往返小时 | 得分 |
    |---|---|---|---|
    | `2:140` | 8,000 | 0.523 | **15,284** |
    | `2:400` | 9,000 | 1.371 | 6,565 |
    | `2:150` | 100 | 0.586 | 171 |

    这组夹具只留一条空航线，所以派出去的就是得分最高的 `2:140`——**答案没变，
    理由变了**：从前是「9000/8000 进池、池内近的先打」，现在是「近而略弱的
    那一发本来就更划算」。
    """
    add_bot_target(session_factory, Coordinate(2, 400, 5), military_score=9_000.0)
    add_bot_target(session_factory, Coordinate(2, 140, 6), military_score=8_000.0)
    add_bot_target(session_factory, Coordinate(2, 150, 7), military_score=100.0)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    # ⚠️ 按位置取而不是按「像坐标」筛：`--origin 2:137:18` 也是三段坐标，
    # 用形状过滤会把它一起捞进来（我第一版就是这么写错的）。
    command = launcher.latest.command
    targets = command[command.index("--targets") + 1 : command.index("--origin")]
    # 这组夹具只留一条空航线；军力 runner 会先取池中得分最高的那颗，并把实际使用的
    # 预设记进命令行，不能再把 `=BBB` 当成坐标的一部分丢掉。
    assert targets == ["2:140:6=BBB"]


def test_military_attack_never_selects_fixed_pirate_positions(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """旧数据即便还标着 bot，1--4 号位也必须在派遣前被硬拦住。"""
    add_bot_target(session_factory, Coordinate(2, 140, 1), military_score=9_000.0)
    add_bot_target(session_factory, Coordinate(2, 141, 5), military_score=8_000.0)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY)
    only_gap_filler(repository)
    scheduler.start()

    scheduler.tick()

    command = launcher.latest.command
    assert "2:141:5=BBB" in command
    assert not any(part.startswith("2:140:1") for part in command)


def test_the_military_pool_ignores_the_system_range(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **军力优先那一支不过恒星系区间。**

    军力榜是全宇宙的，而区间是给「区域攻击」那一支用的。两个一起用等于把
    「打最强的」悄悄降级成「打这个区域里最强的」——而最强的那批本来就散落在
    别的银河（实测：>100K 的那批横跨 3/5/6/7/8/9 系）。
    """
    add_bot_target(session_factory, Coordinate(7, 99, 7), military_score=9_000.0)
    enable(
        repository,
        MissionKind.BOT,
        params_json='{"by_military": true, "galaxy": 2, "first_system": 60, "last_system": 499}',
    )
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert "7:99:7=BBB" in launcher.latest.command


def test_military_pool_skips_targets_attacked_within_the_last_24_hours(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, run_id
) -> None:
    """24 小时过滤发生在取前 N 名之前，不能让已打过的强目标反复占住候选池。"""
    already_attacked = Coordinate(2, 140, 5)
    still_available = Coordinate(2, 141, 6)
    add_bot_target(session_factory, already_attacked, military_score=9_000.0)
    add_bot_target(session_factory, still_available, military_score=8_000.0)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=already_attacked,
        dispatched_at=NOW - timedelta(hours=23),
        flight=timedelta(minutes=1),
    )
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert "2:141:6=BBB" in command
    assert not any(part.startswith("2:140:5") for part in command)


def test_the_military_pool_does_not_re_pick_a_target_that_drew(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, run_id
) -> None:
    """**军力优先这一侧也不再补刀平局。**

    用户口径（2026-08-17）：「bot 攻击移除平局再打一次机制」。范围模式那一条在
    `test_a_target_whose_shot_was_a_draw_no_longer_counts_as_remaining`，
    这一条守的是另一半——两种模式共用 `domain.bot_round.phase_of`，但各有各的
    候选池代码（`_bot_remaining` / `_military_candidates`），只验一边会漏。

    ⚠️ **那一发刻意放在 24 小时以外。** 24 小时内的目标本来就被
    `attacked_bot_targets_since` 挡掉，无论平局与否——拿那种目标来验，
    这条用例在旧规则下也是绿的，什么都守不住。放到 25 小时以外，
    唯一还拦得住它的就只剩 `phase_of` 那一条。
    """
    from evo_helper.domain.battle_outcome import OUTCOME_DRAW

    drew = Coordinate(2, 140, 5)
    untouched = Coordinate(2, 141, 6)
    add_bot_target(session_factory, drew, military_score=9_000.0)
    add_bot_target(session_factory, untouched, military_score=8_000.0)
    dispatch_id = dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=drew,
        dispatched_at=NOW - timedelta(hours=25),
        preset_name=BOT_ATTACK_PRESET,
        flight=timedelta(minutes=1),
    )
    attach_report(session_factory, dispatch_id, drew, NOW - timedelta(hours=24), OUTCOME_DRAW)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert "2:141:6=BBB" in command
    assert not any(part.startswith("2:140:5") for part in command)


def test_without_the_switch_the_chain_still_attacks_by_region(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **默认必须是老的区域攻击。**

    悄悄换掉一条已经在跑的链路的选靶口径，比多一个开关危险得多——用户圈的
    那个范围是他自己配的，而军力优先会把目标散到全宇宙。
    """
    add_bot_target(session_factory, Coordinate(7, 99, 7), military_score=9_000.0)
    add_bot_target(session_factory, Coordinate(2, 150, 5), military_score=100.0)
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert "2:150:5" in command
    assert "7:99:7" not in command, "没开开关就不许跨出区间"


def test_the_cap_keeps_the_unbeatable_ones_out_of_the_pool(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """上限只挡「太强」：1.77M 那个进不来，普通的那个照打。

    ⚠️ 顺带记一笔：这个上限**目前是空转的**（用户口径 2026-08-17，bot 最高战力约
    70K）。这条用例仍然有价值——哪天 bot 变强、上限真的生效时，它就是那道护栏。

    ⚠️ **这条用例 2026-08-18 改过。** 它从前叫
    `test_a_target_with_no_score_still_gets_into_the_pool_under_a_cap`，钉的是
    「上限不许连 `military_score is None` 一起扔掉」——那时没有分数的目标走补位、
    照打不误。现在它们在第 2 步就出局了（用户当日决定，见 `domain.target_order`），
    那个判据在这一层已经无从观测：把 None 那颗放进来的话，它无论如何都不会出现在
    命令行里，用例反而钉不住上限本身。「上限不挡 None」这条规矩仍然活着，钉在
    `tests/unit/domain/test_military_attack.py::test_pool_drops_over_cap_but_keeps_unknown_score`。
    """
    add_bot_target(session_factory, Coordinate(2, 140, 5), military_score=9_000.0)
    add_bot_target(session_factory, Coordinate(2, 141, 6), military_score=1_773_000.0)
    enable(
        repository,
        MissionKind.BOT,
        params_json='{"by_military": true, "top_n": 50, "max_score": 100000}',
    )
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert "2:140:5=BBB" in command
    assert not any(part.startswith("2:141:6") for part in command)


# -- 有效期窗口、窗口门限、以及「放宽窗口」那一档 -----------------------------
#
# 用户口径（2026-08-18）敲定的四步：① 剔除 24h 内打过的 → ② 只留有军力读数的
# → ③ 只留读数在有效期窗口内的（不够就放弃窗口并告警）→ ④ 过军力上限这道安全线，
# 按 `军力 ÷ 往返小时` 降序出击。
#
# ⚠️ **这一节 2026-08-18 整段重写过三次。**
#
# 第一次（2026-08-17 那一版 → PR #176）：那时第 3 步是硬判据「分数过期的整批跳过」，
# 失败方式是「一个新鲜分数都没有时军力完全不参与选靶」——实机连续停摆 2.5 小时。
#
# 第二次（PR #176 → 窗口版）：#176 把第 3 步换成了「按读数时间取前 N 个」
# ＝时间池。**那一步也是错的，而且错得更隐蔽**：军力榜从强到弱扫，「读数最新」
# 系统性地等价于「军力最弱」，于是「军力优先」选出的是全库最弱的一批（实机
# 2026-08-18 09:00 那 8 发只有 3.2K~5.7K；生产实测分段表在 `domain.target_order`
# 模块头第 3 步）。第 3 步因此换成按有效期**划线**——划线不带选择偏差。
#
# 第三次（→ 现在）：旧的第 4、5 步是「窗口内按军力硬截断前 `top_n` 名」＋
# 「这批人按距离由近到远出击」。两步各自说得通，合起来说不清：「第 101 名一个都
# 不打」与「第 1 名和第 100 名之间只按远近分先后」互相矛盾，而它们之间那道墙纯粹
# 是拍出来的。**现在合成一条判据**：`军力 ÷ 往返小时`。`top_n` 保留，但只剩
# 「窗口门限」这一个身份（第 3 步的尺子），**不再决定打谁**。
#
# ⚠️ 分子用军力，依据是**用户口径**（2026-08-18：「已知军力和材料产出正相关，
# 但是没有具体数据来拟合相关曲线」），不是实测的材料产出。整段在
# `domain.target_order` 模块头第 4 步。

BOT_BY_MILITARY_2H = '{"by_military": true, "top_n": 2, "score_max_age_hours": 2}'


def _targets_of(command: list[str]) -> list[str]:
    """命令行里 `--targets` 那一段。

    ⚠️ 按位置取而不是按「像坐标」筛：`--origin 2:137:18` 也是三段坐标，
    用形状过滤会把它一起捞进来。
    """
    return command[command.index("--targets") + 1 : command.index("--origin")]


def test_excluding_the_last_24_hours_never_collapses_the_pool(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, run_id
) -> None:
    """⚠️ **用例 (f)：第 1 步必须在最前，而这一条钉的正是「在最前」这件事本身。**

    24 小时内打过的那个（`2:140`，军力 9000）**同时是得分最高的**：它既更强又更近，
    得分 15,284 对 `2:141` 的 13,415。这组夹具只留一条空航线，所以把剔除挪到得分
    排序之后，派出去的就会是那个刚打过的目标——而 `8000` 那个从头到尾没机会，
    它本该是这一轮唯一该打的。

    `test_military_pool_skips_targets_attacked_within_the_last_24_hours` 守不住
    这一点：那条的航线预算放得下两个，先剔后排和先排后剔的结果一样。

    ⚠️ **这条用例改过两次，判据一次都没变。** 2026-08-18 早先删掉了
    `military_time_pool=1`（那个旋钮随「时间池」那个错误设计一起没了）；
    同日又把「军力截断只留 1 个」这个理由换成了「它同时是得分最高的」——
    截断取消之后，逼出差别的那件事从「名额只有一个」变成了「航线只有一条」。
    """
    already_attacked = Coordinate(2, 140, 5)
    still_available = Coordinate(2, 141, 6)
    add_bot_target(session_factory, already_attacked, military_score=9_000.0, scanned_at=NOW)
    add_bot_target(
        session_factory,
        still_available,
        military_score=8_000.0,
        scanned_at=NOW - timedelta(hours=1),
    )
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=already_attacked,
        dispatched_at=NOW - timedelta(hours=23),
        flight=timedelta(minutes=1),
    )
    enable(repository, MissionKind.BOT, params_json='{"by_military": true, "top_n": 1}')
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert launcher.kinds == [MissionKind.BOT], "剔除必须在最前，否则派的是刚打过的那个"
    assert _targets_of(launcher.latest.command) == ["2:141:6=BBB"]


def test_a_target_that_never_made_the_board_is_not_attacked(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **第 2 步：从没上过军力榜的目标不再攻击**（用户 2026-08-18 决定）。

    ⚠️ **这条用例是对 `test_a_target_never_seen_on_the_board_still_fills_a_seat`
    的整个翻转。** 那一条钉的是「没有分数的照打，只是排在主力后面（按距离补位）」，
    依据是一句错话——「没被榜单扫到过的正是库里最多的一批」，那个数把非 bot 的行
    也算进了分母。实测 628 个，占 bot 总数（3604）的 17.4%。放弃这 17.4% 换来的是
    「军力优先」真的成立：补位一多，这条链路就退化成「按距离随便打」。

    这里没有分数的那个**就在出发星球隔壁**（`2:138`，往返只要 0.51 小时），一旦它
    进得了池而又被当成 0 分之外的任何数，得分排序都会让它第一个被派出去——
    所以「它没出现在命令行里」是一句很强的断言。
    """
    add_bot_target(session_factory, Coordinate(2, 138, 9), military_score=None, scanned_at=None)
    add_bot_target(session_factory, Coordinate(2, 400, 5), military_score=9_000.0, scanned_at=NOW)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert _targets_of(launcher.latest.command) == ["2:400:5=BBB"]


def test_a_pool_where_everything_expired_still_attacks(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **2026-08-17 那晚的复现：全部超期照样出兵。**

    ⚠️ **这条用例取代了 `test_a_target_whose_score_expired_is_not_attacked`。**
    那一条钉的是「分数过期的那个再强也不打」——旧规格。旧规格的代价在实机上
    量到了：当晚一个新鲜分数都没有，闸门于是把**全部**目标滤掉，攻击停摆 2.5 小时；
    而更早那次（`4:293:6` 顶着 3.6 小时前的读数被打出去）真正的害处只是「排序不准」，
    不是「打不动」——用户口径 2026-08-17：bot 最高战力只有 70 多 K，离打不动还很远。
    所以新规格不让它们把整轮拖死。

    ⚠️ **机制 2026-08-18 又换了两次，判据没换。** PR #176 靠「时间池永远拿得出
    最新的 N 个」保证这一点，而那个池带选择偏差（见本节开头）。现在靠的是
    **窗口内不足就放弃窗口**：全都超期时窗口是空的，于是全部有读数的目标都进池
    ——同样不空手。

    这里三个目标的分数全都超期（3 天 ≫ 配的 2 小时）。2026-08-17 那一版下一发都
    派不出去；现在放弃窗口，按得分出击（从 `2:137` 出发）：

    | 目标 | 军力 | 往返小时 | 得分 |
    |---|---|---|---|
    | `2:140` | 8,000 | 0.523 | **15,284** |
    | `2:400` | 9,000 | 1.371 | 6,565 |
    | `2:150` | 100 | 0.586 | 171 |
    """
    three_days = NOW - timedelta(days=3)
    for coordinate, score in (
        (Coordinate(2, 400, 5), 9_000.0),
        (Coordinate(2, 140, 6), 8_000.0),
        (Coordinate(2, 150, 7), 100.0),
    ):
        add_bot_target(session_factory, coordinate, military_score=score, scanned_at=three_days)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert launcher.kinds == [MissionKind.BOT], "全都超期不该让这一轮空手"
    # 这组夹具只留一条空航线，所以派出去的就是得分最高的 `2:140`。
    assert _targets_of(launcher.latest.command) == ["2:140:6=BBB"]


def test_a_target_outside_the_window_stays_out_however_strong_it_is(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **用例 (a)：窗口内够用时，窗口外的再强也不进。**

    ⚠️ **这条用例取代了 `test_the_time_pool_takes_the_newest_readings_not_the_strongest`
    与 `test_the_military_cut_only_looks_inside_the_time_pool`。** 那两条钉的是
    「按读数时间取前 N 个」，而那一步整个是错的：军力榜从强到弱扫，「读数最新」
    系统性地等价于「军力最弱」，所以那一步实际上是一道**反向的军力截断**。
    它们确实也会挡住 `2:400`，但靠的是错误的理由——留着它们等于把那个错误当成
    规格钉住，而实机 2026-08-18 已经量到那个规格选出了全库最弱的一批。

    这里 `2:400` 军力最高（99999），读数却是三天前的；窗口内还剩 2 个，够**窗口
    门限**（1 个）用，所以窗口不必放弃，它进不来。这组夹具只留一条空航线，
    所以命令行里那一个就是得分最高的：`2:401`（8000 ÷ 1.368h ≈ 5,847）
    压过 `2:402`（7000 ÷ 1.366h ≈ 5,124）。
    """
    add_bot_target(
        session_factory,
        Coordinate(2, 400, 5),
        military_score=99_999.0,
        scanned_at=NOW - timedelta(days=3),
    )
    add_bot_target(session_factory, Coordinate(2, 401, 6), military_score=8_000.0, scanned_at=NOW)
    add_bot_target(
        session_factory,
        Coordinate(2, 402, 7),
        military_score=7_000.0,
        scanned_at=NOW - timedelta(minutes=1),
    )
    enable(repository, MissionKind.BOT, params_json='{"by_military": true, "top_n": 1}')
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert _targets_of(command) == ["2:401:6=BBB"]
    assert not any(part.startswith("2:400:5") for part in command), "窗口外的目标不许被选中"


def test_a_short_window_gives_up_the_window_and_says_so_out_loud(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **用例 (b)：窗口内不够时放弃窗口，照样按得分出击，并且打 WARNING。**

    用户 2026-08-18 的原话：「今晚这件事的真正问题不是『用了旧数据』，而是
    **用了旧数据却没人告诉你**——你是从攻击日志里一条一条对出来的」。所以这条
    用例的两半缺一不可：**选对了目标**，而且**说出来了**。

    ⚠️ **级别必须是 WARNING。** 每一轮都会写一条 INFO 的流水线日志；放宽这件事
    淹在那堆 INFO 里等于没说。降成 INFO 这条用例就会红。

    窗口 2 小时、门限 2 个，而窗口内只有 1 个（`2:402`）→ 放弃窗口 → 全部 3 个
    有读数的目标都进池。这组夹具只留一条空航线，按 `军力 ÷ 往返小时` 排，
    `2:400`（99999 ÷ 1.37h ≈ 72,900）远高于 `2:401`（8000 ÷ 1.37h ≈ 5,800）
    和 `2:402`（100 ÷ 1.37h ≈ 73），所以派出去的是 `2:400`。

    ⚠️ **断言换过一次。** 从前这里派出去的是 `2:401`：旧规格先按军力截断前 2 名
    （`[99999, 8000]`），再在池内按距离排，而 `2:401` 更近——于是**全场最强的那个
    被自己的邻居挤掉了**。那正是「截断 + 按距离」两步说不清的地方。
    """
    add_bot_target(
        session_factory,
        Coordinate(2, 400, 5),
        military_score=99_999.0,
        scanned_at=NOW - timedelta(days=3),
    )
    add_bot_target(
        session_factory,
        Coordinate(2, 401, 6),
        military_score=8_000.0,
        scanned_at=NOW - timedelta(days=3),
    )
    add_bot_target(session_factory, Coordinate(2, 402, 7), military_score=100.0, scanned_at=NOW)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert _targets_of(launcher.latest.command) == ["2:400:5=BBB"]
    widened = [item for item in recorded.warnings() if "放宽窗口" in item[1]]
    assert len(widened) == 1, "放宽了窗口却没打 WARNING，就是「用了旧数据却没人告诉你」"
    _, message, payload = widened[0]
    # 四个数一个都不能少：少了任何一个，看见告警的人还得回库里查才知道该怎么办。
    assert payload["score_max_age_hours"] == 2.0
    assert payload["in_window"] == 1
    assert payload["window_floor"] == 2
    assert payload["oldest_eligible_at_utc"] == (NOW - timedelta(days=3)).isoformat()
    assert "2.0 小时" in message
    assert "只有 1 个" in message


def test_a_short_window_is_not_topped_up_with_the_next_newest(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **用例 (d)：不足时是「放弃窗口」，不是「按时间往下补」。**

    往下补捞到的正是**刚出窗口**那一批，而按生产实测那一批恰恰是最弱的
    ——补下去等于把 PR #176 的缺陷换个地方原样复发。

    这里「更新的」和「更强的」刻意分开站，而且**让更强的那个同时也最近**，
    好让「一条空航线只派得出一发」这件事不至于把判据搅浑：

    | 目标 | 读数 | 军力 | 离出发星 |
    |---|---|---|---|
    | `2:402` | 刚读到（窗口内） | 100 | 234 |
    | `2:400` | 3 小时前（刚出窗口） | 200 | 236 |
    | `2:140` | 3 天前 | 99999 | **3** |

    窗口内只有 `2:402` 一个，不够窗口门限要的 2 个 → 放弃窗口 → 三个都进池 →
    按得分排（`2:140` 是 191,073，另外两个都不到 150），派 `2:140`。

    **按时间往下补的话池子是 `[2:402, 2:400]`**，派出去的会是 `2:402`，
    而 `2:140` 一次都轮不到——这条用例因此会红。
    """
    add_bot_target(
        session_factory,
        Coordinate(2, 140, 5),
        military_score=99_999.0,
        scanned_at=NOW - timedelta(days=3),
    )
    add_bot_target(
        session_factory,
        Coordinate(2, 400, 6),
        military_score=200.0,
        scanned_at=NOW - timedelta(hours=3),
    )
    add_bot_target(session_factory, Coordinate(2, 402, 7), military_score=100.0, scanned_at=NOW)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert _targets_of(command) == ["2:140:5=BBB"], "放宽之后该按得分挑，不是按时间"
    assert not any(part.startswith("2:402:7") for part in command), (
        "窗口内那个最弱的不该因为「它在窗口内」就保送"
    )


def test_a_widened_window_shows_up_on_the_page_too(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """⚠️ **用例 (c)：放宽这件事在页面事实里也要说得出来，不能只写进日志。**

    任务页是用户每天真的会看的那一页，日志是出事之后才去翻的。只报日志的话，
    2026-08-18 那件事会原样重演一遍：助手用着 24 小时前的读数在打，而页面上
    写着「待命」。

    文案与次序由 `tests/unit/domain/test_scheduler_status.py` 那一组守，
    这里只钉事实本身——两边合起来才是完整的。
    """
    add_bot_target(
        session_factory,
        Coordinate(2, 400, 5),
        military_score=9_000.0,
        scanned_at=NOW - timedelta(days=1),
    )
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    bot = task_id(repository, MissionKind.BOT)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert scheduler.snapshot().facts.per_task[bot].scores_window_widened


def test_a_round_inside_the_window_says_nothing_about_widening(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, recorded: RecordingLog
) -> None:
    """反向那一半：正常走窗口时**一个字都不许说**。

    少了这条，一个「恒报放宽」的实现会全绿——而每轮都响的告警、每行都标着警告
    的页面，和不响、不标的一样没用。
    """
    add_bot_target(session_factory, Coordinate(2, 400, 5), military_score=9_000.0, scanned_at=NOW)
    add_bot_target(session_factory, Coordinate(2, 401, 6), military_score=8_000.0, scanned_at=NOW)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    bot = task_id(repository, MissionKind.BOT)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert not scheduler.snapshot().facts.per_task[bot].scores_window_widened
    assert [item for item in recorded.warnings() if "放宽窗口" in item[1]] == []


def test_a_far_but_strong_target_can_now_outrank_a_near_weak_one(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **这条用例 2026-08-18 整个翻转，名字也换了。**

    从前它叫 `test_the_dispatch_order_is_still_nearest_first`，钉的是「池内一律
    按距离，先打近的」。现在只有一条判据 `军力 ÷ 往返小时`，于是「远」不再是
    一票否决，而是一个**要被强度买回来的成本**：

    | 目标 | 军力 | 往返小时 | 得分 |
    |---|---|---|---|
    | `2:400` | 30,000 | 1.371 | **21,884** |
    | `2:140` | 8,000 | 0.523 | 15,284 |

    ⚠️ 上一条用例（`..._dispatches_the_best_value_first`）是这条的**反面**：
    那里 `2:400` 只有 9,000，买不回那 2.6 倍的往返，于是近的 `2:140` 赢。
    两条一起才把判据钉住——**只留任何一条，纯就近或纯军力都能全绿**。

    这组夹具只留一条空航线，所以命令行里那一个就是得分最高的。
    """
    add_bot_target(session_factory, Coordinate(2, 400, 5), military_score=30_000.0)
    add_bot_target(session_factory, Coordinate(2, 140, 6), military_score=8_000.0)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert _targets_of(launcher.latest.command) == ["2:400:5=BBB"]


def test_a_pool_with_no_readings_at_all_says_so_instead_of_saying_it_finished(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **一个军力读数都没有时，事实里要写明原因，不能只留一个 0。**

    ⚠️ **成因 2026-08-18 换过一次**（这条用例从前叫
    `test_a_pool_that_is_entirely_stale_says_so_instead_of_saying_it_finished`）：
    那时超期的目标会被整批滤掉，所以「全过期」能让 `targets_remaining` 归零。
    现在超期不再挡任何目标，剩下的唯一成因是**候选全都从没上过军力榜**。
    判据没变，也不该变：`targets_remaining` 归零时「已完成」是一句听起来顺利、
    实际相反的话，用户会照着它去重开一轮，而重开之后候选池还是那批没读数的目标。

    判据落在事实上而不是页面文案上：文案的次序由
    `tests/unit/domain/test_scheduler_status.py` 那一组守，两边合起来才是完整的。
    """
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    bot = task_id(repository, MissionKind.BOT)
    for coordinate in (Coordinate(2, 140, 5), Coordinate(2, 141, 6)):
        add_bot_target(session_factory, coordinate, military_score=None, scanned_at=None)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    facts = scheduler.snapshot().facts.per_task[bot]
    assert launcher.spawned == [], "一个军力读数都没有还派出去了，那是另一个 bug"
    assert facts.targets_remaining == 0
    assert facts.scores_are_missing, "只剩一个 0，页面就只能把它读成「已完成」"


def test_an_empty_pool_is_not_dressed_up_as_missing_data(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """反向那一半：候选池本来就空（真打完了）时，不许说成「数据未采集」。

    少了这条，一个「`usable == 0` 就报没数据」的实现会全绿，而那会把每一个正常
    跑完的轮次都说成数据有问题——用户于是永远等不到「已完成」，也就永远不知道
    该重开一轮。
    """
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    bot = task_id(repository, MissionKind.BOT)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    facts = scheduler.snapshot().facts.per_task[bot]
    assert facts.targets_remaining == 0
    assert not facts.scores_are_missing


def test_the_old_parameter_name_still_sets_the_hint(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """旧任务的 `params_json` 里存的还是 `rescan_after_hours`，得照样认。

    生产库里已经存着一批带旧键的任务，而用户是自己重启 bat 升级的。读不出来的话
    这一格会静默回落到默认的 2 小时——用户配的 1 小时被悄悄改宽了一倍，
    而页面上看不出任何异常。

    ⚠️ **断言换过一次。** 这条从前验的是「旧键设的 1 小时真的把 90 分钟前的读数
    挡住了」（`launcher.spawned == []`）。有效期现在不挡任何目标，所以那个断言在
    新规格下只会验出「它确实不挡了」——什么都守不住。改成直接验**读出来的时长**：
    这个数现在只喂给日志里那句「超期多久」，而日志说假话比不说更糟。
    """
    row_params = '{"by_military": true, "top_n": 2, "rescan_after_hours": 1}'
    add_bot_target(
        session_factory,
        Coordinate(2, 140, 5),
        military_score=9_000.0,
        scanned_at=NOW - timedelta(minutes=90),
    )
    enable(repository, MissionKind.BOT, params_json=row_params)
    row = task(repository, MissionKind.BOT)

    reading = scheduler._military_pool_reading(row)  # noqa: SLF001 - 钉的就是这一层读出来的数

    assert reading.max_age == timedelta(hours=1), "旧键读不出来会静默改宽一倍"
    # 90 分钟 > 1 小时：这一条**照样出兵**，只是在日志里被点名为「已超期」。
    assert reading.stale == 1
    assert [item.coordinate for item in reading.eligible] == [Coordinate(2, 140, 5)]


def test_without_the_parameter_the_hint_uses_the_documented_default(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """没配就是 2 小时——一轮扫描时长的约 2 倍，理由写在 `DEFAULT_SCORE_MAX_AGE` 上。

    同上一条：验的是**读出来的时长**与「谁被点名超期」，不是「谁被挡住」。
    150 分钟那个超期、90 分钟那个没有，而两个都出兵。
    """
    add_bot_target(
        session_factory,
        Coordinate(2, 140, 5),
        military_score=9_000.0,
        scanned_at=NOW - timedelta(minutes=90),
    )
    add_bot_target(
        session_factory,
        Coordinate(2, 400, 6),
        military_score=8_000.0,
        scanned_at=NOW - timedelta(minutes=150),
    )
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY)
    row = task(repository, MissionKind.BOT)

    reading = scheduler._military_pool_reading(row)  # noqa: SLF001

    assert reading.max_age == timedelta(hours=2)
    assert len(reading.eligible) == 2, "超期的照样进池"
    assert reading.stale == 1, "只有 150 分钟那个该被点名"


def test_a_pool_with_no_readings_never_disables_the_task(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **「候选一个军力读数都没有」是「暂时没活干」，不是错误。**

    按 `MissionParamError` 抛出去的话，`_launch` 会调 `disable_mission_task`：
    任务被停用、挂上 `disabled_reason`，而调度判据认的是
    `enabled and disabled_reason is None`——用户不去页面点一次「恢复」，它就
    **永远不再跑**。

    连续失败也一个都不许涨：那个计数数的是「起来了却异常退出」的子进程，而这里
    连进程都没起。涨了会和 #157（环境条件按 75 收场）、#161（航线不足自动恢复）
    那两套记账打架。

    ⚠️ **两颗目标都是「从没上过榜」那一档。** 从前这条用例用的是「有分数但超期」，
    而超期现在照样出兵，用它就验不出「没活干」这件事了。
    """
    add_bot_target(session_factory, Coordinate(2, 140, 5), military_score=None, scanned_at=None)
    add_bot_target(session_factory, Coordinate(2, 141, 6), military_score=None, scanned_at=None)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()

    for _ in range(5):
        scheduler.tick()

    row = task(repository, MissionKind.BOT)
    assert row.disabled_reason is None, "没有军力读数不许把任务停用"
    assert row.consecutive_failures == 0, "一个子进程都没起，不许记失败"
    assert row.enabled is True
    assert launcher.spawned == [], "更不许把没有军力读数的派出去"


def test_building_a_command_out_of_a_starved_pool_is_idle_not_a_param_error(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """同一条规矩的另一半：**组命令行那一步也不许抛 `MissionParamError`。**

    上一条走的是正常路径——`has_work` 早就把这条链路判成没活干，`_launch` 根本
    不会被叫到。这一条钉的是那之间的**时间差**：事实在锁外读，读完到组命令行之间
    池子可能刚好被另一条链路清空，那一刻 `_military_command` 是真会跑到底的。
    抛成参数错误的话，一次几微秒的时间差会把整条链路停用到用户手动恢复为止。

    ⚠️ **`MissionIdle` 不能继承 `MissionParamError`**，否则 `_launch` 里现成的
    `except MissionParamError` 会顺手接住它、接住就是停用——这条断言正是钉那一点：
    第二句要求它**不是** `MissionParamError`。
    """
    add_bot_target(session_factory, Coordinate(2, 140, 5), military_score=None, scanned_at=None)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    row = task(repository, MissionKind.BOT)

    with pytest.raises(MissionIdle):
        scheduler._military_command(row)  # noqa: SLF001 - 钉的就是这一层的表态
    try:
        scheduler._military_command(row)  # noqa: SLF001
    except MissionIdle as exc:
        assert not isinstance(exc, MissionParamError), "继承了就等于这个类型白分了"


def test_a_pool_without_readings_lets_the_ranking_scan_take_the_mouse(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """候选一个军力读数都没有时这条链路让位，调度器自己去跑军力榜把池子刷新。

    ⚠️ **刷新仍然只能由调度器发起。** 攻击链路自己去起 RANKING 的话，两条链路
    会争同一只鼠标。
    """
    add_bot_target(session_factory, Coordinate(2, 140, 5), military_score=None, scanned_at=None)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository, MissionKind.RANKING)
    scheduler.start()
    scheduler.tick()

    assert launcher.kinds == [MissionKind.RANKING]


class RecordingLog:
    """把 `record_system_log` 的调用记下来。签名与真的那一个一致。

    ⚠️ **挂机心跳写的行一律不收**（同 `test_line_shortage_recovery.RecordingLog`，
    理由整段写在那里）：这里钉的是「某一条链路写了几条」，而这几条用例都会把时钟
    往前跳十分钟，正好跳过心跳的断线阈值。心跳自己那份留痕由
    `test_scheduler_uptime_heartbeat.py` 钉着。
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, level, source, message, *, payload=None, logged_at_utc=None, **_):  # type: ignore[no-untyped-def]
        if message.startswith("挂机心跳"):
            return
        self.entries.append((level, message, dict(payload or {})))

    def warnings(self) -> list[tuple[str, str, dict[str, object]]]:
        return [item for item in self.entries if item[0] == "WARNING"]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


def test_a_pool_starved_for_long_enough_writes_a_warning(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, clock, recorded: RecordingLog
) -> None:
    """连着筛不出能打的目标要留下一条 WARNING，否则攻击就是**悄悄**停摆的。

    「候选一个军力读数都没有」会落成「此刻没活干」——那是对的，调度器会去跑军力榜。
    可如果扫描本身跑不起来（扫得太慢、榜单读不出来、军力榜任务被停用），这个状态
    会一直维持，而页面上只有一句不痛不痒的状态。

    ⚠️ **措辞与成因 2026-08-18 换过一次**：从「分数全部过期、扫描跟不上有效期」
    改成「一个军力读数都没有」。不改的话这条警告会指着一个不存在的原因
    （超期现在不挡任何目标），用户照它去把有效期调长，调完照样一发不派。

    ⚠️ **顺带记一笔：这条警告在那之前一次都没响过。** 那时
    `usable = 有读数的 + 没读数的`，而库里从来都有没读数的行，`starved` 恒为假。

    ⚠️ **不能每 tick 刷一条**：tick 每秒一次，一晚上就是几万行，真正要看的那条
    会被淹掉。所以先连 tick 五次确认一条都没写，再把时钟推过那道门槛。
    """
    add_bot_target(session_factory, Coordinate(2, 140, 5), military_score=None, scanned_at=None)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()

    for _ in range(5):
        scheduler.tick()
    assert recorded.warnings() == [], "刚开始那几秒不该报，军力榜刚起步时本来就没有读数"

    clock.now = NOW + STALE_POOL_WARNING_AFTER
    scheduler.tick()

    assert len(recorded.warnings()) == 1
    _, message, payload = recorded.warnings()[0]
    assert "军力候选池" in message
    assert "军力榜还没扫到它们" in message
    assert payload["attackable"] == 1
    assert payload["with_readings"] == 0
    assert payload["dropped_unrated"] == 1

    # 再往前走一点点还不到下一次的间隔，不许补第二条。
    clock.now = NOW + STALE_POOL_WARNING_AFTER + timedelta(minutes=1)
    scheduler.tick()
    assert len(recorded.warnings()) == 1


def test_a_pool_that_recovered_stops_warning(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, clock, recorded: RecordingLog
) -> None:
    """军力榜把这一颗读到之后就该闭嘴，而且那一段的账要清掉。

    不清的话，下一次饿着会立刻按「已经憋了很久」补一条 WARNING——而那一刻其实
    才刚开始，报出来的时长是假的。
    """
    unread = Coordinate(2, 140, 5)
    add_bot_target(session_factory, unread, military_score=None, scanned_at=None)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()
    clock.now = NOW + STALE_POOL_WARNING_AFTER
    scheduler.tick()
    assert len(recorded.warnings()) == 1

    # 军力榜采到了这一颗的分数：池子恢复，这一段的账该清掉。
    score_bot_target(session_factory, unread, military_score=9_000.0, scanned_at=clock.now)
    scheduler.tick()

    # 分数又被清掉（军力榜清过一次坏读数）——那是**新的一段**，刚开始，不许立刻
    # 按上一段补一条。账没清干净的话，这一 tick 会看到「自很久以前起一直饿着」而当场再报。
    score_bot_target(session_factory, unread, military_score=None, scanned_at=None)
    clock.now += timedelta(days=3)
    scheduler.tick()

    assert len(recorded.warnings()) == 1


def test_an_empty_pool_is_never_reported_as_a_starved_one(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, clock, recorded: RecordingLog
) -> None:
    """⚠️ **「一个候选都没有」和「候选的分数全过期」是两回事，只有后者该报。**

    前者是完全正常的一档：已知 bot 全在 24 小时冷却里或还在飞。拿它去报
    「军力榜扫描跟不上有效期」是句假话，而假警报响几次之后就没人看了。
    """
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()

    clock.now = NOW + STALE_POOL_WARNING_AFTER * 3
    for _ in range(5):
        scheduler.tick()

    assert recorded.warnings() == []


def test_a_pool_of_only_stale_scores_is_never_reported_as_starved(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, clock, recorded: RecordingLog
) -> None:
    """⚠️ **「分数全都超期」不是「军力数据未采集」——这两句话要说的事完全不同。**

    ⚠️ **这条用例是 `test_a_pool_of_only_unrated_targets_is_never_reported_as_starved`
    的翻转。** 那一条说的是「全库都没有分数」照样能打（走补位池），所以不该报警；
    2026-08-18 之后没有分数的一个都不打，那一档**恰恰就是**该报警的那一档
    （钉在 `test_a_pool_starved_for_long_enough_writes_a_warning`）。
    换过来的是「分数全都超期」：那一档现在照样能打（放弃窗口），报「筛不出能打的
    目标」就是一条假警报，而假警报响几次之后就没人看了。

    ⚠️ **断言 2026-08-18 二次改造时改窄了一点：从「一条 WARNING 都没有」改成
    「没有『筛不出能打的目标』那一条」。** 这一版超期确实会触发另一条 WARNING
    ——「放宽窗口」——而那条正是这次改动要的，它响是对的。留着原来那个大而全的
    断言，就等于让这条用例反过来禁止本次改动最要紧的那一半。放宽那条自己的护栏
    在 `test_a_short_window_gives_up_the_window_and_says_so_out_loud`。
    """
    add_bot_target(
        session_factory,
        Coordinate(2, 140, 5),
        military_score=9_000.0,
        scanned_at=NOW - timedelta(days=3),
    )
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()

    clock.now = NOW + STALE_POOL_WARNING_AFTER * 3
    for _ in range(5):
        scheduler.tick()

    assert [item for item in recorded.warnings() if "筛不出能打的目标" in item[1]] == []
    assert launcher.kinds == [MissionKind.BOT], "分数超期不妨碍它去打"
