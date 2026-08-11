"""bot 一轮里的判态查询：派遣事实与「不值得打」的标记。

这两个方法喂的是 `domain.bot_round.phase_of`，而那个纯函数的失效方式全是
**静默**的——不崩溃、不报错，只是某个目标永远停在「等战报」，于是 bot 任务
永远不退出，画面上看起来只是「在等」。所以这里测的不是「查出来几行」，
而是「查错了会让 phase_of 得出什么结论」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from evo_helper.domain.bot_round import BotPhase, DispatchFact, phase_of
from evo_helper.domain.fleet_preset import DEFAULT_PRESET
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
from evo_helper.storage.repository import REVISIT_SCOPE_TIER_NEGLIGIBLE

TARGET = Coordinate(2, 137, 14)
PROBE = DEFAULT_PRESET.name

#: 本轮起点。所有测试都拿它当 `since`，和 runner 传的是同一个东西。
ROUND_START = datetime(2026, 8, 9, tzinfo=UTC)
LAST_ROUND = ROUND_START - timedelta(days=1)

#: 「现在」。**必须显式给**，不能让查询取真实时钟：`bot_dispatch_facts` 会把
#: 派出超过 `MAX_REPORT_AGE` 还没战报的那些剔掉（`phase_of` 的前置条件），
#: 而这些用例的时间戳是写死的历史时刻——用真实时钟跑，每一条派遣事实都会被
#: 当成「战报永远不会来了」而消失，测试当天能过、第二天就红。
NOW = ROUND_START + timedelta(hours=1)


# -- 派遣事实：哪些行算「真的派出去了」 --------------------------------------


def test_dispatch_facts_are_typed_domain_records(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """返回的是 `DispatchFact` 而不是 `Any`。

    `phase_of(self._dispatch_facts(...))` 是生产上唯一的调用点；返回 `Any`
    时 mypy 对它一点检查都做不了，字段名写错也照样过。
    """
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW)

    assert [type(fact) for fact in facts] == [DispatchFact]


def test_a_refused_dispatch_does_not_leave_the_target_awaiting_a_report(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**被游戏拒掉的那发不算派出去了。**

    算进来的话它是一条「已派出且永远收不到战报」：目标永远停在
    `AWAITING_ATTACK_REPORT`，bot 的完成态永远达不到，整个任务不退出。
    兄弟方法 `count_dispatches_since` / `pending_reports_for_kind` 都过滤了
    `accepted`，这个漏了。
    """
    _intent(repository, run_id, preset="AAA", created_at=ROUND_START, accepted=False)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW)

    assert facts == []
    assert phase_of(facts) is BotPhase.NEEDS_PROBE


def test_an_intent_with_no_dispatch_at_all_is_not_a_fact(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """闸门拦下、根本没走到派遣面板的意图不构成派遣事实。"""
    repository.save_attack_intent(
        AttackIntent(
            intent_id=uuid4(),
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=TARGET,
            preset=FleetPresetRef(name=PROBE, signature="sig"),
            cycle_start_utc=ROUND_START,
            created_at_utc=ROUND_START,
            target_kind=TARGET_KIND_BOT,
        )
    )

    assert repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW) == []


def test_facts_from_an_earlier_round_are_excluded(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """上一轮打过不代表本轮打过——不按轮切开，重开一轮会立刻显示「已完成」。"""
    _intent(repository, run_id, preset="AAA", created_at=LAST_ROUND, has_report=True)

    assert repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW) == []
    assert (
        len(
            repository.bot_dispatch_facts(
                TARGET, since=LAST_ROUND, now_utc=LAST_ROUND + timedelta(hours=1)
            )
        )
        == 1
    )


def test_a_pirate_intent_on_the_same_coordinate_is_not_a_bot_fact(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """海盗和 bot 可能落在同一批坐标上，判态只认 bot 那条链路的记录。"""
    _intent(
        repository,
        run_id,
        preset="AAA",
        created_at=ROUND_START,
        target_kind=TARGET_KIND_PIRATE,
    )

    assert repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW) == []


