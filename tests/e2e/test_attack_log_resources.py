"""攻击日志那一行要看得见这一发捞了多少。

⚠️ **近似值必须标出来。** `928K` 是画面上缩写显示的，真值取不回来了（误差 ±500）；
`233` 是精确读到的。用户接受这个精度（口径 2026-08-17），但把近似值渲染得像精确
读数是另一回事——仓库里已有先例（`military_score_estimated`）。

⚠️ **名字没核对过就显示「第 N 格」。** 库里存的是槽位，编一个资源名挂上去是这一块
最坏的失败方式：数字全对，只是安在了别的资源名下，页面上一点异样都没有。
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
    BattleResourceEntry,
)
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.pirate_reports import OUTCOME_VICTORY
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.database import scratch_database_url
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 137, 4)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
DISPATCHED = datetime(2026, 8, 17, 3, 55, tzinfo=UTC)
PRESET = FleetPresetRef(name="AAA", signature="深空吞噬者:70")

#: 用户 2026-08-17 那份 VICTORY 战报里非零的几格（截取三格够钉住三种写法了）。
HAUL = (
    BattleResourceEntry(slot=0, amount=928_000, approximate=True, uncertainty=500),
    BattleResourceEntry(slot=1, amount=501_100, approximate=True, uncertainty=50),
    BattleResourceEntry(slot=6, amount=233),
)


def _client(tmp_path: Path, resources: tuple[BattleResourceEntry, ...]) -> TestClient:
    engine = create_database_engine(scratch_database_url(tmp_path, "haul.db"))
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
        factory, plan_id=plan.id, idempotency_key="haul-page-0001", created_at_utc=DISPATCHED
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
    repository.append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=DISPATCHED + timedelta(minutes=43),
            attacker_origin=ORIGIN,
            defender_target=TARGET,
            raw_time_text="17/08/2026 04:38:46",
            outcome=OUTCOME_VICTORY,
            attacker_losses=0,
            defender_losses=783,
            resources=resources,
        )
    )
    return TestClient(create_persistent_app(factory))


def test_the_haul_shows_up_on_the_row(tmp_path: Path) -> None:
    body = _client(tmp_path, HAUL).get("/logs").text

    assert "收获" in body
    assert "928,000" in body
    assert "501,100" in body
    assert "233" in body


def test_approximate_values_are_marked_and_exact_ones_are_not(tmp_path: Path) -> None:
    """⚠️ 这一条钉的是**诚实**，不是排版。

    `928K` 的真值取不回来了，页面上写「约 928,000」；`233` 是逐位读到的，
    不带「约」。两者显示成同一个样子，就是把一个 ±500 的估算说成了确数。
    """
    body = _client(tmp_path, HAUL).get("/logs").text

    assert "约 928,000" in body
    assert "约 501,100" in body
    assert "约 233" not in body


def test_the_error_range_follows_the_displayed_digits(tmp_path: Path) -> None:
    """`928K` 是三位有效数字（±500），`501.1K` 是四位（±50）——差一个数量级。

    对所有近似值统一按一个误差算，页面上给出的范围就是编的。
    """
    body = _client(tmp_path, HAUL).get("/logs").text

    assert "误差不超过 ±500" in body
    # 结尾那个引号是 `title="…"` 的右引号，用它把 `±50` 和 `±500` 区分开。
    assert '误差不超过 ±50"' in body


def test_slots_are_rendered_as_their_confirmed_resource_names(tmp_path: Path) -> None:
    """槽位在**渲染时**翻译成资源名（用户 2026-08-17 逐格确认的那张表）。

    库里存的仍是 0..11：这次从「第 N 格」换成真名只改了一行常量，历史数据自动
    跟着对上——把名字写进库就再也没有第二次了。
    """
    body = _client(tmp_path, HAUL).get("/logs").text

    assert "金属 约 928,000" in body
    assert "晶体 约 501,100" in body
    assert "晶体矿石 233" in body
    assert "第 1 格" not in body


def test_a_blank_haul_says_nothing_at_all(tmp_path: Path) -> None:
    """12 格全 0 时不摆一行「收获」。

    库里只存非零的格子，「没有行」就是「这一发没捞着东西」；为它专门摆一句话
    只会挤占真正有收获的那几行。
    """
    body = _client(tmp_path, ()).get("/logs").text

    assert "收获" not in body
