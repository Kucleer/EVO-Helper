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

from evo_helper.application.mission_progress import STALL_TIMEOUT, ProgressReading
from evo_helper.application.mission_scheduler import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_ENVIRONMENT_EXEMPTIONS,
    MissionScheduler,
)
from evo_helper.domain.bot_round import BOT_ATTACK_PRESET
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
    session_factory, coordinate: Coordinate, *, military_score: float | None = None
) -> None:
    with session_factory() as session:
        session.add(
            orm.BotTargetRow(
                id=uuid4(),
                galaxy=coordinate.galaxy,
                system=coordinate.system,
                position=coordinate.position,
                is_bot=True,
                military_score=military_score,
            )
        )
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
):
    """记一发被游戏接受的派遣。

    `flight` 不传就留空航线钟，那一档按 `UNKNOWN_LINE_HOLD`（90 分钟）算**仍然
    占着航线**——测试若只想验别的事，就得把飞行时间给上，否则这一发会一直压着
    航线让链路起不来。
    """
    intent_id, dispatch_id = uuid4(), uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
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
    0.32 秒；而 tick 每秒一次、页面每 2 秒问一次状态、桌面悬浮窗还有一次。
    这里只是把那段时间拉长成一把测试握得住的闸门。
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


def test_the_military_pool_takes_the_strongest_then_orders_them_by_distance(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """用户口径（2026-08-15）：「先取前 50 名，然后按距离排序，开始攻击」。

    这里 `top_n=2`：`9000` 与 `8000` 进池，`100` 落选；而池内按距离排，
    所以近的 2:140 排在远的 2:400 前面——**军力只决定谁进池，不决定池内次序**。
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
    # 这组夹具只留一条空航线；军力 runner 会先取池中最近的那颗，并把实际使用的
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


def test_a_target_with_no_score_still_gets_into_the_pool_under_a_cap(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """上限只挡「太强」，不挡「读不出来」——库里最多的正是没扫到过的那批。"""
    add_bot_target(session_factory, Coordinate(2, 140, 5), military_score=None)
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
