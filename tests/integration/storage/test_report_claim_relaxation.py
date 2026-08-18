"""出发点读错时，战报还认不认得上那一发派遣。

## 实机故障（生产库 2026-08-18）

用户第二颗出发星球 `9:250:8` 打出去的战报，**每一份**都认领不上：

    battle_reports  3:250:8 → 8:80:19  23:38:49  dispatch_id=NULL  UNMATCHED
    attack_dispatches  9:250:8 → 8:80:19  预计抵达 23:38:52

目标一模一样、时刻差 3 秒，唯一对不上的是攻方出发点——`9` 被 OCR 读成了 `3`
（`[` 与 `9` 在 7× LANCZOS 下糊成一个 `3`，识别那一侧的修法在
`vision.scan_reading.vote_coordinate`）。而出发点当时是**硬条件**，写在
`_unmatched_dispatch_candidates` 的 `WHERE` 里，于是候选数直接是零。

这颗星在 `battle_reports` 里一份都不存在：7 份全被记成 `3:250:8`，
**多出发星球这条链路的战报账目整个是空的**。

## 判据改成什么

出发点从硬条件降级成打分项：目标 + 抵达时刻定人，出发点只决定置信度。
三档与各自的数据依据写在 `storage.repository._link_dispatch` 与
`MATCH_EXPECTED_WINDOW_AFTER` 上，这里只钉行为。

⚠️ **放宽不等于谁都能认。** 本文件里真正有牙的是那几条「不认」的用例：
窗口里有两发就一发都不认、抵达时刻差 20 分钟就不认、飞行时长没读到就不认。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
    FleetPresetRef,
)
from evo_helper.storage import models as orm
from evo_helper.storage.repository import (
    CONFIDENCE_EXPECTED_WINDOW,
    CONFIDENCE_ORIGIN_MISMATCH,
    CONFIDENCE_ORIGIN_TARGET_TIME,
)

#: 实机那两颗坐标：真的出发星，与战报上读出来的那个。
ORIGIN = Coordinate(9, 250, 8)
MISREAD_ORIGIN = Coordinate(3, 250, 8)
OTHER_ORIGIN = Coordinate(4, 277, 15)
TARGET = Coordinate(8, 80, 19)

DISPATCHED_AT = datetime(2026, 8, 18, 14, 36, 46, tzinfo=UTC)
FLIGHT = timedelta(minutes=62)
#: 实机那一份差 −3.4 秒：战报比预计抵达早了一点点。
REPORTED_AT = DISPATCHED_AT + FLIGHT - timedelta(seconds=3)


class RecordingLog:
    """把 `record_system_log` 的调用记下来。签名与真的那一个一致。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, level, source, message, *, payload=None, logged_at_utc=None):  # type: ignore[no-untyped-def]
        self.entries.append((level, message, dict(payload or {})))

    @property
    def messages(self) -> list[str]:
        return [message for _level, message, _payload in self.entries]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingLog]:
    log = RecordingLog()
    monkeypatch.setattr("evo_helper.storage.repository.record_system_log", log, raising=True)
    yield log


def _dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id: UUID,
    *,
    origin: Coordinate = ORIGIN,
    at: datetime = DISPATCHED_AT,
    flight: timedelta | None = FLIGHT,
    preset: str = "AAA",
) -> UUID:
    """派一发，并按 `flight` 记下飞行时长（也就是 `expected_report_at_utc`）。

    `flight=None` 走的是实机上 13% 的那一档：飞行时间没读到，没有预计抵达时刻。
    """
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=origin,
            target=TARGET,
            preset=FleetPresetRef(name=preset, signature="sig"),
            cycle_start_utc=at,
            created_at_utc=at,
            target_kind=TARGET_KIND_PIRATE,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=at,
            accepted=True,
            mission_kind=MISSION_KIND_ATTACK,
        )
    )
    repository.record_flight_time(dispatch_id, flight, at)
    return dispatch_id


def _append(  # type: ignore[no-untyped-def]
    repository,
    *,
    origin: Coordinate = MISREAD_ORIGIN,
    at: datetime = REPORTED_AT,
) -> UUID:
    report_id = uuid4()
    repository.append_report(
        BattleReport(
            report_id=report_id,
            reported_at_utc=at,
            attacker_origin=origin,
            defender_target=TARGET,
            raw_time_text=at.strftime("%d/%m/%Y %H:%M:%S"),
            outcome="VICTORY",
        )
    )
    return report_id


def _row(repository, report_id: UUID) -> orm.BattleReportRow:  # type: ignore[no-untyped-def]
    with repository._session_factory() as session:  # noqa: SLF001 - 直接读列，绕开被测方法
        row = session.scalar(select(orm.BattleReportRow).where(orm.BattleReportRow.id == report_id))
        assert row is not None
        return row


# -- 认得上 -------------------------------------------------------------------


