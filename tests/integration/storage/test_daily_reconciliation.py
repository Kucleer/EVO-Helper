"""开工对账落库，以及它怎么进配额计数。

配额（海盗每天 32 次）现在只按库里的 `attack_dispatches` 数。库外发生过的事它
一概不知道：用户手动打的、上一次进程崩在写库之前的、换过库之后游戏里仍然算数
的那些。数少了就会超额，而超额的后果是游戏发邮件通知并把攻击强制返回。

两侧都只是下界，证据互相独立——库知道「刚派出、战报还没到」的那几发，信箱知道
「库外发生过」的那几发——所以按 UTC 日**取大**。取大是能被证据支持的最紧的下界，
只会让助手提前收手；取小或相加都会错。

这一整个文件守的就是这条规则，外加一条绝不能破的：
**对账永远不往 `attack_dispatches` 里补行。**
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)
from evo_helper.storage import models as orm

NOON = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)


def _reconciliation_rows(repository) -> int:  # type: ignore[no-untyped-def]
    with repository._session_factory() as session:  # noqa: SLF001 - 直接数行，绕开被测方法
        return int(
            session.scalar(select(func.count()).select_from(orm.DailyReconciliationRow)) or 0
        )


def _reconciliation_complete(repository) -> bool:  # type: ignore[no-untyped-def]
    with repository._session_factory() as session:  # noqa: SLF001 - 直接读列，绕开被测方法
        return bool(session.scalar(select(orm.DailyReconciliationRow.complete)))


def _dispatch(repository, run_id, *, at, kind=TARGET_KIND_PIRATE, target=None) -> None:  # type: ignore[no-untyped-def]
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=target or Coordinate(2, 137, 1),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=at,
            created_at_utc=at,
            target_kind=kind,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(), intent_id=intent_id, dispatched_at_utc=at, accepted=True
        )
    )


def test_the_mailbox_count_wins_when_it_is_larger(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """库里两发、信箱里数到九份，配额按九算。

    那七发的差额正是对账存在的理由：手工打的、崩在写库之前的、换库之前的。
    只按库算就会以为还剩三十次，接着一路打到游戏发超限邮件。
    """
    _dispatch(repository, run_id, at=NOON)
    _dispatch(repository, run_id, at=NOON + timedelta(minutes=1), target=Coordinate(2, 137, 2))
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=9,
        complete=True,
        reconciled_at_utc=NOON,
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=MIDNIGHT) == 9


def test_the_database_count_wins_when_it_is_larger(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """刚派出去的那几发**只有库知道**——战报还要几分钟才产生。

    取小或者直接用信箱那个数，就会把刚打出去的几发当成没打过，接着重复打。
    """
    for index in range(4):
        _dispatch(
            repository,
            run_id,
            at=NOON + timedelta(minutes=index),
            target=Coordinate(2, 137, index + 1),
        )
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=1,
        complete=True,
        reconciled_at_utc=NOON,
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=MIDNIGHT) == 4


def test_the_two_sides_are_never_added_up(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**取大，不是相加。** 相加会把同一发数两遍：一发打出去、战报回来了，
    库里有一条、信箱里也有一份，那是同一发。相加就是把当天配额凭空砍半。
    """
    _dispatch(repository, run_id, at=NOON)
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=1,
        complete=True,
        reconciled_at_utc=NOON,
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=MIDNIGHT) == 1


def test_a_partial_count_still_counts(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """没翻到底的那次对账**照样算数**——它仍然是一个真实的下界，而且更紧。

    「至少 9 份」比「库里有 5 条」更接近真相。把它当成不可用而扔掉，等于回到
    只按库算，也就是回到会超额的那一侧。`complete` 只作诊断，不作过滤条件。

    方向一律往「打得更少」倒：这个数偏大只会让助手提前收手，偏小才会白飞舰队。
    """
    for index in range(5):
        _dispatch(
            repository,
            run_id,
            at=NOON + timedelta(minutes=index),
            target=Coordinate(2, 137, index + 1),
        )
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=9,
        complete=False,
        reconciled_at_utc=NOON,
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=MIDNIGHT) == 9


