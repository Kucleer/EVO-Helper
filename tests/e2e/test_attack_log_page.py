"""攻击日志页要能把战果显示出来：胜负 + 战损总数。

海盗战报只记这两样（用户口径 2026-08-09），所以日志页上的「战果」一格就是
这条链路的全部产出——渲染不出来，等于白记。

顺带守住另一件事：**还没回战报的那一发不能显示成「零损失」**。
页面上「—」和「0」是两个完全不同的结论。
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
)
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.pirate_reports import OUTCOME_DRAW, OUTCOME_FAIL, OUTCOME_VICTORY
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 137, 4)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
DISPATCHED = datetime(2026, 8, 9, 3, 55, tzinfo=UTC)
PRESET = FleetPresetRef(name="AAA", signature="深空吞噬者:70")


def _client(tmp_path: Path, *, with_report: bool, outcome: str = OUTCOME_VICTORY) -> TestClient:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'logs.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: DISPATCHED)
    plan = service.create_plan(
        name="海盗攻击",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(
            ScanRangeView(Coordinate(2, 137, 1), TARGET, ORIGIN, PRESET.name, PRESET.signature, 0),
        ),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="log-page-0001", created_at_utc=DISPATCHED
    )
    repository = SqlAlchemyRepository(factory)
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=TARGET,
        preset=PRESET,
        cycle_start_utc=CYCLE,
        created_at_utc=DISPATCHED - timedelta(minutes=1),
        target_kind=TARGET_KIND_PIRATE,
    )
    repository.save_attack_intent(intent)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=DISPATCHED,
            accepted=True,
        )
    )
    if with_report:
        repository.append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=DISPATCHED + timedelta(minutes=43),
                attacker_origin=ORIGIN,
                defender_target=TARGET,
                raw_time_text="09/08/2026 04:38:46",
                outcome=outcome,
                attacker_losses=0,
                defender_losses=783,
            )
        )
    return TestClient(create_persistent_app(factory))


def test_the_log_page_shows_the_battle_result(tmp_path: Path) -> None:
    response = _client(tmp_path, with_report=True).get("/logs")

    assert response.status_code == 200
    body = response.text
    assert "战果" in body
    assert "胜" in body
    assert "战损 我 0" in body
    assert "敌 783" in body


def test_a_lost_battle_shows_as_a_defeat(tmp_path: Path) -> None:
    """bot 探路战报现在也会带着战果进来，而它们基本全是 `FAIL`——
    探路本来就是拿一艘船去换一个守方数量，赢不了。"""
    body = _client(tmp_path, with_report=True, outcome=OUTCOME_FAIL).get("/logs").text

    assert "负" in body
    assert "胜" not in body


def test_a_draw_is_not_rendered_as_a_defeat(tmp_path: Path) -> None:
    """⚠️ 原先这一格是「不是 VICTORY 就画成负」。

    库里存的是**画面上的原文**，多一档就会被静默画成败仗——而页面上败仗和平局
    是两个结论。这一条钉的是「兜底分支不许再回来」。
    """
    body = _client(tmp_path, with_report=True, outcome=OUTCOME_DRAW).get("/logs").text

    assert "平" in body
    assert "负" not in body


def test_an_attack_without_a_report_yet_shows_pending(tmp_path: Path) -> None:
    response = _client(tmp_path, with_report=False).get("/logs")

    assert response.status_code == 200
    assert "待战报" in response.text
    assert "战损 我 0" not in response.text


# -- 侦察发：它等的不是战报 ----------------------------------------------------


def _table_body(html: str) -> str:
    """只取 `<tbody>` 里那段。

    整页搜中文会命中顶上那三个下拉框里的选项——「待战报」正是其中之一。
    不收窄的话，「表格里没有待战报」这条断言在**修好之前也是绿的**。
    """
    start = html.find("<tbody")
    end = html.find("</tbody>", start)
    assert start != -1 and end != -1, "页面上没有表格体，这条用例的前提就不成立"
    return html[start:end]


def _scout_client(tmp_path: Path, *, with_report: bool) -> TestClient:
    """一发**侦察**派遣，外加可选的一份侦察报告。

    与上面那个 `_client` 的区别只有两处：`mission_kind` 是 `SCOUT`，
    回来的是 `ScoutReport` 而不是 `BattleReport`——而这两处正是这一格出错的地方。
    """
    from evo_helper.domain.records import MISSION_KIND_SCOUT, ScoutReport

    engine = create_database_engine(f"sqlite:///{tmp_path / 'scout-logs.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: DISPATCHED)
    plan = service.create_plan(
        name="海盗侦察",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(ScanRangeView(Coordinate(2, 137, 1), TARGET, ORIGIN, "侦察", "探测器:1", 0),),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="scout-log-0001", created_at_utc=DISPATCHED
    )
    repository = SqlAlchemyRepository(factory)
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=TARGET,
        preset=FleetPresetRef(name="侦察", signature="探测器:1"),
        cycle_start_utc=CYCLE,
        created_at_utc=DISPATCHED - timedelta(minutes=1),
        target_kind=TARGET_KIND_PIRATE,
    )
    repository.save_attack_intent(intent)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=DISPATCHED,
            accepted=True,
            mission_kind=MISSION_KIND_SCOUT,
        )
    )
    if with_report:
        repository.append_scout_report(
            ScoutReport(
                report_id=uuid4(),
                reported_at_utc=DISPATCHED + timedelta(minutes=2),
                raw_time_text="09/08/2026 03:57:00",
                origin=ORIGIN,
                target=TARGET,
            )
        )
    return TestClient(create_persistent_app(factory))


def test_a_scout_leg_never_waits_for_a_battle_report(tmp_path: Path) -> None:
    """**侦察发永远不该显示「待战报」。**

    实机 2026-08-13 通宵：111 发侦察在这一页上全部挂着「待战报」，而侦察根本不
    产生战报——它产出的是侦察报告，走 `scout_reports` 那张表。更刺眼的是其中不少
    早就把攻击带出去了：攻击都打完了，它的侦察还显示「待战报」。

    用户为此连提了两次（第二次写的是「多次仍然未修复」）。之前几次改的都是
    **情报中心**那一侧——`storage.intel._battle_result` 里那条 `SCOUT → NONE`
    早就写对了，而攻击日志是另一条渲染路径，从来没跟上。
    """
    # ⚠️ 只看表格体。整页搜「待战报」会命中顶上那个「战果」下拉框里的选项，
    # 于是这条断言在**修好之前也是绿的**——它测的是下拉框存不存在，不是那一格。
    rows = _table_body(_scout_client(tmp_path, with_report=True).get("/logs").text)

    assert "待战报" not in rows
    assert "侦察已回" in rows


def test_a_scout_leg_still_shows_that_its_report_has_not_come_back(tmp_path: Path) -> None:
    """没有这条对照，「一律显示侦察已回」也能让上面那条变绿。

    而「报告还没回来」是有用的信息：它说明这个坐标还没轮到判定，不是判完不打。
    """
    rows = _table_body(_scout_client(tmp_path, with_report=False).get("/logs").text)

    assert "待侦察报告" in rows
    assert "侦察已回" not in rows


def test_the_log_tells_a_scout_leg_from_an_attack_leg(tmp_path: Path) -> None:
    """用户口径 2026-08-14：「预设中的侦查和攻击需要标记不同颜色」。

    两种发次在这一页混排，而它们等的东西完全不同——分不出来就没法读战果那一列。
    """
    scout = _scout_client(tmp_path, with_report=True).get("/logs").text
    attack = _client(tmp_path, with_report=True).get("/logs").text

    assert "kind-scout" in scout and "kind-attack" not in scout
    assert "kind-attack" in attack and "kind-scout" not in attack
