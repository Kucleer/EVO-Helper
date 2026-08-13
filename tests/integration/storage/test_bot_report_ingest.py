"""攻击战报入库之后，目标必须从「等战报」走到它该去的那一态。

**这一条就是死锁的解除证明。** 原先的链路是这样闭合的：

- `phase_of` 要 `DispatchFact.has_report` 为真才放目标往下走；
- `has_report` 来自 `battle_reports` 里有没有一行指着那一发派遣；
- 而**全仓没有任何代码为 bot 写过那张表**；
- 唯一读战报的那段代码只挂在「该攻击了」那条分支上。

于是读战报的代码只在读过战报之后才会被执行。实机跑一整夜的结果是等待态出现
152 次、往下那一态出现 0 次，网页「情报中心」一行数据都没多。

这里刻意**走真的 `append_report`**（不用那个绕开匹配的测试后门）：认领派遣靠
「出发坐标 + 目标坐标 + 时间就近」，而入库路径正确与否，最终就体现在
`phase_of` 换不换态上。

⚠️ 战果也在这条路上：`outcome` 决定这个坐标要不要再挨一发（平局才重打）。
所以这里的桩件给了**两屏**——「损失单位」只有拖到底那一屏才读得到，
不给第二屏时 `outcome` 恒为 None，也就永远测不出平局那条路。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from evo_helper.application.report_ingest import to_battle_report
from evo_helper.domain.battle_outcome import OUTCOME_DRAW
from evo_helper.domain.bot_round import BOT_ATTACK_PRESET, BotPhase, phase_of
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    TARGET_KIND_BOT,
    AttackDispatch,
    AttackIntent,
)
from evo_helper.domain.report_wait import MAX_REPORT_AGE
from evo_helper.vision.live_reports import DETAIL_UI_VERSION, LiveReportReader
from evo_helper.vision.models import PageObservation

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 149, 17)
PRESET = BOT_ATTACK_PRESET

#: 战报头上写的时刻（`06/08/2026 11:45:03`，游戏内一律 UTC+0）。
REPORTED_AT = datetime(2026, 8, 6, 11, 45, 3, tzinfo=UTC)
ROUND_START = REPORTED_AT.replace(hour=0, minute=0, second=0)
#: 派出比战报早半小时——`append_report` 的时间容差是 12 小时，稳稳落在里面。
DISPATCHED_AT = REPORTED_AT - timedelta(minutes=30)
NOW = REPORTED_AT + timedelta(minutes=1)

#: 详情页那一屏的 OCR 文本。**参战两列与各回合刻意留空**——生产上这条链路
#: 根本没打开过回放页，桩件多给一点就等于测了一条不存在的路。
HEADER = "发件人: System                    06/08/2026 11:45:03\n主题: 攻击报告"
VERSUS = (
    "Kucleer                    bot_2_149_17\n"
    "奥格瑞玛                   bot_2_149_17's Planet\n"
    "[2:137:18]                 [2:149:17]"
)


class DetailScreens:
    """只有详情页那一屏能提供的取字面。"""

    def mail_rows(self) -> list[str]:
        return []

    def report_header(self) -> str:
        return HEADER

    def versus_block(self) -> str:
        return VERSUS

    def participating_columns(self) -> tuple[str, str]:
        return ("", "")

    def round_columns(self) -> list[tuple[int, str, str]]:
        return []

    def unit_totals(self) -> tuple[str, str]:
        return ("100", "5.36K")

    def loss_totals(self) -> tuple[str, str]:
        # 没拖过的那一屏读不到这一行（被面板下沿切掉）。空就是空。
        return ("", "")


class BottomScreens(DetailScreens):
    """拖到底之后那一屏：只有它给得出「损失单位」。

    双方都还剩船（100−40、5360−1200），所以战果算出来是平局——
    「平局就对同一坐标再打一发」那条口径的输入就是这两行。
    """

    def loss_totals(self) -> tuple[str, str]:
        return ("40", "1.2K")


def _attack_dispatch(repository, run_id, *, dispatched_at=DISPATCHED_AT, accepted=True):  # type: ignore[no-untyped-def]
    """一发已经飞出去的 BBB 攻击，战报还没收。"""
    intent_id, dispatch_id = uuid4(), uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=ORIGIN,
            target=TARGET,
            preset=FleetPresetRef(name=PRESET, signature=f"预设:{PRESET}"),
            cycle_start_utc=dispatched_at,
            created_at_utc=dispatched_at,
            target_kind=TARGET_KIND_BOT,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=dispatched_at,
            accepted=accepted,
            mission_kind=MISSION_KIND_ATTACK,
        )
    )
    return dispatch_id


def _read_detail_only():  # type: ignore[no-untyped-def]
    """把详情页那两屏读成 `LiveBattleReport`，和活链路走的是同一段代码。"""
    reader = LiveReportReader(DetailScreens())  # type: ignore[arg-type]
    return reader.read_detail_only(
        PageObservation(screen="mail_detail", ui_version=DETAIL_UI_VERSION, confidence=1.0),
        bottom=BottomScreens(),
    )


def _ingest(repository):  # type: ignore[no-untyped-def]
    live = _read_detail_only()
    report = to_battle_report(live, report_id=uuid4())
    repository.append_report(report)
    return report


def _phase(repository):  # type: ignore[no-untyped-def]
    return phase_of(repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW))


# -- 死锁的解除 --------------------------------------------------------------


def test_an_attack_without_its_report_waits(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """入库之前的状态：这就是实机跑了一整夜、152 次都停在的那一态。"""
    _attack_dispatch(repository, run_id)

    assert _phase(repository) is BotPhase.AWAITING_ATTACK_REPORT


def test_ingesting_the_report_unblocks_the_target_with_its_outcome(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**死锁解除**：入库之后目标不再停在等战报，而且带着战果往下走。

    这份桩件算出来是平局，所以下一步是「对同一坐标再打一发」。战果没被送到
    判态那一侧的话，这里会变成 `DONE`——看着也很正常，只是从此再没有补刀。
    """
    _attack_dispatch(repository, run_id)

    report = _ingest(repository)

    assert report.outcome == OUTCOME_DRAW
    assert _phase(repository) is BotPhase.NEEDS_ATTACK


