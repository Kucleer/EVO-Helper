"""bot 一轮里的判态查询：哪些行算「真的打出去了」、每一发打成了什么。

这两个方法喂的是 `domain.bot_round.phase_of`，而那个纯函数的失效方式全是
**静默**的——不崩溃、不报错，只是某个目标永远停在「等战报」（于是 bot 任务
永远不退出，画面上看起来只是「在等」），或者反过来被从头再打一遍。所以这里测的
不是「查出来几行」，而是「查错了会让 phase_of 得出什么结论」。

战果那一列仍然查得到、仍然入库，只是**不再喂给 `phase_of`**：平局重打已按用户
口径（2026-08-17）移除。下面「战果」那一节守的正是这条界线的两边。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from evo_helper.domain.battle_outcome import OUTCOME_DRAW, OUTCOME_VICTORY
from evo_helper.domain.bot_round import BOT_ATTACK_PRESET, BotPhase, DispatchFact, phase_of
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

TARGET = Coordinate(2, 137, 14)
OTHER_TARGET = Coordinate(2, 137, 15)

#: 本轮起点。所有测试都拿它当 `since`，和 runner 传的是同一个东西。
ROUND_START = datetime(2026, 8, 9, tzinfo=UTC)
LAST_ROUND = ROUND_START - timedelta(days=1)

#: 「现在」。**必须显式给**，不能让查询取真实时钟：`bot_dispatch_facts` 会把
#: 派出超过 `MAX_REPORT_AGE` 还没战报的那些剔掉（`phase_of` 的前置条件），
#: 而这些用例的时间戳是写死的历史时刻——用真实时钟跑，每一条派遣事实都会被
#: 当成「战报永远不会来了」而消失，测试当天能过、第二天就红。
NOW = ROUND_START + timedelta(hours=1)


# -- 派遣事实：哪些行算「真的打出去了」 --------------------------------------


def test_dispatch_facts_are_typed_domain_records(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """返回的是 `DispatchFact` 而不是 `Any`。

    `phase_of(self._dispatch_facts(...))` 是生产上唯一的调用点；返回 `Any`
    时 mypy 对它一点检查都做不了，字段名写错也照样过。
    """
    _intent(repository, run_id, created_at=ROUND_START)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW)

    assert [type(fact) for fact in facts] == [DispatchFact]


def test_a_refused_dispatch_does_not_leave_the_target_awaiting_a_report(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**被游戏拒掉的那发不算派出去了。**

    算进来的话它是一条「已派出且永远收不到战报」：目标永远停在
    `AWAITING_ATTACK_REPORT`，bot 的完成态永远达不到，整个任务不退出。
    兄弟方法 `count_dispatches_since` / `pending_reports_for_kind` 都过滤了
    `accepted`，这个漏过。
    """
    _intent(repository, run_id, created_at=ROUND_START, accepted=False)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW)

    assert facts == []
    assert phase_of(facts) is BotPhase.NEEDS_ATTACK


def test_an_intent_with_no_dispatch_at_all_is_not_a_fact(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """闸门拦下、根本没走到派遣面板的意图不构成派遣事实。"""
    repository.save_attack_intent(
        AttackIntent(
            intent_id=uuid4(),
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=TARGET,
            preset=FleetPresetRef(name=BOT_ATTACK_PRESET, signature="sig"),
            cycle_start_utc=ROUND_START,
            created_at_utc=ROUND_START,
            target_kind=TARGET_KIND_BOT,
        )
    )

    assert repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW) == []


def test_facts_from_an_earlier_round_are_excluded(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """上一轮打过不代表本轮打过。

    不按轮切开的话，昨天那一发会被当成本轮打过的，这个目标今天一发都不会派——
    而重开一轮的页面上它会立刻显示「已完成」。
    """
    _intent(repository, run_id, created_at=LAST_ROUND, has_report=True)

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
    _intent(repository, run_id, created_at=ROUND_START, target_kind=TARGET_KIND_PIRATE)

    assert repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW) == []


