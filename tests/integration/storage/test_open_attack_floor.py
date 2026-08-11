"""开工翻信箱要往回翻到哪一行为止：`oldest_open_attack_at`。

开工那一趟信箱正常只翻到**今天的 UTC 日界**——列表按时间倒序，再往下都是昨天的，
与今天的配额无关。要往回多翻的情况只有一种：**跨过 UTC 午夜还在等的那一发**。
它的战报写着昨天的时间，翻到日界就停的话永远读不到，那一发要一直挂到
`MAX_REPORT_AGE`（6 小时）才被判缺失，bot 那边还要连带把目标退回去重打一遍。

所以下界取「日界」与「最早那发还在等战报的攻击派于何时」的更早者
（`tools.pirate_loop.PirateLoop._report_floor`）。问库而不是无条件往回翻六小时：
没有在等的派遣时（绝大多数时候）一行都不多翻。

这个查询的三条排除各自都有成因，见下面每一条测试。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    MISSION_KIND_SCOUT,
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
    FleetPresetRef,
)

NOW = datetime(2026, 8, 11, 6, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=6)
ORIGIN = Coordinate(2, 137, 18)


def _dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id: UUID,
    *,
    at: datetime,
    kind: str = TARGET_KIND_PIRATE,
    mission: str = "ATTACK",
    accepted: bool = True,
    target: Coordinate | None = None,
) -> UUID:
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=ORIGIN,
            target=target or Coordinate(2, 137, 1),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=at,
            created_at_utc=at,
            target_kind=kind,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=at,
            accepted=accepted,
            mission_kind=mission,
        )
    )
    return dispatch_id


def _report(repository, *, at: datetime, target: Coordinate | None = None) -> None:  # type: ignore[no-untyped-def]
    repository.append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=at,
            attacker_origin=ORIGIN,
            defender_target=target or Coordinate(2, 137, 1),
            raw_time_text=at.strftime("%d/%m/%Y %H:%M:%S"),
        )
    )


def test_nothing_open_means_no_extra_paging(repository) -> None:  # type: ignore[no-untyped-def]
    """一发都没在等，就没有任何理由往日界以外翻。"""
    assert (
        repository.oldest_open_attack_at(TARGET_KIND_PIRATE, now_utc=NOW, max_age=MAX_AGE) is None
    )


def test_the_oldest_dispatch_still_awaiting_its_report_wins(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """取的是**最早**那一发：窗口要覆盖得住所有还在等的。"""
    older = NOW - timedelta(hours=5)
    _dispatch(repository, run_id, at=older)
    _dispatch(repository, run_id, at=NOW - timedelta(hours=1), target=Coordinate(2, 137, 2))

    assert repository.oldest_open_attack_at(TARGET_KIND_PIRATE, now_utc=NOW, max_age=MAX_AGE) == (
        older
    )


def test_a_dispatch_whose_report_arrived_is_not_open(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """战报已经收到的那一发不再是「在等」——否则窗口会一直被历史钉在最早那天。"""
    older = NOW - timedelta(hours=5)
    _dispatch(repository, run_id, at=older)
    _report(repository, at=older + timedelta(minutes=10))

    assert (
        repository.oldest_open_attack_at(TARGET_KIND_PIRATE, now_utc=NOW, max_age=MAX_AGE) is None
    )


def test_a_scout_dispatch_never_widens_the_window(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**侦察发不产生 `battle_reports`**（侦察报告走信箱里另一条路）。

    算进来就是一条永远不闭合的记录：下界会永远停在那一发上，每一趟都白翻到底。
    同一条排除在 `pending_reports_for_kind` / `count_dispatches_since` 也都有。
    """
    _dispatch(repository, run_id, at=NOW - timedelta(hours=5), mission=MISSION_KIND_SCOUT)

    assert (
        repository.oldest_open_attack_at(TARGET_KIND_PIRATE, now_utc=NOW, max_age=MAX_AGE) is None
    )


def test_a_dispatch_past_the_max_age_is_given_up_on(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """超过 `MAX_REPORT_AGE` 的那些早就被判「战报永远不会来」了。

    把下界钉在一发已经放弃的派遣上，只会让每一趟都翻到底——而那份战报即使还在，
    也早已经不在窗口里了。这个常量与 `_unmatched_dispatch_candidates` /
    `bot_dispatch_facts` 用的是同一个。
    """
    _dispatch(repository, run_id, at=NOW - MAX_AGE - timedelta(minutes=1))

    assert (
        repository.oldest_open_attack_at(TARGET_KIND_PIRATE, now_utc=NOW, max_age=MAX_AGE) is None
    )


def test_a_refused_dispatch_is_not_open(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """没被游戏接受的那一发根本没有舰队飞出去，也就永远不会有战报。"""
    _dispatch(repository, run_id, at=NOW - timedelta(hours=1), accepted=False)

    assert (
        repository.oldest_open_attack_at(TARGET_KIND_PIRATE, now_utc=NOW, max_age=MAX_AGE) is None
    )


def test_each_chain_asks_about_its_own_dispatches(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """两条链路各翻各的信箱窗口：bot 在等的那一发，不该让海盗那趟多翻几屏。"""
    _dispatch(repository, run_id, at=NOW - timedelta(hours=2), kind=TARGET_KIND_BOT)

    assert (
        repository.oldest_open_attack_at(TARGET_KIND_PIRATE, now_utc=NOW, max_age=MAX_AGE) is None
    )
    assert (
        repository.oldest_open_attack_at(TARGET_KIND_BOT, now_utc=NOW, max_age=MAX_AGE) is not None
    )
