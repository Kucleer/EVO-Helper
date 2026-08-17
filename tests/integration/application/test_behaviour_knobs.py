"""三个**运维旋钮**：航线占用时长、翻信箱冷却、bot 重复攻击间隔。

用户口径（2026-08-17）：「你需要多考虑配置场景，不要每次都是我提出」。

这三项的共同点是**没有唯一正确答案**——取值取决于用户当下的处境（活动期信箱堆积、
周一 bot 军力刷新日、机器闲忙、要不要激进），改它们会让结果变得「更适合我」，
而不是变「错」。所以它们做成可配置；而屏幕几何、OCR 配方、盲拖标定的窗口与余量
这些由物理事实决定的**标定常量**一概不配（见各自常量上的注释）。

这份用例钉的是每个旋钮的三件事，外加一条日志：

1. **留空 = 用代码里的默认值**，而且断言的是**具体数字**——写成「等于那个常量」的
   自反断言，改了常量用例照样绿，等于什么都没守住。
2. **配了 N 就真的按 N 跑**：判据要落在真实的查询/命令上，不是读回配置本身。
3. **不可能的取值当场拒掉**，边界两侧各验一次。
4. **用了非默认值必须在 `system_log` 里留一条痕迹。** 用户同期定的规矩是「出事时
   要能只靠库里的日志定位」；一个被改过的阈值最阴的失败方式是日志里一切都像默认
   行为，排障的人照着代码里的数去推，怎么算都对不上。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from evo_helper.application.mission_scheduler import (
    BOT_REVISIT_MAX_HOURS,
    DEFAULT_BOT_REVISIT,
    MissionScheduler,
)
from evo_helper.domain.missions import MissionParamError
from evo_helper.domain.models import Coordinate
from evo_helper.domain.reconcile_cooldown import RECONCILE_COOLDOWN, decide_reconcile
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    TARGET_KIND_BOT,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)
from evo_helper.domain.report_wait import MAX_REPORT_AGE, UNKNOWN_LINE_HOLD
from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    current_system_log_sink,
    install_system_log_sink,
    reset_knob_override_memo,
    shutdown_system_log_sink,
)
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor

HOME = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 140, 3)
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


class Collector:
    def __init__(self) -> None:
        self.records: list[SystemLogRecord] = []

    def __call__(self, batch) -> None:  # type: ignore[no-untyped-def]
        self.records.extend(batch)


@pytest.fixture
def collector() -> Iterator[Collector]:
    """装一个假出口，好让「用了非默认值」那条日志被接住。

    ⚠️ 每次都清一遍去重账本：`record_knob_override` 按进程记「这个取值已经写过
    了」，不清的话第二个用例里那条日志会被当成重复而不写，用例就变成了在验
    「上一个用例留下的状态」。
    """
    reset_knob_override_memo()
    sink_collector = Collector()
    install_system_log_sink(
        SystemLogSink(sink_collector, flush_interval_s=0.01), context=SystemLogContext()
    )
    try:
        yield sink_collector
    finally:
        shutdown_system_log_sink()
        reset_knob_override_memo()


def _flush() -> None:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


def _configure(repository: SqlAlchemyRepository, **knobs: int | None) -> None:
    """写一份全局攻击配置。没提到的旋钮一律留空（= 用默认值）。"""
    repository.replace_military_attack_tiers("[]", **knobs)


def _dispatch(
    repository: SqlAlchemyRepository,
    run_id,  # type: ignore[no-untyped-def]
    *,
    dispatched_at: datetime,
    target: Coordinate = TARGET,
    accepted: bool = True,
) -> None:
    """派出一发**读不到飞行时间**的 bot 攻击：三个钟全留空。

    这正是航线占用时长唯一管得着的那一档——飞行时间读到了的，航线什么时候空由
    `line_free_at_utc` 说了算，跟这个旋钮无关。
    """
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=HOME,
            target=target,
            preset=FleetPresetRef(name="BBB", signature="sig"),
            cycle_start_utc=dispatched_at,
            created_at_utc=dispatched_at,
            target_kind=TARGET_KIND_BOT,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=dispatched_at,
            accepted=accepted,
            mission_kind=MISSION_KIND_ATTACK,
        )
    )


# -- 常量本身 ------------------------------------------------------------------


def test_the_three_defaults_are_still_the_numbers_the_comments_claim() -> None:
    """⚠️ **断言具体数字，不是「等于那个常量」。**

    自反断言（`assert X == X`）在常量被改掉之后照样绿，等于没守住任何东西。
    这三个数各自的来历写在常量上：90 = 实测最长往返 62.6 分钟留四成余量；
    15 = 夹在续跑间隔中位数（10.8）与战报宽限期（30）之间；24 = 用户口径。
    """
    assert UNKNOWN_LINE_HOLD == timedelta(minutes=90)
    assert RECONCILE_COOLDOWN == timedelta(minutes=15)
    assert DEFAULT_BOT_REVISIT == timedelta(hours=24)


# -- 航线占用时长 --------------------------------------------------------------


def test_an_empty_line_hold_still_holds_the_line_for_ninety_minutes(  # type: ignore[no-untyped-def]
    repository, scheduler, run_id
) -> None:
    """留空 = 90 分钟：89 分钟前派出去的还占着，91 分钟前的已经放开。"""
    _configure(repository)
    _dispatch(repository, run_id, dispatched_at=NOW - timedelta(minutes=89))
    _dispatch(repository, run_id, dispatched_at=NOW - timedelta(minutes=91))

    hold = scheduler.unknown_line_hold()
    assert hold == timedelta(minutes=90)
    assert repository.count_inflight(now_utc=NOW, origin=HOME, hold=hold) == 1


def test_a_configured_line_hold_is_what_the_line_accounting_uses(  # type: ignore[no-untyped-def]
    repository, scheduler, run_id
) -> None:
    """配成 45 分钟，60 分钟前那一发就该被当成已回港。

    判据落在 `count_inflight` 上而不是「读回配置等于 45」：后者只证明写库没坏，
    而这个旋钮真正要影响的是**调度器还认为有几条航线被占着**。
    """
    _configure(repository, unknown_line_hold_minutes=45)
    _dispatch(repository, run_id, dispatched_at=NOW - timedelta(minutes=30))
    _dispatch(repository, run_id, dispatched_at=NOW - timedelta(minutes=60))

    hold = scheduler.unknown_line_hold()
    assert hold == timedelta(minutes=45)
    assert repository.count_inflight(now_utc=NOW, origin=HOME, hold=hold) == 1
    # 同一批数据按默认的 90 分钟数，两发都还占着——这一行是在证明上面那个 1
    # 真的来自配置，而不是数据本身就只有一发够得着。
    assert repository.count_inflight(now_utc=NOW, origin=HOME) == 2


def test_the_manual_line_release_measures_with_the_configured_hold(  # type: ignore[no-untyped-def]
    repository, run_id
) -> None:
    """「清理航线占用」必须和 `count_inflight` 用同一把尺子。

    尺子不一致的现象是：页面写着「占着 2 条」，按钮回执却是「放开了 1 条」——
    而那个数字是这个按钮唯一的可见回执，对不上读起来像功能坏了。
    """
    _dispatch(repository, run_id, dispatched_at=NOW - timedelta(minutes=30))
    _dispatch(repository, run_id, dispatched_at=NOW - timedelta(minutes=60))

    hold = timedelta(minutes=45)
    assert repository.count_inflight(now_utc=NOW, origin=HOME, hold=hold) == 1
    assert repository.release_held_lines(now_utc=NOW, hold=hold) == 1


@pytest.mark.parametrize("value", [0, -1, "3.5", True, "很久"])
def test_impossible_line_holds_are_refused(scheduler: MissionScheduler, value: object) -> None:
    """0 是被实机推翻掉的旧口径（「读不到就当没占航线」），不是一个合法取值。"""
    with pytest.raises(MissionParamError):
        scheduler.validate_unknown_line_hold_minutes(value)


def test_a_line_hold_at_or_past_the_give_up_threshold_is_refused(
    scheduler: MissionScheduler,
) -> None:
    """占用时长不能够到「放弃等战报」那条线，否则会出现锁死的航线。

    ⚠️ 上界跟着 `MAX_REPORT_AGE` 走而不是写死 360：那个常量正在被另一条链路
    做成可配置，写死的数会在它一变的当天悄悄失效。
    """
    ceiling = int(MAX_REPORT_AGE.total_seconds() // 60)
    assert scheduler.validate_unknown_line_hold_minutes(ceiling - 1) == ceiling - 1
    with pytest.raises(MissionParamError):
        scheduler.validate_unknown_line_hold_minutes(ceiling)


def test_an_overridden_line_hold_leaves_a_trace_in_the_system_log(  # type: ignore[no-untyped-def]
    repository, scheduler, collector: Collector
) -> None:
    """配了非默认值就得留痕；用默认值时**一个字都不写**。

    只在非默认时写，是为了不让每个进程都刷一条「我用的是默认值」把真正有信息量
    的那几行淹掉。
    """
    _configure(repository)
    assert scheduler.unknown_line_hold() == UNKNOWN_LINE_HOLD
    _flush()
    assert not [item for item in collector.records if "unknown_line_hold" in item.message]

    _configure(repository, unknown_line_hold_minutes=45)
    scheduler.unknown_line_hold()
    _flush()
    traces = [item for item in collector.records if "unknown_line_hold" in item.message]
    assert len(traces) == 1
    assert "0:45:00" in traces[0].message
    assert "1:30:00" in traces[0].message, "默认值也要写进去，否则看日志的人没有参照"

    # 同一个取值读第二遍不再重复记：调度器每 tick 都会读一次配置。
    scheduler.unknown_line_hold()
    _flush()
    assert len([item for item in collector.records if "unknown_line_hold" in item.message]) == 1


# -- 翻信箱冷却 ----------------------------------------------------------------


def test_an_empty_reconcile_cooldown_still_waits_fifteen_minutes(  # type: ignore[no-untyped-def]
    repository,
) -> None:
    """留空 = 15 分钟：14 分钟前对过账就跳过，16 分钟前的就翻。"""
    _configure(repository)
    row = repository.military_attack_config()
    assert row.reconcile_cooldown_minutes is None

    cooldown = RECONCILE_COOLDOWN
    assert cooldown == timedelta(minutes=15)
    assert not decide_reconcile(
        last_reconciled_at_utc=NOW - timedelta(minutes=14), now=NOW, cooldown=cooldown
    ).sweep
    assert decide_reconcile(
        last_reconciled_at_utc=NOW - timedelta(minutes=16), now=NOW, cooldown=cooldown
    ).sweep


def test_a_configured_reconcile_cooldown_changes_the_decision(  # type: ignore[no-untyped-def]
    repository,
) -> None:
    """配成 5 分钟，一个默认口径下会被跳过的时刻就该翻信箱了。"""
    _configure(repository, reconcile_cooldown_minutes=5)
    minutes = repository.military_attack_config().reconcile_cooldown_minutes
    assert minutes == 5

    last = NOW - timedelta(minutes=10)
    assert decide_reconcile(
        last_reconciled_at_utc=last, now=NOW, cooldown=timedelta(minutes=minutes)
    ).sweep
    assert not decide_reconcile(last_reconciled_at_utc=last, now=NOW).sweep


def test_zero_reconcile_cooldown_means_sweep_every_run(scheduler: MissionScheduler) -> None:
    """0 是合法的，而且它不是「关掉」——是「每一轮开工都翻」，最安全的那一侧。"""
    assert scheduler.validate_reconcile_cooldown_minutes(0) == 0
    assert decide_reconcile(last_reconciled_at_utc=NOW, now=NOW, cooldown=timedelta(0)).sweep


def test_the_reconcile_cooldown_ceiling_follows_the_report_grace(  # type: ignore[no-untyped-def]
    session_factory, scheduler
) -> None:
    """上界是战报宽限期的一半，**宽限期改了它跟着改**。

    写死一个 15 的话，用户把宽限期调到 60 之后仍然填不进 30——而那时 30 已经
    完全安全了。页面上显示的上界与校验用的必须是同一个数。
    """
    assert scheduler.reconcile_cooldown_ceiling() == 15
    assert scheduler.validate_reconcile_cooldown_minutes(15) == 15
    with pytest.raises(MissionParamError):
        scheduler.validate_reconcile_cooldown_minutes(16)

    with session_factory() as session:
        row = session.get(orm.SchedulerConfigRow, 1)
        assert row is not None
        row.report_grace_minutes = 60
        session.commit()
    assert scheduler.reconcile_cooldown_ceiling() == 30
    assert scheduler.validate_reconcile_cooldown_minutes(30) == 30
    with pytest.raises(MissionParamError):
        scheduler.validate_reconcile_cooldown_minutes(31)


@pytest.mark.parametrize("value", [-1, "1.5", True, "一会儿"])
def test_impossible_reconcile_cooldowns_are_refused(
    scheduler: MissionScheduler, value: object
) -> None:
    with pytest.raises(MissionParamError):
        scheduler.validate_reconcile_cooldown_minutes(value)


# -- bot 重复攻击间隔 ----------------------------------------------------------


def test_an_empty_bot_revisit_still_excludes_the_last_twenty_four_hours(  # type: ignore[no-untyped-def]
    repository, scheduler, run_id
) -> None:
    """留空 = 24 小时：23 小时前打过的仍被排除，25 小时前的可以再打。"""
    _configure(repository)
    _dispatch(repository, run_id, dispatched_at=NOW - timedelta(hours=23), target=TARGET)
    older = Coordinate(2, 140, 4)
    _dispatch(repository, run_id, dispatched_at=NOW - timedelta(hours=25), target=older)

    window = scheduler._bot_revisit_window()
    assert window == timedelta(hours=24)
    assert repository.attacked_bot_targets_since(NOW - window) == {TARGET}


def test_a_configured_bot_revisit_window_reopens_older_targets(  # type: ignore[no-untyped-def]
    repository, scheduler, run_id
) -> None:
    """配成 6 小时，8 小时前打过的那个就重新回到候选池。"""
    _configure(repository, bot_revisit_hours=6)
    _dispatch(repository, run_id, dispatched_at=NOW - timedelta(hours=8), target=TARGET)

    window = scheduler._bot_revisit_window()
    assert window == timedelta(hours=6)
    assert repository.attacked_bot_targets_since(NOW - window) == set()
    # 同一批数据按默认的 24 小时算仍然被排除——证明上面那个空集来自配置。
    assert repository.attacked_bot_targets_since(NOW - DEFAULT_BOT_REVISIT) == {TARGET}


@pytest.mark.parametrize("value", [0, -1, "2.5", True, "一天"])
def test_impossible_bot_revisit_windows_are_refused(
    scheduler: MissionScheduler, value: object
) -> None:
    """0 会让榜首那一个被反复打——候选池是军力降序排的，排除一取消就没有别的
    机制拦得住它。所以 0 当场拒掉，而不是当成「最激进的那一档」。
    """
    with pytest.raises(MissionParamError):
        scheduler.validate_bot_revisit_hours(value)


def test_the_bot_revisit_window_stops_at_one_week(scheduler: MissionScheduler) -> None:
    """再长就跨过了 bot 军力每周一 UTC+0 的刷新周期。"""
    assert BOT_REVISIT_MAX_HOURS == 168
    assert scheduler.validate_bot_revisit_hours(168) == 168
    with pytest.raises(MissionParamError):
        scheduler.validate_bot_revisit_hours(169)


def test_an_overridden_bot_revisit_leaves_a_trace_in_the_system_log(  # type: ignore[no-untyped-def]
    repository, scheduler, collector: Collector
) -> None:
    _configure(repository)
    assert scheduler._bot_revisit_window() == DEFAULT_BOT_REVISIT
    _flush()
    assert not [item for item in collector.records if "bot_revisit" in item.message]

    _configure(repository, bot_revisit_hours=6)
    scheduler._bot_revisit_window()
    _flush()
    traces = [item for item in collector.records if "bot_revisit" in item.message]
    assert len(traces) == 1
    assert "6:00:00" in traces[0].message
    assert "1 day" in traces[0].message, "默认值也要写进去，否则看日志的人没有参照"


# -- 留空的语义 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "validate",
    [
        "validate_unknown_line_hold_minutes",
        "validate_reconcile_cooldown_minutes",
        "validate_bot_revisit_hours",
    ],
)
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_means_follow_the_default_not_zero(
    scheduler: MissionScheduler, validate: str, blank: object
) -> None:
    """⚠️ **「没配」和「配了 0」是两回事。**

    空串被读成 0 的话，翻信箱冷却会静悄悄变成「每轮都翻」、另外两个则直接被
    下界拒掉——两种都不是用户按下「保存」时的意思。
    """
    assert getattr(scheduler, validate)(blank) is None