def test_a_scout_dispatch_is_not_a_bot_round_fact(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**侦察发不产生战报，判态时算进来就是永久卡死。**

    `phase_of` 连预设名都不看了（bot 一律 BBB），更认不出「这一发根本不会有
    战报」。一条侦察发混进来就会被当成攻击发：目标永远停在
    `AWAITING_ATTACK_REPORT`，bot 任务永远不退出，而画面上只是「在等」。
    PR #95 修的正是这个形状。
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
    assert phase_of(facts) is BotPhase.NEEDS_ATTACK


def test_the_attack_report_is_what_finishes_the_target(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**只有把战报入库才到得了 `DONE`。**

    调度器数「还有几个没走完」用的就是这条（`mission_scheduler._bot_remaining`），
    所以没人去收攻击战报时，这一轮在调度器眼里永远没跑完，攻击日志上那一行也
    永远停在「战果 待战报」（实机 2026-08-11，AAA 那一发）。
    """
    _intent(repository, run_id, created_at=ROUND_START)

    waiting = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW)
    assert phase_of(waiting) is BotPhase.AWAITING_ATTACK_REPORT

    _close_dispatch_at(repository, ROUND_START, outcome=OUTCOME_VICTORY)

    assert (
        phase_of(repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW))
        is BotPhase.DONE
    )


# -- 战果：入库照旧，但不再影响判态 ------------------------------------------


def test_a_draw_report_finishes_the_target(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**平局也走完。** 用户口径（2026-08-17）：「bot 攻击移除平局再打一次机制」。

    这条原先叫 `test_the_stored_outcome_is_carried_into_the_fact`，钉的是反过来的
    口径（平局 → `NEEDS_ATTACK`，好让链路补刀）。规则移除之后它改钉新口径，
    而不是删掉：删掉的话，「平局又被接回去重打」就没有任何一层拦得住，
    而复发的样子是链路悄悄多烧航线，日志上只是一句「又打了一发」。

    ⚠️ **战果本身照旧写在库里。** 这条只说 `phase_of` 不看它；那一行
    `battle_reports.outcome` 仍是 `DRAW`，日志页与情报中心照样显示、照样筛得出来。
    """
    _intent(repository, run_id, created_at=ROUND_START, has_report=True, outcome=OUTCOME_DRAW)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW)

    assert phase_of(facts) is BotPhase.DONE


def test_the_outcome_column_is_still_written(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """判态不看战果 ≠ 战果没写进去。

    上一条只证明了「平局不再补刀」。要是有人顺手把战果那一列一起停写了，
    上一条照样绿——而攻击日志上那一列会静默变成一片空白。这条守住的是那半边：
    库里存的仍然是 `DRAW` 这个词本身。
    """
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    _intent(repository, run_id, created_at=ROUND_START, has_report=True, outcome=OUTCOME_DRAW)

    with repository._session_factory() as session:  # noqa: SLF001
        stored = list(session.scalars(select(orm.BattleReportRow.outcome)))

    assert stored == [OUTCOME_DRAW]


def test_a_lost_report_frees_the_target_to_be_attacked_again(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """派出超过 `MAX_REPORT_AGE` 还没战报的那一发整条剔掉。

    平局重打移除之后，**这是唯一还会让同一坐标本轮再吃一发的路径**，而它管的是
    「这一发的结果永远拿不到」，不是「结果不满意」。不剔的代价则是它这一整轮
    再也不动，而画面上只是「在等」。
    """
    from evo_helper.domain.report_wait import MAX_REPORT_AGE

    _intent(repository, run_id, created_at=ROUND_START)
    much_later = ROUND_START + MAX_REPORT_AGE + timedelta(minutes=1)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=much_later)

    assert facts == []
    assert phase_of(facts) is BotPhase.NEEDS_ATTACK


def test_many_dispatch_facts_match_the_single_target_query(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """军力候选池一次量几千个坐标时，批量查询不能改变任一目标的判态口径。"""
    _intent(repository, run_id, created_at=ROUND_START, target=TARGET)
    _intent(repository, run_id, created_at=ROUND_START + timedelta(minutes=1), target=OTHER_TARGET)

    batch = repository.bot_dispatch_facts_many(
        [TARGET, OTHER_TARGET], since=ROUND_START, now_utc=NOW
    )

    assert batch == {
        TARGET: repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW),
        OTHER_TARGET: repository.bot_dispatch_facts(OTHER_TARGET, since=ROUND_START, now_utc=NOW),
    }


def test_attacked_bot_targets_since_only_includes_accepted_recent_attacks(  # type: ignore[no-untyped-def]
    repository, run_id
) -> None:
    _intent(repository, run_id, created_at=NOW - timedelta(hours=23), target=TARGET)
    _intent(repository, run_id, created_at=NOW - timedelta(hours=25), target=OTHER_TARGET)
    _intent(
        repository,
        run_id,
        created_at=NOW - timedelta(hours=1),
        target=OTHER_TARGET,
        accepted=False,
    )

    assert repository.attacked_bot_targets_since(NOW - timedelta(hours=24)) == {TARGET}


# -- 翻信箱要的两个时刻 ------------------------------------------------------


def test_the_open_dispatch_hands_out_both_clocks(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """`bot_report_due_at` 一次交出**派出时刻**和**预计战报时刻**。

    两者用途完全不同：前者是翻信箱的时间下界（列表按时间倒序，比它还早的报告
    不可能是这一发的），后者只用来把日志上那句话说准。分两次查就会有人只查一个，
    然后拿预计时刻去当下界——那是个 OCR 读数，实机上同距离的六发读出 8 秒到
    25 分钟不等，读大一次就能把真报告永久挡在窗口外，而且完全静默。
    """
    _intent(repository, run_id, created_at=ROUND_START)
    _set_expected_report(repository, ROUND_START + timedelta(minutes=25))

    due = repository.bot_report_due_at([TARGET], since=ROUND_START)

    assert due == {TARGET: (ROUND_START, ROUND_START + timedelta(minutes=25))}


def test_a_dispatch_whose_report_is_back_is_not_waiting_any_more(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """战报已经收到的那一发不再交出来——它没有什么可等的了。

    留着它只会把时间下界压到更早，于是每一趟都往回多翻几屏旧邮件。
    """
    _intent(repository, run_id, created_at=ROUND_START, has_report=True)

    assert repository.bot_report_due_at([TARGET], since=ROUND_START) == {}


def test_a_refused_dispatch_is_not_waiting_either(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """被游戏拒掉的那一发没有舰队飞出去，也就永远不会有战报。

    和 `bot_dispatch_facts` / `count_dispatches_since` 同一条口径：算进来就是
    一个永远等不到的下界。
    """
    _intent(repository, run_id, created_at=ROUND_START, accepted=False)

    assert repository.bot_report_due_at([TARGET], since=ROUND_START) == {}


def test_the_earliest_open_dispatch_wins(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """同一个坐标有多发未闭合时取**最早**那一发。

    战报丢失后允许重来一发，于是这个坐标上会同时挂着两发，所以这条不是假想情形
    （平局重打已于 2026-08-17 移除，但那条路径与它无关）。翻信箱要的是覆盖全部
    候选的下界；取最晚那一发会把更早那一发的报告挡在外面。
    """
    _intent(repository, run_id, created_at=ROUND_START)
    _intent(repository, run_id, created_at=ROUND_START + timedelta(minutes=10))

    due = repository.bot_report_due_at([TARGET], since=ROUND_START)

    assert due[TARGET][0] == ROUND_START


def test_last_rounds_dispatch_does_not_drag_the_floor_back(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """上一轮的派遣不参与本轮：`since` 之前的那些不该把下界拖到昨天去。"""
    _intent(repository, run_id, created_at=LAST_ROUND)

    assert repository.bot_report_due_at([TARGET], since=ROUND_START) == {}


def _set_expected_report(repository, expected: datetime) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001
        for row in session.scalars(select(orm.AttackDispatchRow)):
            row.expected_report_at_utc = expected
        session.commit()


def _intent(  # type: ignore[no-untyped-def]
    repository,
    run_id,
    *,
    created_at: datetime,
    preset: str = BOT_ATTACK_PRESET,
    target_kind: str = TARGET_KIND_BOT,
    accepted: bool = True,
    has_report: bool = False,
    outcome: str | None = None,
    mission_kind: str = MISSION_KIND_ATTACK,
    target: Coordinate = TARGET,
):
    """一条针对 ``target`` 的意图 + 它的派遣（+ 可选的战报）。"""
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=target,
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
        _attach_report(repository, dispatch_id, created_at, outcome)
    return intent_id


def _close_dispatch_at(  # type: ignore[no-untyped-def]
    repository, dispatched_at: datetime, *, outcome: str | None = None
) -> None:
    """给这个时刻派出的那一发挂上战报。用于「后来那一发的战报回来了」。"""
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001
        dispatch_id = session.scalar(
            select(orm.AttackDispatchRow.id).where(
                orm.AttackDispatchRow.dispatched_at_utc == dispatched_at
            )
        )
    assert dispatch_id is not None
    _attach_report(repository, dispatch_id, dispatched_at, outcome)


def _attach_report(repository, dispatch_id, reported_at: datetime, outcome: str | None) -> None:  # type: ignore[no-untyped-def]
    """直接把战报挂到指定派遣上，绕开 `append_report` 的坐标+时间匹配。

    这几条测的是「有没有战报、战果是什么」这两个事实，不该顺带依赖认领算法。
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
                outcome=outcome,
            )
        )
        session.commit()