def test_a_scout_dispatch_is_not_a_bot_round_fact(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**侦察发不产生战报，判态时算进来就是永久卡死。**

    `phase_of` 只按预设名分探路发和攻击发，认不出「这一发根本不会有战报」。
    一条侦察发若带着非探路的预设名混进来，它会被当成攻击发：目标永远停在
    `AWAITING_ATTACK_REPORT`，bot 任务永远不退出，而画面上只是「在等」。
    """
    _intent(
        repository,
        run_id,
        preset="侦察",
        created_at=ROUND_START,
        mission_kind=MISSION_KIND_SCOUT,
    )

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW)

    assert facts == []
    assert phase_of(facts) is BotPhase.NEEDS_PROBE


def test_a_probe_with_its_report_back_moves_the_target_to_needs_attack(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """正常那条路：探路战报回来了，该分档真打。"""
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START, has_report=True)

    assert (
        phase_of(repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW))
        is BotPhase.NEEDS_ATTACK
    )


# -- 翻信箱要的两个时刻 ------------------------------------------------------


def test_the_open_dispatch_hands_out_both_clocks(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """`bot_report_due_at` 一次交出**派出时刻**和**预计战报时刻**。

    两者用途完全不同：前者是翻信箱的时间下界（列表按时间倒序，比它还早的报告
    不可能是这一发的），后者只用来把日志上那句话说准。分两次查就会有人只查一个，
    然后拿预计时刻去当下界——那是个 OCR 读数，实机上同距离的六发读出 8 秒到
    25 分钟不等，读大一次就能把真报告永久挡在窗口外，而且完全静默。
    """
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START)
    _set_expected_report(repository, ROUND_START + timedelta(minutes=25))

    due = repository.bot_report_due_at([TARGET], since=ROUND_START)

    assert due == {TARGET: (ROUND_START, ROUND_START + timedelta(minutes=25))}


def test_a_dispatch_whose_report_is_back_is_not_waiting_any_more(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """战报已经收到的那一发不再交出来——它没有什么可等的了。

    留着它只会把时间下界压到更早，于是每一趟都往回多翻几屏旧邮件。
    """
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START, has_report=True)

    assert repository.bot_report_due_at([TARGET], since=ROUND_START) == {}


def test_a_refused_dispatch_is_not_waiting_either(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """被游戏拒掉的那一发没有舰队飞出去，也就永远不会有战报。

    和 `bot_dispatch_facts` / `count_dispatches_since` 同一条口径：算进来就是
    一个永远等不到的下界。
    """
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START, accepted=False)

    assert repository.bot_report_due_at([TARGET], since=ROUND_START) == {}


def test_the_earliest_open_dispatch_wins(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """同一个坐标有多发未闭合时取**最早**那一发。

    翻信箱要的是覆盖全部候选的下界；取最晚那一发会把更早那一发的报告挡在外面。
    """
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START)
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START + timedelta(minutes=10))

    due = repository.bot_report_due_at([TARGET], since=ROUND_START)

    assert due[TARGET][0] == ROUND_START


def test_last_rounds_dispatch_does_not_drag_the_floor_back(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """上一轮的派遣不参与本轮：`since` 之前的那些不该把下界拖到昨天去。"""
    _intent(repository, run_id, preset=PROBE, created_at=LAST_ROUND)

    assert repository.bot_report_due_at([TARGET], since=ROUND_START) == {}


def _set_expected_report(repository, expected: datetime) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001
        for row in session.scalars(select(orm.AttackDispatchRow)):
            row.expected_report_at_utc = expected
        session.commit()


# -- 「不值得打」的标记 ------------------------------------------------------


def test_a_skipped_target_counts_as_done(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """分档判定不值得打，本轮就不会再有攻击发，算走完。"""
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START, has_report=True)
    repository.mark_bot_target_skipped(TARGET, since=ROUND_START)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW)

    assert [fact.skipped for fact in facts] == [True]
    assert phase_of(facts) is BotPhase.DONE


def test_skipping_leaves_the_guard_status_alone(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**`guard_status` 已经被别人占着。**

    `application/workflow.py` 往那一列写 `ALLOWED` / `REFUSED`，`logs.html` 把它
    渲染成「未派出 · {guard_status}」。塞第三套词汇进去，日志页会给出错误的
    未派出原因——一发确实飞出去了的攻击，会显示成「未派出 · SKIPPED_NEGLIGIBLE」。
    """
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START, has_report=True)
    before = _intent_guard_statuses(repository)

    repository.mark_bot_target_skipped(TARGET, since=ROUND_START)

    assert _intent_guard_statuses(repository) == before


def test_the_skip_is_recorded_as_a_revisit_with_its_own_scope(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """记在 `target_revisits` 上，用独立 scope，与「战报缺失」那批分得开。"""
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START, has_report=True)

    repository.mark_bot_target_skipped(TARGET, since=ROUND_START)

    rows = _revisits(repository)
    assert [row.scope for row in rows] == [REVISIT_SCOPE_TIER_NEGLIGIBLE]
    assert [(row.target_galaxy, row.target_system, row.target_position) for row in rows] == [
        (TARGET.galaxy, TARGET.system, TARGET.position)
    ]


