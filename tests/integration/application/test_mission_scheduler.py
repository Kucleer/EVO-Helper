"""常驻调度循环：把纯判据、子进程管理、数据库粘起来。

判据本身在 `tests/unit/domain/test_scheduler.py` 里已经钉死，这里守的是**接线**：
事实从库里读对了没有、参数换算成了什么命令行、起停有没有落进 `mission_runs`。
接线错了不会报错，只会让调度器静默地空转或者永久卡死——那正是这整条修复要防的。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from evo_helper.application.mission_scheduler import MAX_CONSECUTIVE_FAILURES, MissionScheduler
from evo_helper.domain.fleet_preset import DEFAULT_PRESET
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)
from evo_helper.domain.report_wait import MAX_REPORT_AGE
from evo_helper.domain.scheduler import RESTART_COOLDOWN, MissionKind
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


def enable(repository: SqlAlchemyRepository, kind: MissionKind, **fields: object) -> None:
    repository.update_mission_task(kind, enabled=True, **fields)  # type: ignore[arg-type]


def add_bot_target(session_factory, coordinate: Coordinate) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        session.add(
            orm.BotTargetRow(
                id=uuid4(),
                galaxy=coordinate.galaxy,
                system=coordinate.system,
                position=coordinate.position,
                is_bot=True,
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
):
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
        )
    )
    return dispatch_id


def attach_report(session_factory, dispatch_id, target: Coordinate, reported_at: datetime) -> None:  # type: ignore[no-untyped-def]
    """直接挂一份战报，不走 `append_report` 的坐标+时间容差匹配。

    那条路等于让测试依赖匹配算法，而这里要验的只是「有没有战报」这一个事实。
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
    add_bot_target(session_factory, Coordinate(2, 150, 3))
    add_bot_target(session_factory, Coordinate(2, 900, 4))
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    repository.update_mission_task(MissionKind.SCAN, enabled=False)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert "2:150:3" in command
    assert "2:900:4" not in command
    assert command[-2:] == ["--probe", "--attack"]


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
        self.pending_calls: list[tuple[str, timedelta, timedelta]] = []

    def pending_reports_for_kind(  # type: ignore[override]
        self, target_kind, *, now_utc, grace, max_age
    ):
        self.pending_calls.append((target_kind, grace, max_age))
        return super().pending_reports_for_kind(
            target_kind, now_utc=now_utc, grace=grace, max_age=max_age
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

    assert (TARGET_KIND_PIRATE, timedelta(minutes=45), MAX_REPORT_AGE) in repository.pending_calls


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
    repository.update_mission_task(MissionKind.SCAN, enabled=False)
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


# -- 连续失败自停 --------------------------------------------------------------


def test_three_consecutive_crashes_disable_the_chain(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """没有这条，调度循环会在一个坏掉的任务上变成满速空转的重启循环。"""
    enable(repository, MissionKind.PIRATE)
    repository.update_mission_task(MissionKind.SCAN, enabled=False)
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
    target = Coordinate(2, 150, 3)
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


def test_a_target_that_only_got_a_probe_report_still_counts_as_remaining(  # type: ignore[no-untyped-def]
    repository, launcher, clock, run_id, session_factory
) -> None:
    """探路发的战报只说明该分档了，不说明这一轮走完了。

    把探路当成完成，bot 会在只探不打的状态下「完成」整轮。
    """
    target = Coordinate(2, 150, 3)
    add_bot_target(session_factory, target)
    dispatch_id = dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=target,
        dispatched_at=NOW - timedelta(hours=1),
        preset_name=DEFAULT_PRESET.name,
    )
    attach_report(session_factory, dispatch_id, target, NOW - timedelta(minutes=30))
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    repository.update_mission_task(MissionKind.SCAN, enabled=False)
    scheduler.start()

    scheduler.tick()

    assert launcher.kinds == [MissionKind.BOT]


# -- 孤儿 ----------------------------------------------------------------------


def test_prepare_marks_orphans_rather_than_shooting_at_a_recycled_pid(  # type: ignore[no-untyped-def]
    repository, launcher, clock
) -> None:
    """pid 会被系统回收复用，照着一个可能已经换了主人的号码开枪比留个警告更糟。"""
    repository.ensure_mission_rows(now_utc=NOW)
    repository.begin_mission_run(
        MissionKind.SCAN,
        command=["python"],
        pid=31337,
        started_at_utc=NOW - timedelta(hours=1),
        log_path="var/logs/mission-scan.log",
    )

    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)

    assert scheduler.prepare() == 1
    assert repository.mission_runs(limit=1)[0].stopped_by == "UNKNOWN"


def test_prepare_seeds_the_three_chains_and_the_config(repository, launcher, clock) -> None:  # type: ignore[no-untyped-def]
    """迁移里没有 `bulk_insert`，这几行得有人保证存在。"""
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)

    scheduler.prepare()

    assert len(repository.mission_tasks()) == 3
    assert repository.scheduler_config().pirate_daily_quota == 32