def test_yesterdays_reconciliation_stays_out_of_todays_quota(repository) -> None:  # type: ignore[no-untyped-def]
    """配额的日界是 UTC 00:00。昨天数到的份数跟今天的额度无关。"""
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT - timedelta(days=1),
        observed_reports=30,
        complete=True,
        reconciled_at_utc=NOON - timedelta(days=1),
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=MIDNIGHT) == 0


def test_each_chain_has_its_own_reconciliation(repository) -> None:  # type: ignore[no-untyped-def]
    """海盗与 bot 的战报主题不同，配额也各算各的；混在一起就会互相污染。"""
    repository.record_daily_reconciliation(
        TARGET_KIND_BOT,
        day_utc=MIDNIGHT,
        observed_reports=7,
        complete=True,
        reconciled_at_utc=NOON,
    )

    assert repository.count_dispatches_since(TARGET_KIND_BOT, since=MIDNIGHT) == 7
    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=MIDNIGHT) == 0


def test_reconciling_never_invents_a_dispatch_row(  # type: ignore[no-untyped-def]
    repository, session_factory: sessionmaker[Session]
) -> None:
    """⚠️ **这条不能破。**

    `attack_dispatches` 里的每一行都意味着「一支舰队正在外面」。凭空多一条，
    调度器就会以为一条航线被占着、并等一份永远不会来的战报，要到
    `MAX_REPORT_AGE`（6 小时）才被判缺失清掉。对账只更新计数所依赖的那个事实。
    """
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=12,
        complete=True,
        reconciled_at_utc=NOON,
    )

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(orm.AttackDispatchRow)) == 0
        assert session.scalar(select(func.count()).select_from(orm.AttackIntentRow)) == 0


def test_a_day_keeps_one_row_and_a_later_bigger_count_wins(repository) -> None:  # type: ignore[no-untyped-def]
    """一天一行，键是 **UTC 日**；同一天再对一次就更新那一行，不堆行。

    对账现在**每次开工都跑**（用户会暂停任务再重启，「今日 X/32」必须接得上），
    所以同一个 UTC 日会写好几次。
    """
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=3,
        complete=True,
        reconciled_at_utc=NOON,
    )
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=8,
        complete=True,
        reconciled_at_utc=NOON + timedelta(hours=1),
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=MIDNIGHT) == 8
    assert _reconciliation_rows(repository) == 1


def test_a_later_smaller_count_never_lowers_the_day(repository) -> None:  # type: ignore[no-untyped-def]
    """**本文件里与「取大」并列的另一条：同一个 UTC 日里这个数只增不减。**

    每趟能翻到多远并不一样：翻到底的那趟数到 20，下一趟面板夹住只数到 6。
    照覆盖写，第二趟就把配额判据从 20 松回 6，助手于是以为还剩 26 发可打——
    **计数偏小正是会超额的那一侧**，代价是游戏把攻击强制返回、白飞一趟舰队。
    而战报只会变多，所以「今天至少有几份」本来就只该往上走。
    """
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=20,
        complete=True,
        reconciled_at_utc=NOON,
    )
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=6,
        complete=False,
        reconciled_at_utc=NOON + timedelta(hours=1),
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=MIDNIGHT) == 20
    # `complete` 跟着胜出的那个数走：它说的是「那个数是不是全天」。
    assert _reconciliation_complete(repository) is True


def test_the_next_day_starts_from_zero_again(repository) -> None:  # type: ignore[no-untyped-def]
    """只增不减是**一天之内**的规则。日界一到，新的一天从头数。"""
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=20,
        complete=True,
        reconciled_at_utc=NOON,
    )
    tomorrow = MIDNIGHT + timedelta(days=1)
    repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=tomorrow,
        observed_reports=1,
        complete=True,
        reconciled_at_utc=tomorrow,
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=tomorrow) == 1
    assert _reconciliation_rows(repository) == 2