def test_the_report_is_claimed_by_that_very_dispatch(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """换态的依据必须是「这一发的战报」，不是「这个坐标有过战报」。

    `phase_of` 只看 `has_report`，而那个字段是 `battle_reports.dispatch_id`
    连出来的。认领没成功却让态往前走，等于把别人的战报算到这一发头上。
    """
    dispatch_id = _attack_dispatch(repository, run_id)

    _ingest(repository)

    assert _linked_dispatch_ids(repository) == [dispatch_id]
    assert _match_statuses(repository) == ["MATCHED"]


def test_an_attack_report_writes_no_fleet_snapshots(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**只读详情页，就一行明细都不写。**

    情报中心按 `side='defender' and round_no is null` 取「这个 bot 有什么船」。
    这条链路没看过回放页，凑一份进去等于在页面上凭空造出一支舰队——
    海盗那条链路的测试守着同一句话（`.changes/28-pirate-report-outcome.md`）。
    """
    _attack_dispatch(repository, run_id)

    report = _ingest(repository)

    assert report.fleet == ()
    assert _fleet_rows(repository) == []


def test_the_four_totals_survive_into_the_row(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """算胜负的那四个数要真的落库。

    只存 `outcome` 不存四个输入的话，事后没人能复核那一条战果是怎么算出来的；
    而这条链路的重打决策全挂在它上面。
    """
    _attack_dispatch(repository, run_id)

    report = _ingest(repository)

    assert (report.attacker_units, report.attacker_losses) == (100, 40)
    assert (report.defender_units, report.defender_losses) == (5360, 1200)


# -- 重复入库 ----------------------------------------------------------------


def test_the_same_report_is_only_stored_once(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """信箱里那几行每趟都在。没有这道去重，一份读不上号的战报会每趟复制一行。"""
    _attack_dispatch(repository, run_id)
    _ingest(repository)

    assert repository.has_report_at(TARGET, REPORTED_AT) is True


def test_an_unseen_report_time_is_not_mistaken_for_a_stored_one(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """去重按**报告时间**，不是按坐标——按坐标就再也收不到第二份战报了。"""
    _attack_dispatch(repository, run_id)
    _ingest(repository)

    assert repository.has_report_at(TARGET, REPORTED_AT + timedelta(seconds=1)) is False


# -- 报告就是读不到时的出路 --------------------------------------------------


def test_an_attack_whose_report_never_arrives_is_eventually_given_up_on(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**这是 `phase_of` 那条前置条件的落实。**

    它的 docstring 写着「调用方必须先把已判定战报永远不会来的派遣剔除掉」，
    否则目标会**静默卡死**在等待态：不崩溃、不报错，画面上只是「在等」。
    收报告那一步会失败（OCR 读不出、报告根本没来、认不上号），所以这条出路
    必须存在——过了 `MAX_REPORT_AGE` 就把那一发剔掉，目标退回「该打了」。

    这也是重打配额（`MAX_ATTACKS_PER_TARGET`）在「读不到战报」那一侧的边界：
    剔掉之后那一发不再占配额，所以每个目标每 6 小时最多因此多打一发。
    """
    stale = NOW - MAX_REPORT_AGE - timedelta(minutes=1)
    _attack_dispatch(repository, run_id, dispatched_at=stale)

    assert repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=NOW) == []
    assert _phase(repository) is BotPhase.NEEDS_ATTACK


def test_a_dispatch_still_within_the_window_keeps_waiting(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """还等得到的不许剔。剔早了就是每一趟都重派一发，白烧航线和配额。"""
    fresh = NOW - MAX_REPORT_AGE + timedelta(minutes=1)
    _attack_dispatch(repository, run_id, dispatched_at=fresh)

    assert _phase(repository) is BotPhase.AWAITING_ATTACK_REPORT


def test_an_old_dispatch_that_did_get_its_report_is_not_dropped(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """放弃规则只对**没有战报**的那些生效。

    连已闭合的一起剔掉，这个目标的重打配额就会凭空退回去：一个本轮已经打过
    三发的 bot 在六小时后被从头再打一遍，而且从日志上看不出为什么。
    """
    stale = REPORTED_AT - timedelta(minutes=30)
    _attack_dispatch(repository, run_id, dispatched_at=stale)
    _ingest(repository)
    much_later = REPORTED_AT + MAX_REPORT_AGE + timedelta(hours=1)

    facts = repository.bot_dispatch_facts(TARGET, since=ROUND_START, now_utc=much_later)

    assert [fact.has_report for fact in facts] == [True]
    assert [fact.outcome for fact in facts] == [OUTCOME_DRAW]


# -- 直接看库 ----------------------------------------------------------------


def _reports(repository):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001 - 直接看列，不经查询
        return list(session.scalars(select(orm.BattleReportRow)))


def _linked_dispatch_ids(repository):  # type: ignore[no-untyped-def]
    return [row.dispatch_id for row in _reports(repository)]


def _match_statuses(repository):  # type: ignore[no-untyped-def]
    return [row.match_status for row in _reports(repository)]


def _fleet_rows(repository):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001
        return list(session.scalars(select(orm.FleetSnapshotRow)))
