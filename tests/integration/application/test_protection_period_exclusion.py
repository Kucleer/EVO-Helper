"""撞上保护期的目标不再被下一轮重新挑中。

## 修的是什么

游戏的保护期是 **8 小时**，**任何人打过都会触发**，而且**只能撞上了才知道**
（`game.pirate_ui.DIALOG_NO_MISSION`）。在 `bot_targets.protection_seen_at_utc`
出现之前，「撞上了」这件事只存在于 `system_log` 的纯文本里，选靶查不到。

实机 2026-08-18 那一轮（真打模式，四个目标）：

    20:29:56  模式：真打；目标 4:393:10, 4:445:5, 4:447:15, 4:452:13
    20:39:21    4:393:10 在保护期内（没有可执行的任务）；跳过这个目标
    ...
    20:41:21  完成：目标 4 个，攻击 0 发，拦下 4 次      ← 整轮 11.5 分钟，一发没派
    20:41:22  模式：真打；目标 4:393:10, 4:445:5, ...   ← 一秒后，同样的四个

**上一轮已经当场确认这四个都在保护期，下一轮还是把同样的四个挑了出来**，如此
往复直到 8 小时自然过去。每个目标每轮约 2.9 分钟鼠标时间（导航 + 开面板 + 撞弹窗
+ 退出），一轮四个 11.5 分钟——而这台机器一天的鼠标时间本来只有 56% 在干活。

## 这份用例钉的五件事

1. **撞上保护期 → 落库**（`note_protection_period`）。
2. **下一轮选靶不再挑中它。**
3. **排除窗口到期后重新可选**——排除是有尽头的，不是把目标永久删掉。
4. **排除排在「花掉航线预算」之前**，和 24 小时那条一样；挪到后面会把候选池缩成
   空集（`_military_candidates` 的 docstring 写着为什么）。
   ⚠️ PR #194 之前这一条的说法是「排在取前 N 之前」——那道军力硬截断随第 ④⑤ 步
   合并一起没有了，收窄候选池的闸口换成了航线预算。**不变量没有放宽，只换了载体。**
5. **旋钮留空 = 默认 8 小时**，配了就按配的算，不可能的取值当场拒掉。

⚠️ **「保护期 8 小时」和「排除 8 小时」是两件事，同数不同义。** 前者是游戏规则
（`domain.target_order.GAME_PROTECTION_HOURS`），后者是策略——我们只知道「在时刻
T 撞上了」，不知道保护期什么时候开始的，所以按 T+8h 排除必然过度。代价不对称
（过度排除只是少打几个，候选池有 3000+ 个；排除不足是每轮白烧鼠标时间），
所以宁可过度。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.missions import ORIGIN, MissionParamError
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.domain.target_order import (
    DEFAULT_PROTECTION_EXCLUSION,
    GAME_PROTECTION_HOURS,
    PROTECTION_EXCLUSION_MAX_HOURS,
    ScoredTarget,
    attack_value,
)
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

NOW = datetime(2026, 8, 18, 20, 41, tzinfo=UTC)
#: ⚠️ `top_n` 这个键 PR #194 之后**只剩「窗口门限」一个身份**（Python 侧叫
#: `window_floor`），**不再决定打谁**。这里填 2 只是把门限压到候选数之下，好让窗口
#: 不被放弃——「这一轮派几发」现在由航线预算定，见
#: `test_the_exclusion_runs_before_the_budget_is_spent`。
BOT_TOP_TWO = '{"by_military": true, "top_n": 2}'

#: 实机 2026-08-18 20:29 那一轮里的两个坐标，原样搬过来。
STRONGEST = Coordinate(4, 393, 10)
WEAKER = Coordinate(4, 445, 5)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


def _task_id(repository: SqlAlchemyRepository, kind: MissionKind) -> int:
    return next(row.id for row in repository.mission_tasks() if row.kind == kind.value)


def _enable(repository: SqlAlchemyRepository, kind: MissionKind, **fields: object) -> None:
    """启用一条链路。**默认给 2 条航线**——理由见下。

    ⚠️ `scheduler_config.fleet_line_limit` 的默认值只有 1 条，而按得分分配
    （`domain.military_attack.assign_by_capacity_and_value`）只会派到航线用完为止。
    于是「候选池里有两个、只派得出一个」时，「排除了谁」和「谁的得分更高」这两件事
    的结果长得一模一样——**变异测试当场验出这个洞**：把排除整条删掉，
    `test_the_next_round_no_longer_picks_a_protected_target` 照样绿，因为被排除的
    那个本来也轮不到。给足 2 条航线，「两个都该派出去」才成立，断言才落在排除上。

    ⚠️ **`test_the_exclusion_runs_before_the_budget_is_spent` 反过来要 1 条**，
    显式传 `fleet_lines=1` 覆盖这个默认——那一条要的正是「预算不够、必须二选一」
    这个局面。两条用例的航线数是各自判据的一部分，不是随手填的。
    """
    fields.setdefault("fleet_lines", 2)
    repository.update_mission_task(_task_id(repository, kind), enabled=True, **fields)  # type: ignore[arg-type]


def _only_bot(repository: SqlAlchemyRepository) -> None:
    """把会填空隙的那几种关掉，只留 bot 攻击——否则「起了谁」的断言看到的是它们。"""
    for kind in (MissionKind.SCAN, MissionKind.RANKING, MissionKind.PIRATE):
        repository.update_mission_task(_task_id(repository, kind), enabled=False)


def _add_target(  # type: ignore[no-untyped-def]
    session_factory, coordinate: Coordinate, *, military_score: float
) -> None:
    """放一颗有军力读数的 bot。**分数必须给**：没读数的 2026-08-18 起不参与攻击。"""
    with session_factory() as session:
        session.add(
            orm.BotTargetRow(
                id=uuid4(),
                galaxy=coordinate.galaxy,
                system=coordinate.system,
                position=coordinate.position,
                is_bot=True,
                military_score=military_score,
                military_score_at_utc=NOW,
            )
        )
        session.commit()


def _configure(repository: SqlAlchemyRepository, **knobs: int | None) -> None:
    """写一份全局攻击配置。没提到的旋钮一律留空（= 用默认值）。"""
    repository.replace_military_attack_tiers("[]", **knobs)


def _launched(launcher) -> list[str]:  # type: ignore[no-untyped-def]
    return list(launcher.latest.command) if launcher.spawned else []


# -- ① 撞上保护期 → 落库 ------------------------------------------------------


def test_a_protection_hit_lands_in_the_database(  # type: ignore[no-untyped-def]
    repository, session_factory
) -> None:
    """`note_protection_period` 把撞上的时刻写进 `bot_targets`。

    这是这条修复的地基：在它之前，「撞上了」只是 `system_log` 里的一句中文，
    而选靶读的是库表。164 条 `[拦下]` 一条都没能拦住下一轮。
    """
    _add_target(session_factory, STRONGEST, military_score=9_000.0)

    assert repository.note_protection_period(STRONGEST, seen_at_utc=NOW) is True

    with session_factory() as session:
        row = session.scalars(
            select(orm.BotTargetRow).where(
                orm.BotTargetRow.galaxy == STRONGEST.galaxy,
                orm.BotTargetRow.system == STRONGEST.system,
                orm.BotTargetRow.position == STRONGEST.position,
            )
        ).one()
        assert row.protection_seen_at_utc == NOW


def test_a_coordinate_with_no_row_reports_that_it_was_not_recorded(  # type: ignore[no-untyped-def]
    repository,
) -> None:
    """`bot_targets` 里没有这一行时**返回 False，而不是插一行**。

    这条链路上的坐标也可能是海盗位（1--4 号位，`clear_pirate_position_bot_candidates`
    专门在清它们）。凭一个弹窗就插一行，等于断言「这坐标是个 bot 目标」。
    返回 False 让调用方能把话说清楚——runner 那侧据此写的是 WARNING 而不是 INFO。
    """
    assert repository.note_protection_period(Coordinate(4, 393, 2), seen_at_utc=NOW) is False


def test_only_hits_inside_the_window_come_back(  # type: ignore[no-untyped-def]
    repository, session_factory
) -> None:
    """`protected_bot_targets_since` 按时刻划线，与 `attacked_bot_targets_since` 同形。"""
    _add_target(session_factory, STRONGEST, military_score=9_000.0)
    _add_target(session_factory, WEAKER, military_score=8_000.0)
    repository.note_protection_period(STRONGEST, seen_at_utc=NOW - timedelta(hours=1))
    repository.note_protection_period(WEAKER, seen_at_utc=NOW - timedelta(hours=9))

    assert repository.protected_bot_targets_since(NOW - timedelta(hours=8)) == {STRONGEST}


# -- ② 下一轮选靶不再挑中它 ----------------------------------------------------


def test_the_next_round_no_longer_picks_a_protected_target(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """**这一条就是那 11.5 分钟。**

    上一轮撞上保护期的那个不再进候选池，顺位让给下一个——而不是又一轮
    「导航过去、开面板、撞弹窗、退出来」。
    """
    _add_target(session_factory, STRONGEST, military_score=9_000.0)
    _add_target(session_factory, WEAKER, military_score=8_000.0)
    repository.note_protection_period(STRONGEST, seen_at_utc=NOW - timedelta(minutes=12))
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    command = _launched(launcher)
    assert any(part.startswith("4:445:5") for part in command), "没撞过保护期的那个该照打"
    assert not any(part.startswith("4:393:10") for part in command), (
        "撞过保护期的那个又被挑出来了——这正是每轮白烧 2.9 分钟鼠标时间的那个循环"
    )


def test_a_protected_target_is_the_only_reason_it_dropped_out(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """同一份数据、只差「撞没撞过保护期」，结果必须相反。

    不设这个对照的话，上一条的绿色可能来自任何别的闸门（没读数、超期、
    本轮走完），而那几道闸门自己都有用例——重复守一遍等于什么都没守。
    """
    _add_target(session_factory, STRONGEST, military_score=9_000.0)
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    assert any(part.startswith("4:393:10") for part in _launched(launcher))


# -- ③ 排除窗口到期后重新可选 --------------------------------------------------


def test_a_target_returns_once_the_exclusion_window_expires(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """排除**有尽头**：撞上那一刻起满 8 小时之后它重新进候选池。

    没有这一条，「不再挑中它」这句话可以由「永久删掉它」来满足——那会把候选池
    一夜一夜地锁小下去，而症状和这次修的缺陷一样静默。
    """
    _add_target(session_factory, STRONGEST, military_score=9_000.0)
    repository.note_protection_period(
        STRONGEST, seen_at_utc=NOW - DEFAULT_PROTECTION_EXCLUSION - timedelta(minutes=1)
    )
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    assert any(part.startswith("4:393:10") for part in _launched(launcher)), (
        "排除窗口过了它还进不来——那不是排除，是永久删除"
    )


# -- ④ 排除必须排在「按得分花掉航线预算」之前 ---------------------------------


def test_the_exclusion_runs_before_the_budget_is_spent(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **顺序本身就是判据。**

    ## PR #194 之后这条不变量换了载体，但**没有放宽**

    #194 把旧的第 ④ 步（军力硬截断前 `top_n` 名）与第 ⑤ 步（按距离出击）合并成了
    一条判据：`得分 = 军力 ÷ 往返小时`。**「取前 N」那道墙没有了**，所以原先那个
    「先取前 1 个 → 再排除 → 空集」的构造在新结构里造不出来。

    但**缩成空集那个失败模式还在**，只是换了个闸口：如今把候选池收窄成「这一轮
    真的派几发」的是**航线预算**（`domain.military_attack.assign_by_capacity_and_value`
    ——航线用尽就不再配对）。所以不变量原样成立，只是措辞从「取前 N 之前」变成
    「花掉航线预算之前」。

    ## 这一条构造的局面

    **只有 1 条航线**，两个候选，而**得分最高的那个正在保护期里**（两者同银河、
    往返小时相同，所以 9000 分的排在 8000 分前面）：

    - 先排除、再分配（正确）：池 = {弱}，那条航线派给弱的那个。
    - 先分配、再排除（错）：那条唯一的航线被**强的那个**占掉，排除之后 →
      **一发不派**，而弱的那个明明能打——正是实机 20:29 那一轮的形状。

    24 小时那一条当初正是因此被要求排在最前；保护期这一条是同一档判据。
    """
    _add_target(session_factory, STRONGEST, military_score=9_000.0)
    _add_target(session_factory, WEAKER, military_score=8_000.0)
    # 前置条件明写出来：得分高的那个必须正是被保护的那个，否则这条用例什么都没验。
    # 断言它而不是相信它——`domain.flight_time` 的系数将来若改，要在这里先红。
    assert attack_value(ScoredTarget(STRONGEST, military_score=9_000.0), ORIGIN) > attack_value(
        ScoredTarget(WEAKER, military_score=8_000.0), ORIGIN
    )

    repository.note_protection_period(STRONGEST, seen_at_utc=NOW - timedelta(minutes=12))
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO, fleet_lines=1)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    command = _launched(launcher)
    assert command, "一发都没派——那条唯一的航线被保护期里的目标占掉了，排除跑晚了"
    assert any(part.startswith("4:445:5") for part in command)