def test_a_misread_origin_still_claims_by_target_and_arrival(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **本文件的重点。** 出发点读错，目标与抵达时刻仍然唯一定下那一发。

    实机那 7 份就卡在这里：出发点是硬条件时候选数为零，而它们的目标与时刻分毫不差。
    """
    dispatch_id = _dispatch(repository, run_id)

    row = _row(repository, _append(repository))

    assert row.dispatch_id == dispatch_id
    assert row.match_status == "MATCHED"


def test_the_relaxed_claim_is_not_recorded_as_certain(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """凭两个字段认下来的，**不许记成凭三个字段认的**。

    `match_confidence` 是页面与排障唯一能看出「这一份是怎么认下来的」的地方。
    放宽之后统一给 1.0 的话，一份猜出来的和一份量出来的在库里长得一模一样，
    而这个仓库的规矩是「宁可留 NULL，不许让猜出来的数长得像量出来的」。
    """
    _dispatch(repository, run_id)
    relaxed = _row(repository, _append(repository))

    assert relaxed.match_confidence == CONFIDENCE_ORIGIN_MISMATCH
    assert relaxed.match_confidence < CONFIDENCE_ORIGIN_TARGET_TIME


def test_a_matching_origin_still_claims_at_full_confidence(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """出发点相符那一档一个字都没变：照旧 `MATCHED` + 1.0，且**不要求**飞行时长。

    绝大多数战报走这一档。放宽是加了一条后路，不是把原来那条改窄。
    """
    dispatch_id = _dispatch(repository, run_id, flight=None)

    row = _row(repository, _append(repository, origin=ORIGIN))

    assert row.dispatch_id == dispatch_id
    assert row.match_confidence == CONFIDENCE_ORIGIN_TARGET_TIME


def test_two_same_origin_legs_are_split_by_the_arrival_window(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """同一出发点两发都在 12 小时容差里时，用**预计抵达**把它们分开。

    生产库里 4 份 `AMBIGUOUS` 是这个形状（2026-08-12 的 2:137:1 / :2 / :3 与
    2:139:3）：真正那一发差 −3 秒，另一发差 20 分钟，而两发都够得着 12 小时容差。
    分开之后置信度记 0.9——比 1.0 低，因为是靠窗口收窄才定下来的。
    """
    _dispatch(repository, run_id, at=DISPATCHED_AT - timedelta(minutes=20), preset="早一发")
    right = _dispatch(repository, run_id)

    row = _row(repository, _append(repository, origin=ORIGIN))

    assert row.dispatch_id == right
    assert row.match_confidence == CONFIDENCE_EXPECTED_WINDOW


# -- 认不上（放宽的边界） ------------------------------------------------------


def test_two_legs_inside_the_arrival_window_claim_nothing(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 窗口里有两发就**一发都不认**，绝不挑一个。

    这是放宽之后唯一还挡着「认错人」的东西。挑一个的实现在这里会绿——它挑中的
    那一发有 50% 概率是对的，而错的那一半会把战果挂到没打过的那一发头上，
    页面上看起来一切正常。
    """
    _dispatch(repository, run_id)
    _dispatch(repository, run_id, origin=OTHER_ORIGIN, at=DISPATCHED_AT + timedelta(seconds=30))

    row = _row(repository, _append(repository))

    assert row.dispatch_id is None
    assert row.match_status == "AMBIGUOUS"
    assert row.match_confidence == 0.0


def test_a_leg_that_arrived_twenty_minutes_off_is_not_claimed(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 抵达窗口必须**窄到套不住两发**。

    生产库里「同目标、时刻相近、却是两发不同派遣」只有 3 例，每一例里真正那一发
    差 −2s…+7s，另一发差 1183s…1193s。窗口一旦放宽到分钟量级，那两发就会同时
    落进来——而这里钉的是它落不进来：差 20 分钟的那一发，出发点又对不上，不认。

    差这么多的那一档在生产库里有 37 份，成因是**飞行时长读错**，不是战报来得晚。
    救它们要靠猜，所以这里明确不救。
    """
    _dispatch(repository, run_id)

    row = _row(repository, _append(repository, at=REPORTED_AT + timedelta(minutes=20)))

    assert row.dispatch_id is None
    assert row.match_status == "UNMATCHED"


def test_a_leg_without_a_flight_time_is_not_claimed_by_a_misread_origin(  # type: ignore[no-untyped-def]
    repository, run_id
) -> None:
    """飞行时长没读到（`expected_report_at_utc` 为 NULL）时**不放宽**。

    没有预计抵达就没有窄窗口，放它进来等于只凭「目标相同 + 6 小时之内」认领——
    那是拿「什么都不知道」换一次认领。实机上有 13% 的派遣是这一档。
    """
    _dispatch(repository, run_id, flight=None)

    row = _row(repository, _append(repository))

    assert row.dispatch_id is None
    assert row.match_status == "UNMATCHED"


# -- 日志：出事时只看库里的日志就要能定位 --------------------------------------


def test_an_origin_mismatch_leaves_a_line_in_the_system_log(  # type: ignore[no-untyped-def]
    repository, run_id, recorded: RecordingLog
) -> None:
    """⚠️ 这条钉的是**日志本身存在**，而且说得出两边的读数。

    删掉那一句 `record_system_log`，上面的行为用例仍然全绿——战报照样认上了——
    但库里一个字都没有，没人知道「这一份是在出发点对不上的情况下认下来的」。
    而它恰恰是最该被人看一眼的那一档。
    """
    _dispatch(repository, run_id)
    _append(repository)

    mismatched = [
        (level, message, payload)
        for level, message, payload in recorded.entries
        if payload.get("origin_matched") is False
    ]
    assert len(mismatched) == 1
    level, message, payload = mismatched[0]
    assert level == "WARNING"
    assert str(MISREAD_ORIGIN) in message and str(ORIGIN) in message
    assert payload["report_origin"] == str(MISREAD_ORIGIN)
    assert payload["dispatch_origin"] == str(ORIGIN)
    assert payload["confidence"] == CONFIDENCE_ORIGIN_MISMATCH


def test_an_unclaimed_report_says_what_it_saw(  # type: ignore[no-untyped-def]
    repository, run_id, recorded: RecordingLog
) -> None:
    """认不上的那一条要写清**当时看到了哪几个候选**。

    只说一句「认不上」的日志等于没写：2026-08-17 那次整晚空转就是这么拖了两天。
    候选连同各自的出发点、派出时刻、预计抵达一起留下，事后才分得清是判据太严、
    是出发点读错，还是那一发压根没进库。
    """
    _dispatch(repository, run_id)
    _dispatch(repository, run_id, origin=OTHER_ORIGIN, at=DISPATCHED_AT + timedelta(seconds=30))

    _append(repository)

    unclaimed = [
        payload for _level, _message, payload in recorded.entries if "candidates" in payload
    ]
    assert len(unclaimed) == 1
    payload = unclaimed[0]
    assert payload["candidate_count"] == 2
    assert payload["report_origin"] == str(MISREAD_ORIGIN)
    assert payload["match_status"] == "AMBIGUOUS"
    origins = {str(item["origin"]) for item in payload["candidates"]}  # type: ignore[index,union-attr]
    assert origins == {str(ORIGIN), str(OTHER_ORIGIN)}


def test_a_claim_that_never_changes_is_not_logged_again(  # type: ignore[no-untyped-def]
    repository, run_id, recorded: RecordingLog
) -> None:
    """⚠️ 回头重认那条路**每趟信箱都可能走**，认不上的话不能每趟写一遍。

    限流的口径是「只在 `match_status` 真的变了时写」（CLAUDE.md：每 tick 可能
    触发的要限流）。入库那一刻写的那一条留着——那是唯一一次「这份战报进来了」。
    """
    _append(repository)
    before = len(recorded.entries)

    for _ in range(3):
        assert repository.rematch_report_at(TARGET, REPORTED_AT) is False

    assert len(recorded.entries) == before


# -- 已经在库里的那些行能不能回填 ----------------------------------------------


def test_a_stuck_report_is_claimed_by_the_batch_rematch(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """判据修好之后，**已经在库里**的那些行要能被批量重认接上。

    实机那 7 份是先入库、后修判据的，而 `has_report_at` 那道去重保证了它们永远
    不会被重新读一遍——没有这条路，改判据对它们毫无作用。
    """
    report_id = _append(repository)
    assert _row(repository, report_id).dispatch_id is None
    dispatch_id = _dispatch(repository, run_id)

    assert repository.rematch_unlinked_reports() == 1

    row = _row(repository, report_id)
    assert row.dispatch_id == dispatch_id
    assert row.match_confidence == CONFIDENCE_ORIGIN_MISMATCH


def test_the_dry_run_plan_says_what_would_happen_and_writes_nothing(  # type: ignore[no-untyped-def]
    repository, run_id, recorded: RecordingLog
) -> None:
    """⚠️ 干跑必须**一个字节都不写**，日志也不写。

    这条钉的是「动生产数据之前先能看一眼」的那个「看」是真的只看。写一条
    「已认领」的日志而实际什么都没发生，比不写更糟——日志说假话这个仓库已经
    为它付过两天的代价。
    """
    report_id = _append(repository)
    dispatch_id = _dispatch(repository, run_id)
    before = len(recorded.entries)

    plans = repository.plan_unlinked_rematch()

    assert len(plans) == 1
    plan = plans[0]
    assert plan.claims and plan.dispatch_id == dispatch_id
    assert plan.previous_status == "UNMATCHED" and plan.status == "MATCHED"
    assert plan.report_origin == MISREAD_ORIGIN, "干跑要说出战报上读到的是什么"
    assert plan.dispatch_origin == ORIGIN, "也要说出派遣记录上是什么，两者不同才看得出读错了"
    assert plan.match_confidence == CONFIDENCE_ORIGIN_MISMATCH

    assert _row(repository, report_id).dispatch_id is None, "干跑改了库"
    assert len(recorded.entries) == before, "干跑往库里写了一条没发生过的事"
