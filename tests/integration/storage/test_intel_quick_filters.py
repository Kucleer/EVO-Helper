"""情报中心：海盗不进这张表、四个判定舰种、以及三个快速过滤（预设 / 结果 / 战果）。

这一份钉死三件在实机上出过错的事：

1. **海盗一行都不进这张表**（用户口径 2026-08-14）。海盗每 24 小时刷新一次，
   「这颗星球上有什么舰队」对它根本不成立；而情报中心是 bot 星球的**长期台账**，
   每一行都默认「它明天还是这样」。海盗仍然记在攻击日志里（那一页一行是一次派遣，
   是流水不是台账）。
   **收必须收在数据这一侧**：只在模板里跳过的话，「共 N 条」、页码、以及三个下拉框
   的候选值仍然按含海盗的集合算，总数就和看得见的行数对不上——所以下面既有「行没了」
   的用例，也有「计数与候选值跟着收了」的用例。
2. **`NULL` 不是 0。** 数量为 0 的格子在画面上只是一个孤零零的 `0`，实测最容易
   读空；读空当成 0 就是把「没看清」记成「这里是空的」，而整套
   ATTACK / SKIP / UNREADABLE 判定就建立在这个区分上。
3. **三个快速过滤按「最近一次派遣」判，不是「打过就算」。** 一个赢过也输过的目标
   要是同时落进「胜」和「负」，两个筛选谁都答不上「它现在什么情况」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.report_ingest import to_scout_report
from evo_helper.domain.intel_query import InvalidQueryError, parse_coordinate_span
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
    CoordinateScan,
)
from evo_helper.domain.report_wait import MAX_REPORT_AGE
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.intel import (
    DISPATCH_BLOCKED,
    DISPATCH_NEVER,
    DISPATCH_REJECTED,
    DISPATCH_SENT,
    RESULT_AWAITING,
    RESULT_FAIL,
    RESULT_NO_REPORT,
    RESULT_NONE,
    RESULT_VICTORY,
    IntelRow,
    IntelSearchQuery,
    SqlAlchemyIntelRepository,
)
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.scout_reports import PirateScoutReading
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
BASE_TIME = datetime(2026, 8, 11, 3, 0, 0, tzinfo=UTC)
SPAN = parse_coordinate_span("2:1", "2:999")

BOT = Coordinate(2, 320, 11)
PIRATE = Coordinate(2, 137, 4)
SCOUTED_ONLY = Coordinate(2, 137, 9)
#: 扫描认出来的 bot，身上又挂着一份侦察报告。实机上罕见，但它是「类型判错就丢一行」
#: 的那个口子：判成海盗就等于把一颗真的 bot 从台账里删掉。
SCOUTED_BOT = Coordinate(2, 330, 7)


@pytest.fixture
def factory(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(f"sqlite:///{tmp_path / 'quick.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def seed(factory):  # type: ignore[no-untyped-def]
    return _Seed(factory)


class _Seed:
    """写测试数据的工具。派遣链路要三张表配合，逐条手写会淹掉断言。"""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.repository = SqlAlchemyRepository(factory)
        self.intel = SqlAlchemyIntelRepository(factory)
        self.run_id = _make_run(factory)
        self._cycle = 0
        self._scanned: set[Coordinate] = set()

    def bot(self, coordinate: Coordinate, *, at: datetime = BASE_TIME) -> None:
        self._scanned.add(coordinate)
        self.repository.save_scan(
            CoordinateScan(
                run_id=self.run_id,
                coordinate=coordinate,
                scanned_at_utc=at,
                owner_name=f"bot_{coordinate.galaxy}_{coordinate.system}_{coordinate.position}",
                is_bot=True,
                confidence=1.0,
            )
        )

    def attempt(
        self,
        coordinate: Coordinate,
        *,
        preset: str,
        at: datetime,
        kind: str = TARGET_KIND_BOT,
        dispatched: bool = True,
        accepted: bool = True,
        mission_kind: str = MISSION_KIND_ATTACK,
        outcome: str | None = None,
    ) -> None:
        """一发派遣：意图 →（可选）派遣 →（可选）战报。

        打的是 bot 就顺手把它写进 `bot_targets`（一次坐标扫描）：情报中心的候选集
        来自那张表，而一发攻击不会往里写一行。实机上每个被打的 bot 都是先扫出来的，
        夹具照着实机来——不然这里造出来的会是一种现实中不存在的目标，用它测出来的
        「在不在列表里」也就说明不了什么。
        """
        if kind == TARGET_KIND_BOT and coordinate not in self._scanned:
            self.bot(coordinate, at=at)
        self._cycle += 1
        intent = AttackIntent(
            intent_id=uuid4(),
            run_id=self.run_id,
            origin=ORIGIN,
            target=coordinate,
            preset=FleetPresetRef(name=preset, signature=f"{preset}:1"),
            cycle_start_utc=BASE_TIME + timedelta(days=self._cycle),
            created_at_utc=at,
            target_kind=kind,
        )
        self.repository.save_attack_intent(intent)
        if not dispatched:
            return
        dispatch = AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=at,
            accepted=accepted,
            mission_kind=mission_kind,
        )
        self.repository.save_dispatch(dispatch)
        if outcome is None:
            return
        # 战报由 `append_report` 自己认领派遣（出发点 + 目标 + 时间窗）。
        # 这里不手工写 `dispatch_id`：那会绕开认领判据，于是这份用例就再也
        # 测不到「战果确实是顺着那一发接上来的」。
        self.repository.append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=at + timedelta(minutes=30),
                attacker_origin=ORIGIN,
                defender_target=coordinate,
                fleet=(),
                outcome=outcome,
            )
        )

    def scout(
        self,
        coordinate: Coordinate,
        *,
        counts: dict[str, int],
        missing: tuple[str, ...] = (),
        at: datetime = BASE_TIME,
    ) -> None:
        self.repository.append_scout_report(
            to_scout_report(
                PirateScoutReading(
                    raw_time_text="11/08/2026 03:00:00",
                    reported_at_utc=at,
                    origin=ORIGIN,
                    target=coordinate,
                    trigger_ships=counts,
                    missing=missing,
                ),
                report_id=uuid4(),
            )
        )

    def rows(self, **kwargs: object) -> dict[Coordinate, IntelRow]:
        page = self.intel.search(IntelSearchQuery(span=SPAN, **kwargs))  # type: ignore[arg-type]
        return {row.coordinate: row for row in page.rows}


def _make_run(factory: sessionmaker[Session]) -> UUID:
    """派遣意图有一条指向 run_instances 的外键，先得有一次运行。"""
    return seed_run_instance(
        factory,
        plan_name="quick-filters",
        idempotency_key="quick-filters-0001",
        created_at_utc=BASE_TIME,
    )


# -- 海盗不进这张表 -----------------------------------------------------------


class TestPiratesAreNotInTheLedger:
    """海盗每 24 小时刷新，「这颗星球上有什么舰队」对它不成立，所以不进台账。

    海盗进这张表的路一共两条——有侦察报告、有海盗派遣——两条都得堵上；
    而**堵不能只堵在模板里**，页面上的计数、分页、候选值都要跟着收。
    """

    def test_a_scouted_pirate_is_not_listed(self, seed: _Seed) -> None:
        """侦察报告是海盗进这张表的第一条路。"""
        seed.scout(SCOUTED_ONLY, counts={"深空吞噬者": 3}, missing=("噬能截击者",))

        assert SCOUTED_ONLY not in seed.rows()

    def test_a_pirate_that_was_only_ever_attacked_is_not_listed(self, seed: _Seed) -> None:
        """第二条路：一发都没侦察、直接打的海盗，派遣自己记着 `target_kind`。"""
        seed.attempt(PIRATE, preset="AAA", at=BASE_TIME, kind=TARGET_KIND_PIRATE)

        assert PIRATE not in seed.rows()

    def test_the_count_and_the_rows_shrink_together(self, seed: _Seed) -> None:
        """**这条钉的是「计数也跟着收了」。**

        只在渲染那一层跳过海盗的话，行看不见了，但「共 N 条」、页码、还有页脚的
        「正在看第 1–50 条」仍然按含海盗的集合算——于是第 2 页上一行都没有，
        而标题写着共 3 条。所以数的和显示的必须是同一批行。
        """
        seed.bot(BOT)
        seed.attempt(PIRATE, preset="AAA", at=BASE_TIME, kind=TARGET_KIND_PIRATE)
        seed.scout(SCOUTED_ONLY, counts={"深空吞噬者": 3})

        page = seed.intel.search(IntelSearchQuery(span=SPAN))

        assert [row.coordinate for row in page.rows] == [BOT]
        assert page.total == 1

    def test_a_bot_target_stays_a_bot(self, seed: _Seed) -> None:
        seed.bot(BOT)
        seed.attempt(BOT, preset="探路", at=BASE_TIME, kind=TARGET_KIND_BOT)

        assert seed.rows()[BOT].kind == TARGET_KIND_BOT

    def test_a_non_bot_scan_without_any_pirate_evidence_is_not_listed(self, seed: _Seed) -> None:
        """扫到的空位/真人不进这张表——这一页问的是「能打谁」。"""
        empty = Coordinate(2, 400, 3)
        seed.repository.save_scan(
            CoordinateScan(
                run_id=seed.run_id,
                coordinate=empty,
                scanned_at_utc=BASE_TIME,
                owner_name=None,
                is_bot=False,
                confidence=1.0,
            )
        )

        assert empty not in seed.rows()


# -- 扫出来的 bot + 一份侦察报告：四个判定舰种、0 与「没读到」 -----------------


class TestAScannedBotSurvivesAScoutReport:
    """一颗扫描认出来的 bot，身上又挂着一份侦察报告。

    从前这只是 chip 上错个色的问题；海盗被收掉之后，**类型判错就等于丢一行**——
    按「有侦察报告 = 海盗」去判，这颗 bot 会连同它的战报一起从台账里消失。
    所以 `bot_targets` 里那一行（坐标扫描的正面证据）必须压过那条推断。

    这也是侦察那条取数路现在唯一还走得到的地方，「`None` 不是 0」跟着一起钉在这里。
    """

    def test_a_scout_report_does_not_turn_a_scanned_bot_into_a_pirate(self, seed: _Seed) -> None:
        seed.bot(SCOUTED_BOT)
        seed.scout(SCOUTED_BOT, counts={"深空吞噬者": 3}, missing=("噬能截击者",))

        rows = seed.rows()

        assert SCOUTED_BOT in rows
        assert rows[SCOUTED_BOT].kind == TARGET_KIND_BOT

    def test_a_zero_stays_zero_and_an_unread_cell_stays_none(self, seed: _Seed) -> None:
        """**这条是整份文件的重点。**

        0 是「对方没有这种船，随便打」，`None` 是「这一格没看清」。两者在页面上
        长得可以一样（都是一小格），含义却相反；把 `None` 折成 0 就是把一支实打实
        的舰队记成空的。
        """
        seed.bot(SCOUTED_BOT)
        seed.scout(
            SCOUTED_BOT,
            counts={"深空吞噬者": 0, "钛能守卫者": 7},
            missing=("噬能截击者", "收割者"),
        )

        ships = seed.rows()[SCOUTED_BOT].scout_ships

        assert ships["深空吞噬者"] == 0
        assert ships["钛能守卫者"] == 7
        assert ships["噬能截击者"] is None
        assert ships["收割者"] is None
        # `0 or None` 与 `None or 0` 都会把上面两条揉成一样；分别断言，
        # 顺便钉死「这一格确实在字典里」——整个键缺席也是一种丢信息。
        assert set(ships) == {"深空吞噬者", "噬能截击者", "钛能守卫者", "收割者"}

    def test_only_the_newest_scout_report_counts(self, seed: _Seed) -> None:
        """对方会补船。拿旧报告当现状，下一发就是照着上周的情报打的。"""
        seed.bot(SCOUTED_BOT)
        seed.scout(SCOUTED_BOT, counts={"深空吞噬者": 3}, at=BASE_TIME)
        seed.scout(SCOUTED_BOT, counts={"深空吞噬者": 88}, at=BASE_TIME + timedelta(days=1))

        assert seed.rows()[SCOUTED_BOT].scout_ships["深空吞噬者"] == 88

    def test_scout_counts_do_not_leak_into_the_fleet_condition_input(self, seed: _Seed) -> None:
        """侦察报告**不是舰队快照**：它只有四个判定舰种，不是对方的全部家当。

        混进 `counts` 的话，「舰队总数 > 2000」这种条件会拿一份只读了四行的报告
        去算总数，把一个实际上厚得打不动的目标算成小猫两三只。
        """
        seed.bot(SCOUTED_BOT)
        seed.scout(SCOUTED_BOT, counts={"深空吞噬者": 3, "钛能守卫者": 4})

        row = seed.rows()[SCOUTED_BOT]

        assert row.counts == {}
        assert row.total is None
        assert row.has_fleet_data is False


# -- 三个快速过滤 -------------------------------------------------------------


class TestQuickFiltersUseTheLatestAttempt:
    def test_preset_filters_on_the_latest_attempt_not_on_ever_used(self, seed: _Seed) -> None:
        """先探路后 AAA 的目标，按「探路」筛**不该**筛到它。

        「打过就算」在这里最要命：探路是每个 bot 都走过的第一步，于是那个筛选
        等于「全选」，而用户点它是想找「还停在探路阶段的」。
        """
        seed.bot(BOT)
        seed.attempt(BOT, preset="探路", at=BASE_TIME, kind=TARGET_KIND_BOT)
        seed.attempt(BOT, preset="AAA", at=BASE_TIME + timedelta(hours=1), kind=TARGET_KIND_BOT)

        assert BOT not in seed.rows(preset="探路")
        assert BOT in seed.rows(preset="AAA")

    def test_battle_result_follows_the_latest_attempt(self, seed: _Seed) -> None:
        """赢过又输过的目标只属于「负」——它现在的样子是输。"""
        seed.bot(BOT)
        seed.attempt(BOT, preset="AAA", at=BASE_TIME, kind=TARGET_KIND_BOT, outcome=RESULT_VICTORY)
        seed.attempt(
            BOT,
            preset="AAA",
            at=BASE_TIME + timedelta(hours=2),
            kind=TARGET_KIND_BOT,
            outcome=RESULT_FAIL,
        )

        assert seed.rows()[BOT].battle_result == RESULT_FAIL
        assert BOT not in seed.rows(battle_result=RESULT_VICTORY)
        assert BOT in seed.rows(battle_result=RESULT_FAIL)

    def test_the_three_dispatch_outcomes_are_told_apart(self, seed: _Seed) -> None:
        """已派出 / 未派出（被闸门拦下）/ 被拒是三件事。

        合成一档的话，「为什么这个目标没打」在页面上就没有答案了。
        """
        sent, blocked, rejected = (
            Coordinate(2, 200, 1),
            Coordinate(2, 200, 2),
            Coordinate(2, 200, 3),
        )
        seed.attempt(sent, preset="AAA", at=BASE_TIME)
        seed.attempt(blocked, preset="AAA", at=BASE_TIME, dispatched=False)
        seed.attempt(rejected, preset="AAA", at=BASE_TIME, accepted=False)

        rows = seed.rows()

        assert rows[sent].dispatch_state == DISPATCH_SENT
        assert rows[blocked].dispatch_state == DISPATCH_BLOCKED
        assert rows[rejected].dispatch_state == DISPATCH_REJECTED
        assert set(seed.rows(dispatch_state=DISPATCH_BLOCKED)) == {blocked}
        assert set(seed.rows(dispatch_state=DISPATCH_REJECTED)) == {rejected}

    def test_a_target_that_was_never_dispatched_at_all(self, seed: _Seed) -> None:
        seed.bot(BOT)

        row = seed.rows()[BOT]

        assert row.dispatch_state == DISPATCH_NEVER
        assert row.battle_result == RESULT_NONE
        assert row.preset_name is None

    def test_a_dispatched_attack_without_a_report_is_awaiting(self, seed: _Seed) -> None:
        """还在 6 小时窗口里的那一发才叫「待战报」——它真的还等得到。

        ⚠️ **派出时刻必须跟着此刻走，不能再用固定的 `BASE_TIME`。** 这一档是拿
        `MAX_REPORT_AGE` 和现在比出来的（判据现算、不写标记，理由见
        `intel._battle_result`），钉死一个 2026-08-11 的时刻，测到的就是下面那条
        「等不到了」，而这条用例的名字会照旧说它测的是「还在等」。
        """
        seed.attempt(BOT, preset="AAA", at=datetime.now(UTC) - timedelta(minutes=5))

        assert seed.rows()[BOT].battle_result == RESULT_AWAITING

    def test_a_report_that_will_never_arrive_stops_looking_like_a_warning(
        self, seed: _Seed
    ) -> None:
        """派出去超过 6 小时还没战报的，判「无战报」而不是一直挂着「待战报」。

        实机成因：用户手动关掉 runner 并**撤回了舰队**，那一发（00:36:31 派出、
        目标 2:277:8）永远不会产生战报。用户口径（2026-08-17）：「对账允许有对不上
        的情况，比如我手动操作的，只是读已经对上的。」

        6 小时不是这里新立的界：`repository.pending_reports_for_kind` /
        `due_attack_dispatches` / `bot_dispatch_facts` 早就按同一个 `MAX_REPORT_AGE`
        把这类派遣判成「战报永远不会来」整条剔掉了。情报中心这一格是唯一没跟上的
        地方，于是同一发在调度那边早已结案、页面上却还亮着黄色的「待战报」。
        """
        seed.attempt(
            BOT, preset="AAA", at=datetime.now(UTC) - MAX_REPORT_AGE - timedelta(minutes=1)
        )

        row = seed.rows()[BOT]

        assert row.battle_result == RESULT_NO_REPORT
        # **账要留着**：这一行照旧在情报中心里、照旧记着它派出去过，
        # 变的只是它不再摆出「还等得到」的样子。
        assert row.dispatch_state == DISPATCH_SENT
        assert BOT not in seed.rows(battle_result=RESULT_AWAITING)

    def test_a_received_report_is_never_called_missing(self, seed: _Seed) -> None:
        """战报收到了、只是没读出胜负——那一档绝不能判成「无战报」。

        `outcome` 为 None 有两个完全不同的成因，而其中一个手里正握着一份真躺在
        库里的战报。只按时间判就会拿它说这份战报不存在，用户于是去补录一份
        永远补不出新东西的东西。判据里那个 `report_received` 就是这条用例。
        """
        dispatched_at = datetime.now(UTC) - MAX_REPORT_AGE - timedelta(minutes=1)
        seed.attempt(BOT, preset="AAA", at=dispatched_at)
        # 直接落一份 **`outcome` 为 NULL** 的战报：`_Seed.attempt` 的 `outcome=None`
        # 那一档表示「压根不写战报」，而这里要的恰恰是「战报在、胜负没读出来」。
        seed.repository.append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=dispatched_at + timedelta(minutes=30),
                attacker_origin=ORIGIN,
                defender_target=BOT,
                fleet=(),
            )
        )

        assert seed.rows()[BOT].battle_result != RESULT_NO_REPORT

    def test_a_scout_leg_is_never_left_waiting_for_a_battle_report(self, seed: _Seed) -> None:
        """侦察发不产生战报，把它算成「待战报」会让页面上永远挂着等不到的行。

        PR #95 是在认领那一侧踩的同一个坑：自己那一发侦察被当成了战报候选。

        判据是 `mission_kind`，不是 `target_kind`：海盗离场之后这一档在页面上暂时
        碰不到（侦察只对海盗发），但那条分支还在 `_battle_result` 里——哪天 bot 链路
        也加一发侦察，这条就是它的护栏。
        """
        seed.attempt(BOT, preset="侦察", at=BASE_TIME, mission_kind=MISSION_KIND_SCOUT)

        row = seed.rows()[BOT]

        assert row.dispatch_state == DISPATCH_SENT
        assert row.battle_result == RESULT_NONE
        assert BOT not in seed.rows(battle_result=RESULT_AWAITING)

    def test_a_rejected_dispatch_is_not_awaiting_either(self, seed: _Seed) -> None:
        """没飞出去就不会有战报。挂成「待战报」是在等一个永远不来的东西。"""
        seed.attempt(BOT, preset="AAA", at=BASE_TIME, accepted=False)

        assert seed.rows()[BOT].battle_result == RESULT_NONE

    def test_filters_compose(self, seed: _Seed) -> None:
        """三个一起用是 AND。"""
        hit = Coordinate(2, 210, 1)
        other_preset = Coordinate(2, 210, 2)
        other_result = Coordinate(2, 210, 3)
        seed.attempt(hit, preset="AAA", at=BASE_TIME, outcome=RESULT_VICTORY)
        seed.attempt(other_preset, preset="BBB", at=BASE_TIME, outcome=RESULT_VICTORY)
        seed.attempt(other_result, preset="AAA", at=BASE_TIME, outcome=RESULT_FAIL)

        matched = seed.rows(
            preset="AAA", dispatch_state=DISPATCH_SENT, battle_result=RESULT_VICTORY
        )

        assert set(matched) == {hit}

    def test_no_filter_means_no_filter(self, seed: _Seed) -> None:
        """None 是「不筛」，不是「筛一个叫 None 的预设」。"""
        attacked = Coordinate(2, 211, 2)
        seed.attempt(attacked, preset="AAA", at=BASE_TIME)
        seed.bot(BOT)

        assert set(seed.rows()) == {attacked, BOT}

    def test_the_total_count_is_the_filtered_count(self, seed: _Seed) -> None:
        """分页的总数要算筛完之后的，否则页码指向的位置根本没有行。"""
        seed.attempt(Coordinate(2, 211, 2), preset="AAA", at=BASE_TIME)
        seed.attempt(Coordinate(2, 211, 1), preset="BBB", at=BASE_TIME)

        page = seed.intel.search(IntelSearchQuery(span=SPAN, preset="AAA"))

        assert page.total == 1


class TestUnknownFilterValues:
    def test_an_unknown_dispatch_state_is_rejected(self) -> None:
        """打错的档位要当场说出来，而不是安静地筛出 0 条。"""
        with pytest.raises(InvalidQueryError):
            IntelSearchQuery(dispatch_state="ALMOST")

    def test_an_unknown_battle_result_is_rejected(self) -> None:
        with pytest.raises(InvalidQueryError):
            IntelSearchQuery(battle_result="WON")


class TestPresetNames:
    def test_it_lists_what_was_actually_dispatched(self, seed: _Seed) -> None:
        """下拉框的选项来自库，不写死：预设是用户在游戏里配的。"""
        seed.attempt(Coordinate(2, 212, 1), preset="BBB", at=BASE_TIME)
        seed.attempt(Coordinate(2, 212, 2), preset="AAA", at=BASE_TIME)

        assert seed.intel.preset_names() == ["AAA", "BBB"]

    def test_a_preset_only_ever_used_on_pirates_is_not_offered(self, seed: _Seed) -> None:
        """**这条钉的是「候选值也跟着收了」。**

        海盗不在这张表里了（见文件头），而「侦察」这个预设只对海盗用过——留在下拉框
        里就是留一个**选下去必然是空页**的选项。那比没有这个选项更糟：用户会以为侦察
        记录被弄丢了，而它们一直在攻击日志里。
        """
        seed.attempt(PIRATE, preset="侦察", at=BASE_TIME, kind=TARGET_KIND_PIRATE)
        seed.attempt(BOT, preset="探路", at=BASE_TIME)

        assert seed.intel.preset_names() == ["探路"]
        # 候选值与行说的是同一批数据：选项没了，那个筛选原本会命中的行也没了。
        assert seed.rows(preset="侦察") == {}


class TestSortingUsesTheLatestIntelTime:
    def test_a_fresh_scout_report_outranks_an_old_battle_report(self, seed: _Seed) -> None:
        """按「最新情报时间 ↓」排时，只有侦察报告的那一行不该沉底。

        排序键是 `intel_at`（战报时间与侦察报告时间取晚的那个），不是 `snapshot_at`：
        列名写的是「最新情报时间」，而一行的最新情报可以只是一份侦察报告。
        """
        seed.bot(BOT)
        seed.repository.append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=BASE_TIME,
                attacker_origin=ORIGIN,
                defender_target=BOT,
                fleet=(),
                defender_units=319,
            )
        )
        seed.bot(SCOUTED_BOT)
        seed.scout(SCOUTED_BOT, counts={"深空吞噬者": 1}, at=BASE_TIME + timedelta(days=2))

        page = seed.intel.search(IntelSearchQuery(span=SPAN, sort="snapshot_desc"))

        assert [row.coordinate for row in page.rows] == [SCOUTED_BOT, BOT]


class TestSpanStillBounds:
    def test_a_target_outside_the_span_is_excluded(self, seed: _Seed) -> None:
        outside = Coordinate(3, 137, 4)
        seed.bot(BOT)
        seed.bot(outside)

        assert set(seed.rows()) == {BOT}

    def test_the_span_is_compared_on_the_packed_coordinate(self, seed: _Seed) -> None:
        """**判据在位号上**：区间 `2:130:15` – `2:140:3` 里有 2:135:9。

        位号写全的区间里，起点位号比终点位号大是常态（130 系从 15 位开始，
        140 系到 3 位为止）。逐分量比较会拿 `position BETWEEN 15 AND 3` 去卡，
        整段中间星系的行一条都留不下——而它们正是这个区间的主体。

        写成 `2:130` – `2:140` 那种简写试不出这件事：简写把位号补成 1..999，
        逐分量比较照样放行，两种实现看起来一模一样。
        """
        inside = Coordinate(2, 135, 9)
        low_edge = Coordinate(2, 130, 16)
        outside_low = Coordinate(2, 130, 14)
        outside_high = Coordinate(2, 140, 4)
        for coordinate in (inside, low_edge, outside_low, outside_high):
            seed.bot(coordinate)

        page = seed.intel.search(
            IntelSearchQuery(span=parse_coordinate_span("2:130:15", "2:140:3"))
        )

        assert [row.coordinate for row in page.rows] == [low_edge, inside]
