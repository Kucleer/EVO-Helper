"""海盗链路的当日去重：判据早就写好了，缺的是**接线**。

事故账（2026-08-13 通宵，UTC 15:00–19:00 / 本地 23:00–03:00）：

    海盗侦察 111 发，打在 54 个不同坐标上（2:137:1~4 各 5 发）
    海盗攻击 只有 12 发

配额全烧在重复侦察上。`domain.pirate_round.phase_for` 的七态、
`storage.repository.pirate_progress` 的按目标聚合，2026-08-11 就都在库里了——
但那个方法的注释写着「供控制台显示」，而 `tools.pirate_loop._find_pirates` /
`_decide_and_attack` **一次都没问过它**。于是每一轮都当作今天什么都没做过：
认出是海盗就发一发侦察。

用户口径（2026-08-13）：海盗刷新是当日内（游戏内 UTC+0），所以

- 今天已经**攻击**过这个坐标 → 不侦查、不攻击
- 今天已经**侦查**过这个坐标 → 不重复侦查，直接用今天那份报告的结论
- 唯一例外 `SCOUT_UNREADABLE`（报告回来了但四格没读全）→ 当天可以再补一次

这个文件用**真库**（每条测试自己一个临时文件）走完整条路：写库 → 仓储聚合 →
`domain.pirate_round.action_for` → 链路派不派。只桩掉会动鼠标的那几个方法。
桩掉仓储的话，钉住的就只是「链路照着我给的假答案做事」，而这次的毛病恰恰
不在链路的分支里，而在**它根本没去问库**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    ScoutReport,
    ScoutTriggerShip,
)
from evo_helper.domain.scheduler import quota_day_start_utc
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools.pirate_loop import LoopOptions, Outcome, PirateLoop, TargetCheck

ORIGIN = Coordinate(2, 137, 18)
SYSTEM = (2, 137)

#: 日界走的就是活链路那一份，不另写 `replace(hour=0)`——那正是这条链路
#: 不许自己写的东西（理由整段在 `domain.scheduler.quota_day_start_utc`）。
DAY_START = quota_day_start_utc(datetime.now(UTC))
TODAY = DAY_START + timedelta(minutes=1)
YESTERDAY = DAY_START - timedelta(minutes=1)

#: 四格都读到、都有实打实的舰队 → 判定「打」。
HAS_FLEET = {"深空吞噬者": 8, "噬能截击者": 4, "钛能守卫者": 1, "收割者": 0}
#: 四格都读到、都 ≤ 1 → 判定「不值得打」。
ALL_ZERO = {"深空吞噬者": 0, "噬能截击者": 0, "钛能守卫者": 0, "收割者": 0}
#: `收割者` 那格 NULL → 判定「没看清」。实机上这一格一份都没读出来过。
ONE_BLIND = {"深空吞噬者": 1, "噬能截击者": 0, "钛能守卫者": 1, "收割者": None}


class _Loop:
    """一个只剩「查库 → 决定 → 记一笔」的 `PirateLoop`。

    动鼠标的三处（导航、侦察、攻击）换成往 `events` 里记一行；库是真的。
    """

    def __init__(self, tmp_path: Path, *, pirates_at: set[int], scout: bool, attack: bool) -> None:
        engine = create_database_engine(f"sqlite:///{tmp_path / 'dedup.db'}")
        Base.metadata.create_all(engine)
        self.factory = create_session_factory(engine)
        self.repository = SqlAlchemyRepository(self.factory)
        # 意图的 `run_id` 是指向 `run_instances` 的外键，而本项目的 SQLite
        # 外键约束是开着的——随手编一个 UUID 会当场撞 IntegrityError。
        with self.factory() as session:
            plan = orm.ScanPlan(name="dedup-fixture", created_at_utc=TODAY)
            session.add(plan)
            session.flush()
            run = orm.RunInstance(
                plan_id=plan.id,
                idempotency_key="dedup-fixture-001",
                state="SCANNING",
                created_at_utc=TODAY,
            )
            session.add(run)
            session.commit()
            self.run_id = run.id
        self.events: list[str] = []
        self._seq = 0

        loop = PirateLoop.__new__(PirateLoop)
        loop._options = LoopOptions(systems=(SYSTEM,), scout=scout, attack=attack)
        loop._outcome = Outcome()
        loop._repository = self.repository
        loop._run_id = self.run_id
        loop._daily = None
        loop._navigator = _Navigator(self.events)

        def _check(coordinate: Coordinate) -> TargetCheck:
            hit = coordinate.position in pirates_at
            return TargetCheck.CONFIRMED if hit else TargetCheck.ABSENT

        def _scout(coordinate: Coordinate) -> bool:
            self.events.append(f"scout {coordinate.position}")
            return True

        def _attack(coordinate: Coordinate, *, preset: str | None = None) -> bool:
            del preset
            self.events.append(f"attack {coordinate.position}")
            return True

        loop.check_target = _check  # type: ignore[assignment, method-assign]
        loop.scout = _scout  # type: ignore[assignment, method-assign]
        loop.attack = _attack  # type: ignore[assignment, method-assign]
        loop._wait_for_reports = lambda count: None  # type: ignore[assignment, method-assign]
        self.loop = loop

    # -- 往库里摆事实 ------------------------------------------------------

    def scout_dispatch(self, position: int, *, at: datetime = TODAY, accepted: bool = True) -> None:
        self._dispatch(position, at=at, mission_kind=MISSION_KIND_SCOUT, accepted=accepted)

    def attack_dispatch(self, position: int, *, at: datetime = TODAY, report: bool = False) -> None:
        self._dispatch(position, at=at, mission_kind=MISSION_KIND_ATTACK, has_report=report)

    def scout_report(self, position: int, counts: dict[str, int | None], *, at: datetime) -> None:
        self.repository.append_scout_report(
            ScoutReport(
                report_id=uuid4(),
                reported_at_utc=at,
                raw_time_text=at.strftime("%d/%m/%Y %H:%M:%S"),
                origin=ORIGIN,
                target=Coordinate(*SYSTEM, position),
                trigger_ships=tuple(
                    ScoutTriggerShip(ship_type=name, count=count) for name, count in counts.items()
                ),
            )
        )

    def _dispatch(
        self,
        position: int,
        *,
        at: datetime,
        mission_kind: str,
        accepted: bool = True,
        has_report: bool = False,
    ) -> None:
        target = Coordinate(*SYSTEM, position)
        intent_id, dispatch_id = uuid4(), uuid4()
        # `save_attack_intent` 按 (run, 目标, cycle_start) 去重，所以每一发要有
        # 自己的 cycle_start；窗口筛的是 `created_at_utc`，那个才是要摆的事实。
        self._seq += 1
        self.repository.save_attack_intent(
            AttackIntent(
                intent_id=intent_id,
                run_id=self.run_id,
                origin=ORIGIN,
                target=target,
                preset=FleetPresetRef(name="AAA", signature="sig"),
                cycle_start_utc=at + timedelta(seconds=self._seq),
                created_at_utc=at,
                target_kind=TARGET_KIND_PIRATE,
            )
        )
        self.repository.save_dispatch(
            AttackDispatch(
                dispatch_id=dispatch_id,
                intent_id=intent_id,
                dispatched_at_utc=at,
                accepted=accepted,
                mission_kind=mission_kind,
            )
        )
        if has_report:
            with self.factory() as session:
                session.add(
                    orm.BattleReportRow(
                        id=uuid4(),
                        dispatch_id=dispatch_id,
                        reported_at_utc=at,
                        attacker_origin_galaxy=ORIGIN.galaxy,
                        attacker_origin_system=ORIGIN.system,
                        attacker_origin_position=ORIGIN.position,
                        defender_target_galaxy=target.galaxy,
                        defender_target_system=target.system,
                        defender_target_position=target.position,
                    )
                )
                session.commit()


class _Navigator:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def goto(self, coordinate: Coordinate) -> None:
        self._events.append(f"goto {coordinate.position}")


def _fixture(tmp_path: Path, **kwargs: Any) -> _Loop:
    kwargs.setdefault("pirates_at", {1})
    kwargs.setdefault("scout", True)
    kwargs.setdefault("attack", True)
    return _Loop(tmp_path, **kwargs)


def _walk(*after_first: str) -> list[str]:
    """`_find_pirates` 走完 1–4 位的完整事件序列，海盗只摆在第 1 位。

    2–4 位的 `goto` 一个都不能少：这几条钉的是「侦察派不派」，把导航一起
    改掉的话，「整轮根本没走完」也会跟着变绿。
    """
    return ["goto 1", *after_first, "goto 2", "goto 3", "goto 4"]


# -- 侦察那一侧 --------------------------------------------------------------


def test_a_target_untouched_today_is_still_scouted(tmp_path: Path) -> None:
    """去重不许把正常的第一发也挡掉。没有这条，「一发都不派」也算通过。"""
    fixture = _fixture(tmp_path)

    pirates, scouted = fixture.loop._find_pirates(*SYSTEM)

    assert fixture.events == _walk("scout 1")
    assert (scouted, [c.position for c in pirates]) == (1, [1])


def test_a_target_scouted_today_is_not_scouted_again(tmp_path: Path) -> None:
    """**这条就是 111 发那笔账。** 今天派过侦察 = 不再派第二发。"""
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)

    _pirates, scouted = fixture.loop._find_pirates(*SYSTEM)

    assert fixture.events == _walk()
    assert scouted == 0


def test_a_target_attacked_today_is_neither_scouted_nor_attacked(tmp_path: Path) -> None:
    """今天已经攻击过 → 不侦查、不攻击，连信箱都不用为它翻。"""
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)
    fixture.scout_report(1, HAS_FLEET, at=TODAY)
    fixture.attack_dispatch(1, report=True)

    pirates, scouted = fixture.loop._find_pirates(*SYSTEM)

    assert fixture.events == _walk()
    assert (scouted, pirates) == (0, [])
    # 认出来这件事仍旧要记——它是「这一位上有海盗」的事实，与今天做过什么无关。
    assert [c.position for c in fixture.loop._outcome.pirates] == [1]


def test_a_fully_read_empty_pirate_is_dropped_for_the_day(tmp_path: Path) -> None:
    """四格都读全、都 ≤ 1：结论确定，当天不再碰它。"""
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)
    fixture.scout_report(1, ALL_ZERO, at=TODAY)

    pirates, scouted = fixture.loop._find_pirates(*SYSTEM)

    assert fixture.events == _walk()
    assert (scouted, pirates) == (0, [])


def test_yesterdays_scout_does_not_count_as_todays(tmp_path: Path) -> None:
    """日界是**游戏内那一天**（UTC+0）。昨天侦察过，今天照样要侦察一发。

    日界走 `domain.scheduler.quota_day_start_utc`；写成本地 `replace(hour=0)`
    的话，本地 0–8 点整整八个钟头会把昨天的派遣算成今天的，那几个钟头正是
    这条链路整夜在跑的时段。
    """
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1, at=YESTERDAY)
    fixture.scout_report(1, HAS_FLEET, at=YESTERDAY)

    _pirates, scouted = fixture.loop._find_pirates(*SYSTEM)

    assert fixture.events == _walk("scout 1")
    assert scouted == 1


def test_a_refused_scout_leaves_the_target_open_for_another(tmp_path: Path) -> None:
    """被游戏拒掉的那一发没有探测器飞出去，不该把这个坐标锁到明天。"""
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1, accepted=False)

    _pirates, scouted = fixture.loop._find_pirates(*SYSTEM)

    assert fixture.events == _walk("scout 1")
    assert scouted == 1


# -- `SCOUT_UNREADABLE` 那一档：补一次为限 -----------------------------------


def test_an_unreadable_report_earns_one_make_up_scout(tmp_path: Path) -> None:
    """报告回来了但四格没读全，算不出该不该打 → 当天可以再补一次。"""
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)
    fixture.scout_report(1, ONE_BLIND, at=TODAY)

    _pirates, scouted = fixture.loop._find_pirates(*SYSTEM)

    assert fixture.events == _walk("scout 1")
    assert scouted == 1


def test_the_make_up_scout_happens_exactly_once(tmp_path: Path) -> None:
    """**这条是「补一次」那个「一」。**

    今天已经派过两发侦察、最新那份仍旧没看清 → 收手。没有这条上界，
    `UNREADABLE` 就是一条无限重侦的路：实机上 `收割者` 那一格
    **一份报告都没读出来过**，所以这一档不是边角情形，而是常态。
    """
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)
    fixture.scout_dispatch(1, at=TODAY + timedelta(minutes=5))
    fixture.scout_report(1, ONE_BLIND, at=TODAY + timedelta(minutes=6))

    pirates, scouted = fixture.loop._find_pirates(*SYSTEM)

    assert fixture.events == _walk()
    assert (scouted, pirates) == (0, [])


# -- 攻击那一侧 --------------------------------------------------------------


def test_a_verdict_of_attack_attacks_without_a_fresh_reading(tmp_path: Path) -> None:
    """**「待触发攻击 → 直接攻击，不重新侦察」。**

    `reading` 传 `None`（这一趟信箱里没翻到这份报告），照样要打：今天那份报告
    已经在库里判为「打」，重侦一次只是把配额烧掉再得出同一个结论。
    原先这里会报「读不到侦察报告；跳过」，于是明天再侦察一次、后天再一次。
    """
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)
    fixture.scout_report(1, HAS_FLEET, at=TODAY)

    fixture.loop._decide_and_attack(Coordinate(*SYSTEM, 1), None)

    assert fixture.events == ["goto 1", "attack 1"]


def test_a_target_attacked_today_is_not_attacked_again(tmp_path: Path) -> None:
    """今天已经打过 → 不再打，哪怕这一趟又翻到了一份判「打」的报告。"""
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)
    fixture.scout_report(1, HAS_FLEET, at=TODAY)
    fixture.attack_dispatch(1, report=True)

    fixture.loop._decide_and_attack(Coordinate(*SYSTEM, 1), _Reading("ATTACK"))

    assert fixture.events == []


def test_an_attack_still_in_flight_is_not_doubled_up(tmp_path: Path) -> None:
    """战报还没回来的那一发也占着这个坐标——重复派就是双倍烧配额。"""
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)
    fixture.scout_report(1, HAS_FLEET, at=TODAY)
    fixture.attack_dispatch(1)

    fixture.loop._decide_and_attack(Coordinate(*SYSTEM, 1), _Reading("ATTACK"))

    assert fixture.events == []


def test_yesterdays_verdict_never_launches_todays_attack(tmp_path: Path) -> None:
    """**昨天那份报告不许决定今天打不打。**

    海盗每天刷新，昨天读到的舰队量说的是昨天那批。今天的侦察发刚出去、报告
    还没回的那十几分钟，若拿昨天那份当判定，链路会照着过期情报把舰队扔出去。
    2:137:1~4 天天侦察，库里永远躺着昨天那份，所以这不是假想的情形。
    """
    fixture = _fixture(tmp_path)
    fixture.scout_report(1, HAS_FLEET, at=YESTERDAY)
    fixture.scout_dispatch(1)

    fixture.loop._decide_and_attack(Coordinate(*SYSTEM, 1), None)

    assert fixture.events == []


def test_a_live_reading_still_drives_the_attack_when_nothing_was_dispatched_today(
    tmp_path: Path,
) -> None:
    """`--attack` 不给 `--scout` 那一档：今天库里一发都没有，判定只能来自
    刚翻到的那份报告。既有行为一个字没变。
    """
    fixture = _fixture(tmp_path, scout=False)

    fixture.loop._decide_and_attack(Coordinate(*SYSTEM, 1), _Reading("ATTACK"))

    assert fixture.events == ["goto 1", "attack 1"]


def test_a_live_reading_of_skip_still_does_not_attack(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, scout=False)

    fixture.loop._decide_and_attack(Coordinate(*SYSTEM, 1), _Reading("SKIP"))

    assert fixture.events == []


def test_a_scout_still_in_flight_is_not_attacked_on_a_stray_reading(tmp_path: Path) -> None:
    """今天的侦察发还在路上时，一份来路不明的 `reading` 不许把攻击带出去。

    正常路径上这一份读到就落库、落库之后进度会重取（见本文件最后一节），
    所以走到这里说明它**不在今天的库里**——最常见的原因是它是昨天那份。
    """
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)

    fixture.loop._decide_and_attack(Coordinate(*SYSTEM, 1), _Reading("ATTACK"))

    assert fixture.events == []


# -- 一整趟 ------------------------------------------------------------------


def test_a_report_collected_this_round_is_acted_on_this_round(tmp_path: Path) -> None:
    """**读完信箱要重取一次当日进度。**

    不重取的话缓存里那份还是进信箱之前的：本轮刚回来的报告要等到下一轮才被
    看见，「待侦察报告」会一直挂着，攻击永远慢一拍——而慢的那一拍里，
    `_find_pirates` 又会因为「今天侦察过了」而不再派侦察，两头一夹就是死等。
    """
    fixture = _fixture(tmp_path, scout=False)
    fixture.scout_dispatch(1)

    def _collect(wanted: Any) -> dict[Coordinate, Any]:
        # 真 `collect_scout_reports` 就是这么干的：读通一份就落库一份。
        fixture.scout_report(1, HAS_FLEET, at=TODAY + timedelta(minutes=2))
        fixture.events.append("信箱")
        return {}

    fixture.loop.collect_scout_reports = _collect  # type: ignore[assignment, method-assign]

    fixture.loop._sweep()

    assert fixture.events == [*_walk(), "信箱", "goto 1", "attack 1"]


def test_a_system_whose_targets_are_all_done_needs_no_mailbox_trip(tmp_path: Path) -> None:
    """整系都处理完时连信箱都不该翻——那一趟是十几秒的纯导航。"""
    fixture = _fixture(tmp_path)
    fixture.scout_dispatch(1)
    fixture.scout_report(1, ALL_ZERO, at=TODAY)

    def _collect(wanted: Any) -> dict[Coordinate, Any]:
        fixture.events.append("信箱")
        return {}

    fixture.loop.collect_scout_reports = _collect  # type: ignore[assignment, method-assign]

    fixture.loop._sweep()

    assert "信箱" not in fixture.events


class _Reading:
    """`vision.scout_reports.PirateScoutReading` 里 `_decide_and_attack` 用到的两个字段。"""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.trigger_ships = HAS_FLEET
