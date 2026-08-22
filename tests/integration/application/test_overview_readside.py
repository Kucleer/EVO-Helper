"""数据概览页向调度器要的那两样：航线配置与候选池。

⚠️ **这两个读口存在的全部理由，是让页面和调度器量同一把尺子**
（需求文档 8.7：「页面自己不许再造判据」）。所以这一份钉的不是「函数返回了
什么」，而是「它和调度判据是不是同一份」：

- 航线数只能来自 `mission_task_origins.fleet_lines`（8.3：每颗星球各不相同，
  实测 5 条 / 4 条，不许写死）；
- 候选池必须走 `_military_candidates`——近期打过的、刚撞上保护期的都得少掉，
  而这两个窗口都是用户能在攻击配置页上改的**策略**，页面另算一份的话，
  它显示的池子和调度器下一轮真的会挑的那批不是同一个东西。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.models import Coordinate
from evo_helper.domain.report_wait import UNKNOWN_LINE_HOLD
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)

HOME = Coordinate(4, 277, 15)
SECOND = Coordinate(9, 250, 8)
#: 实测每颗星球各自配的航线数（需求文档 8.3 点名的那两个数）。两颗故意不一样：
#: 配成同一个数的话，「按每颗星球各自的配置画」这条写死一个常量也能过。
HOME_LINES = 5
SECOND_LINES = 4

TARGET_A = Coordinate(3, 141, 9)
TARGET_B = Coordinate(5, 200, 7)
#: ⚠️ 三个坐标的位置号都在 5 以上：1–4 号位是海盗位，选靶那一侧另有判据把它们
#: 挡掉（`clear_pirate_position_bot_candidates`），拿它当样本会让这几条用例
#: 断在一条与本文件无关的判据上。
TARGET_C = Coordinate(7, 310, 12)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


def _bot_task(repository: SqlAlchemyRepository) -> int:
    return next(row.id for row in repository.mission_tasks() if row.kind == MissionKind.BOT.value)


def _configure_origins(repository: SqlAlchemyRepository) -> int:
    task_id = _bot_task(repository)
    repository.update_mission_task(task_id, params_json='{"by_military": true}')
    home = repository.create_attack_planet(HOME)
    second = repository.create_attack_planet(SECOND)
    repository.replace_mission_task_origins(
        task_id, [(home.id, HOME_LINES, True), (second.id, SECOND_LINES, True)]
    )
    return task_id


def _add_target(session_factory, coordinate: Coordinate, *, score: float) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        session.add(
            orm.BotTargetRow(
                id=uuid4(),
                galaxy=coordinate.galaxy,
                system=coordinate.system,
                position=coordinate.position,
                is_bot=True,
                military_score=score,
                military_score_at_utc=NOW,
            )
        )
        session.commit()


# -- 航线配置 -------------------------------------------------------------------


def test_each_planet_reports_its_own_configured_line_count(  # type: ignore[no-untyped-def]
    repository, scheduler
) -> None:
    """⚠️ 每颗星球的航线数**各不相同**，只能从 `mission_task_origins` 读。"""
    _configure_origins(repository)

    origins = scheduler.configured_line_origins()

    assert [(item.coordinate, item.fleet_lines) for item in origins] == [
        (HOME, HOME_LINES),
        (SECOND, SECOND_LINES),
    ]


def test_a_disabled_origin_is_still_reported_so_the_page_can_tell(  # type: ignore[no-untyped-def]
    repository, scheduler
) -> None:
    """停用的那颗要带出来（连同 `enabled=False`）。

    直接不给的话，「用户把 2 号星停掉了」这件事在页面上和「这颗星球被删了」
    长得一模一样。
    """
    task_id = _configure_origins(repository)
    planets = repository.attack_planets()
    repository.replace_mission_task_origins(
        task_id,
        [
            (planets[0].id, HOME_LINES, True),
            (planets[1].id, SECOND_LINES, False),
        ],
    )

    origins = scheduler.configured_line_origins()

    assert [item.enabled for item in origins] == [True, False]


def test_a_task_without_configured_origins_reports_nothing(  # type: ignore[no-untyped-def]
    repository, scheduler
) -> None:
    """没有军力攻击任务时给空——那是「没有这条链路」，不是「配了 0 条航线」。"""
    assert scheduler.configured_line_origins() == ()


def test_the_page_and_the_scheduler_read_the_same_unknown_line_hold(  # type: ignore[no-untyped-def]
    repository, scheduler
) -> None:
    """⚠️ 页面不许写死 90 分钟（需求文档 8.1）。

    用户在攻击配置页把它改成 45 之后，`unknown_line_hold()` 必须跟着变——
    页面就是靠它判「这一发还占不占航线」的。
    """
    assert scheduler.unknown_line_hold() == UNKNOWN_LINE_HOLD

    repository.replace_military_attack_tiers("[]", unknown_line_hold_minutes=45)

    assert scheduler.unknown_line_hold() == timedelta(minutes=45)


# -- 候选池 ---------------------------------------------------------------------


def test_the_candidate_pool_excludes_targets_that_just_hit_a_protection_period(  # type: ignore[no-untyped-def]
    repository, scheduler, session_factory
) -> None:
    """⚠️ 页面**不许**自己筛（需求文档 8.7）。

    刚撞上 8 小时保护期的那个必须少掉——它是调度器下一轮也挑不到的那一个。
    页面另写一份筛选的话，「候选池 510 个」里会混进一批此刻根本打不了的目标。
    """
    _configure_origins(repository)
    for target in (TARGET_A, TARGET_B, TARGET_C):
        _add_target(session_factory, target, score=9_000.0)

    assert {item.coordinate for item in scheduler.military_candidate_pool()} == {
        TARGET_A,
        TARGET_B,
        TARGET_C,
    }

    repository.note_protection_period(TARGET_B, seen_at_utc=NOW - timedelta(hours=1))

    assert {item.coordinate for item in scheduler.military_candidate_pool()} == {
        TARGET_A,
        TARGET_C,
    }


def test_the_candidate_pool_follows_the_configured_protection_window(  # type: ignore[no-untyped-def]
    repository, scheduler, session_factory
) -> None:
    """排除窗口是**策略**，用户能改。改小之后那个目标要回到池子里。

    这一条钉的是「页面跟着旋钮走」——写死一个 8 小时的话，用户把它调成 1 小时，
    页面上的池子纹丝不动。
    """
    _configure_origins(repository)
    _add_target(session_factory, TARGET_A, score=9_000.0)
    repository.note_protection_period(TARGET_A, seen_at_utc=NOW - timedelta(hours=2))

    assert scheduler.military_candidate_pool() == ()

    repository.replace_military_attack_tiers("[]", protection_exclusion_hours=1)

    assert {item.coordinate for item in scheduler.military_candidate_pool()} == {TARGET_A}


def test_the_candidate_pool_is_empty_without_a_military_task(  # type: ignore[no-untyped-def]
    repository, scheduler, session_factory
) -> None:
    """没有军力攻击任务时给空元组——「没有这条链路」和「池子空了」页面上要分得开。"""
    _add_target(session_factory, TARGET_A, score=9_000.0)

    assert scheduler.military_candidate_pool() == ()


def test_the_candidate_pool_carries_the_military_reading(  # type: ignore[no-untyped-def]
    repository, scheduler, session_factory
) -> None:
    """页面要按往返时长分档，也要说得出「其中几个没有军力读数」。"""
    _configure_origins(repository)
    _add_target(session_factory, TARGET_A, score=1_234.0)

    pool = scheduler.military_candidate_pool()

    assert [(item.coordinate, item.military_score) for item in pool] == [(TARGET_A, 1_234.0)]


# -- 「那一轮开始时配着几条航线」 ------------------------------------------------


def test_a_launch_records_how_many_lines_were_configured(  # type: ignore[no-untyped-def]
    repository, scheduler, launcher
) -> None:
    """⚠️ **航线数必须在起子进程那一刻记进 `mission_runs`。**

    数据概览页的利用率分母是「周期总时长 × 航线数」，而航线数会变（用户
    2026-08-20 把 4 条加到 9 条）。读页面时现取「此刻配着几条」去乘历史那些天，
    会把 08-15（当时 4 条）低估到 44%——**而页面上一点异样都看不出来**。

    两颗星球各 5 条 / 4 条，相加是 9：它们抢的是同一批航线里的份额，
    这个数是账号级的。
    """
    _configure_origins(repository)
    pirate = next(
        row.id for row in repository.mission_tasks() if row.kind == MissionKind.PIRATE.value
    )
    repository.update_mission_task(pirate, enabled=True)
    scheduler.start()

    scheduler.tick()

    assert launcher.spawned, "这一趟没起子进程，下面那条断言就什么都没守住"
    assert repository.mission_runs(limit=1)[0].configured_lines == HOME_LINES + SECOND_LINES


def test_a_launch_with_no_configured_origin_records_none_not_zero(  # type: ignore[no-untyped-def]
    repository, scheduler, launcher
) -> None:
    """⚠️ **一颗出发星球都没配时记 None，不许记 0。**

    「配了 0 条」和「不知道」在库里长得一样，而 0 会让那一天的分母变成 0、
    利用率整段显示成「—」，把那天真打出去的活抹掉。None 的意思是「这一行说不出
    线数」，页面会退回下界推算。
    """
    pirate = next(
        row.id for row in repository.mission_tasks() if row.kind == MissionKind.PIRATE.value
    )
    repository.update_mission_task(pirate, enabled=True)
    scheduler.start()

    scheduler.tick()

    assert launcher.spawned
    assert repository.mission_runs(limit=1)[0].configured_lines is None