# -- ⑤ 旋钮 --------------------------------------------------------------------


def test_an_empty_knob_excludes_for_the_game_protection_length(  # type: ignore[no-untyped-def]
    repository, scheduler
) -> None:
    """留空 = 8 小时，**断言的是具体数字**。

    写成「等于那个常量」的自反断言，改了常量用例照样绿，等于什么都没守住。
    8 这个数的出处是游戏保护期的长度（`GAME_PROTECTION_HOURS`）——我们不知道
    保护期何时开始，所以从撞上那一刻起最多还剩 8 小时，排满 8 小时就一定够。
    """
    _configure(repository)

    assert scheduler._protection_exclusion_window() == timedelta(hours=8)
    assert GAME_PROTECTION_HOURS == 8
    assert DEFAULT_PROTECTION_EXCLUSION == timedelta(hours=8)


def test_a_configured_knob_reopens_a_target_sooner(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """配成 2 小时，3 小时前撞上的那个就重新可打。**判据落在真实选靶上**，
    不是读回配置本身——读回配置只证明它存下来了，证明不了有谁在用它。
    """
    _add_target(session_factory, STRONGEST, military_score=9_000.0)
    repository.note_protection_period(STRONGEST, seen_at_utc=NOW - timedelta(hours=3))
    _configure(repository, protection_exclusion_hours=2)
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    assert scheduler._protection_exclusion_window() == timedelta(hours=2)
    assert any(part.startswith("4:393:10") for part in _launched(launcher))
    # 同一批数据按默认的 8 小时算仍然被排除——证明上面那一发来自配置。
    assert repository.protected_bot_targets_since(NOW - DEFAULT_PROTECTION_EXCLUSION) == {STRONGEST}


@pytest.mark.parametrize("value", [0, -1, "2.5", True, "八小时"])
def test_impossible_exclusion_windows_are_refused(
    scheduler: MissionScheduler, value: object
) -> None:
    """0 等于取消排除——那正是这条功能要修的缺陷本身，所以当场拒掉，
    而不是当成「最激进的那一档」。
    """
    with pytest.raises(MissionParamError):
        scheduler.validate_protection_exclusion_hours(value)


def test_the_exclusion_window_stops_at_one_day(scheduler: MissionScheduler) -> None:
    """保护期最长 8 小时，8 以上纯属保守余量；越过一天就开始和
    `bot_revisit_hours` 争同一件事，排障时分不清目标被哪一条挡住。
    """
    assert PROTECTION_EXCLUSION_MAX_HOURS == 24
    assert scheduler.validate_protection_exclusion_hours(24) == 24
    with pytest.raises(MissionParamError):
        scheduler.validate_protection_exclusion_hours(25)


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_means_follow_the_default_not_zero(
    scheduler: MissionScheduler, blank: object
) -> None:
    """⚠️ **「没配」和「配了 0」是两回事。** 空串被读成 0 的话会撞上下界被拒，
    而那不是用户按下「保存」时的意思。
    """
    assert scheduler.validate_protection_exclusion_hours(blank) is None


# -- 旋钮被改过要在日志里留痕 --------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.records: list[SystemLogRecord] = []

    def __call__(self, batch) -> None:  # type: ignore[no-untyped-def]
        self.records.extend(batch)


@pytest.fixture
def collector() -> Iterator[_Collector]:
    """装一个假出口接住「用了非默认值」那条日志。

    ⚠️ 每次都清一遍去重账本：`record_knob_override` 按进程记「这个取值已经写过
    了」，不清的话用例之间会互相污染。抄 `test_behaviour_knobs` 的同名夹具。
    """
    reset_knob_override_memo()
    sink = _Collector()
    install_system_log_sink(SystemLogSink(sink, flush_interval_s=0.01), context=SystemLogContext())
    try:
        yield sink
    finally:
        shutdown_system_log_sink()
        reset_knob_override_memo()


def _flush() -> None:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


def test_an_overridden_exclusion_leaves_a_trace_in_the_system_log(  # type: ignore[no-untyped-def]
    repository, scheduler, collector: _Collector
) -> None:
    """一个被改过的阈值最阴的失败方式是日志里一切都像默认行为——排障的人照着
    代码里的 8 小时去推，怎么算都对不上。
    """
    _configure(repository)
    assert scheduler._protection_exclusion_window() == DEFAULT_PROTECTION_EXCLUSION
    _flush()
    assert not [item for item in collector.records if "protection_exclusion" in item.message]

    _configure(repository, protection_exclusion_hours=2)
    scheduler._protection_exclusion_window()
    _flush()

    traces = [item for item in collector.records if "protection_exclusion" in item.message]
    assert len(traces) == 1
    assert "2:00:00" in traces[0].message
    assert "8:00:00" in traces[0].message, "默认值也要写进去，否则看日志的人没有参照"
