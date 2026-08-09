"""bot 一轮里的判态查询：派遣事实与「不值得打」的标记。

这两个方法喂的是 `domain.bot_round.phase_of`，而那个纯函数的失效方式全是
**静默**的——不崩溃、不报错，只是某个目标永远停在「等战报」，于是 bot 任务
永远不退出，画面上看起来只是「在等」。所以这里测的不是「查出来几行」，
而是「查错了会让 phase_of 得出什么结论」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from evo_helper.domain.bot_round import BotPhase, DispatchFact, phase_of
from evo_helper.domain.fleet_preset import DEFAULT_PRESET
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)

TARGET = Coordinate(2, 137, 14)
PROBE = DEFAULT_PRESET.name

#: 本轮起点。所有测试都拿它当 `since`，和 runner 传的是同一个东西。
ROUND_START = datetime(2026, 8, 9, tzinfo=UTC)
LAST_ROUND = ROUND_START - timedelta(days=1)


# -- 派遣事实：哪些行算「真的派出去了」 --------------------------------------


def test_dispatch_facts_are_typed_domain_records(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """返回的是 `DispatchFact` 而不是 `Any`。

    `phase_of(self._dispatch_facts(...))` 是生产上唯一的调用点；返回 `Any`
    时 mypy 对它一点检查都做不了，字段名写错也照样过。
    """
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START)

    assert [type(fact) for fact in facts] == [DispatchFact]


def test_a_refused_dispatch_does_not_leave_the_target_awaiting_a_report(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**被游戏拒掉的那发不算派出去了。**

    算进来的话它是一条「已派出且永远收不到战报」：目标永远停在
    `AWAITING_ATTACK_REPORT`，bot 的完成态永远达不到，整个任务不退出。
    兄弟方法 `count_dispatches_since` / `pending_reports_for_kind` 都过滤了
    `accepted`，这个漏了。
    """
    _intent(repository, run_id, preset="AAA", created_at=ROUND_START, accepted=False)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START)

    assert facts == []
    assert phase_of(facts) is BotPhase.NEEDS_PROBE


def test_a_rehearsal_dispatch_does_not_leave_the_target_awaiting_a_report(
    repository, run_id
) -> None:  # type: ignore[no-untyped-def]
    """演习模式不会产生战报，同样不能算成「已派出」。"""
    _intent(repository, run_id, preset="AAA", created_at=ROUND_START, dry_run=True)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START)

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

    assert repository.bot_dispatch_facts(TARGET, since=ROUND_START) == []


def test_facts_from_an_earlier_round_are_excluded(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """上一轮打过不代表本轮打过——不按轮切开，重开一轮会立刻显示「已完成」。"""
    _intent(repository, run_id, preset="AAA", created_at=LAST_ROUND, has_report=True)

    assert repository.bot_dispatch_facts(TARGET, since=ROUND_START) == []
    assert len(repository.bot_dispatch_facts(TARGET, since=LAST_ROUND)) == 1


def test_a_pirate_intent_on_the_same_coordinate_is_not_a_bot_fact(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """海盗和 bot 可能落在同一批坐标上，判态只认 bot 那条链路的记录。"""
    _intent(
        repository,
        run_id,
        preset="AAA",
        created_at=ROUND_START,
        target_kind=TARGET_KIND_PIRATE,
    )

    assert repository.bot_dispatch_facts(TARGET, since=ROUND_START) == []


def test_a_probe_with_its_report_back_moves_the_target_to_needs_attack(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """正常那条路：探路战报回来了，该分档真打。"""
    _intent(repository, run_id, preset=PROBE, created_at=ROUND_START, has_report=True)

    assert (
        phase_of(repository.bot_dispatch_facts(TARGET, since=ROUND_START)) is BotPhase.NEEDS_ATTACK
    )


def _intent(  # type: ignore[no-untyped-def]
    repository,
    run_id,
    *,
    preset: str,
    created_at: datetime,
    target_kind: str = TARGET_KIND_BOT,
    accepted: bool = True,
    dry_run: bool = False,
    has_report: bool = False,
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
            dry_run=dry_run,
            accepted=accepted,
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