def test_a_skip_is_not_counted_as_a_pending_revisit(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**别把控制台的「待复查」计数撑起来。**

    `persistent_service` 数的是 `status == "PENDING"` 的复查行，missions 页把它
    显示成「待复查」。分档说不值得打是一个**已经下完的判定**，不是等人去做的
    活；用默认的 PENDING 写进去，每跳过一个 bot 就凭空多一条谁也不会去执行的
    复查请求。
    """
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START, has_report=True)

    repository.mark_bot_target_skipped(TARGET, since=ROUND_START)

    assert [row.status for row in _revisits(repository)] == ["DONE"]


def test_skipping_a_target_with_nothing_this_round_marks_nothing(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**本轮没碰过这个目标就什么都不写。**

    原先它把所有匹配行一起改，那句 `.order_by(created_at_utc)` 在「全部都改」
    的语境下是死代码，恰恰说明作者想的是「那一条」。落到复查表上，「那一条」
    就是「本轮真的探过路，才记这一轮的判定」——本轮没有依据，就没有判定可记。
    """
    _intent(repository, run_id, preset=PROBE, created_at=LAST_ROUND, has_report=True)

    repository.mark_bot_target_skipped(TARGET, since=ROUND_START)

    assert _revisits(repository) == []


def test_the_skip_belongs_to_one_round_and_is_written_once(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """一轮一条。同轮里探了两次、或者这一趟重跑了，都不该越堆越多。"""
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START, has_report=True)
    _intent(
        repository,
        run_id,
        preset=PROBE,
        created_at=ROUND_START + timedelta(hours=1),
        has_report=True,
    )

    repository.mark_bot_target_skipped(TARGET, since=ROUND_START)
    repository.mark_bot_target_skipped(TARGET, since=ROUND_START)

    rows = _revisits(repository)
    assert len(rows) == 1
    assert rows[0].requested_at_utc >= ROUND_START


def test_a_missing_round_start_is_rejected_rather_than_wiping_history(repository) -> None:  # type: ignore[no-untyped-def]
    """`since` 必填。**这条守的是那个删不掉的逃生口。**

    原先它可空，而 `None` 在查询侧的含义是「不限时间范围」：手工跑一次
    `--probe --attack`，只要有一个目标被判成「不值得打」，这个坐标历史上
    每一轮的记录就全被刷掉。宁可当场报错，也不要静默改写历史。
    """
    with pytest.raises((TypeError, ValueError, AttributeError)):
        repository.mark_bot_target_skipped(TARGET, since=None)


def _intent_guard_statuses(repository):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001 - 直接看列，不经查询
        return [
            row.guard_status
            for row in session.scalars(
                select(orm.AttackIntentRow).order_by(orm.AttackIntentRow.created_at_utc)
            )
        ]


def _revisits(repository):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001
        return list(
            session.scalars(
                select(orm.TargetRevisitRow).order_by(orm.TargetRevisitRow.requested_at_utc)
            )
        )


def _intent(  # type: ignore[no-untyped-def]
    repository,
    run_id,
    *,
    preset: str,
    created_at: datetime,
    target_kind: str = TARGET_KIND_BOT,
    accepted: bool = True,
    has_report: bool = False,
    mission_kind: str = MISSION_KIND_ATTACK,
):
    """一条针对 `TARGET` 的意图 + 它的派遣（+ 可选的战报）。"""
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=TARGET,
            preset=FleetPresetRef(name=preset, signature="sig"),
            # `save_attack_intent` 按 (target, cycle_start, forced_revisit) 去重，
            # 所以同一个目标的多条意图必须给不同的 cycle_start。
            cycle_start_utc=created_at,
            created_at_utc=created_at,
            target_kind=target_kind,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=created_at,
            accepted=accepted,
            mission_kind=mission_kind,
        )
    )
    if has_report:
        _attach_report(repository, dispatch_id, created_at)
    return intent_id


def _attach_report(repository, dispatch_id, reported_at: datetime) -> None:  # type: ignore[no-untyped-def]
    """直接把战报挂到指定派遣上，绕开 `append_report` 的坐标+时间匹配。

    这几条测的是「有没有战报」这一个事实，不该顺带依赖认领算法。
    """
    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001
        session.add(
            orm.BattleReportRow(
                id=uuid4(),
                dispatch_id=dispatch_id,
                reported_at_utc=reported_at,
                attacker_origin_galaxy=2,
                attacker_origin_system=137,
                attacker_origin_position=18,
                defender_target_galaxy=TARGET.galaxy,
                defender_target_system=TARGET.system,
                defender_target_position=TARGET.position,
            )
        )
        session.commit()
