"""情报中心接口：海盗不在结果里、快速过滤、以及四个判定舰种的 `null`。

页面把这些直接摊在表上，所以接口这一层的形状就是页面的形状：

- **海盗一行都不返回**（用户口径 2026-08-14：海盗 24 小时刷新，进不了长期台账；
  它的每一发仍在攻击日志里）。而且**总数与候选值要跟着收**：接口把 `total` 一起
  返回，页码由它算；`/api/intel/presets` 是「预设」下拉框的候选值。少收一处，
  页面上就会出现「共 3 条」却只看得见 1 行、或者一个选下去必然空页的选项。
- `preset` / `dispatch_state` / `battle_result` 的**空串等同于不筛**。下拉框里
  「不限」那一项的 value 就是空串，当成「预设名叫空字符串」的话，这一页的默认
  选项点下去永远是 0 条（同 `web.app._blank_to_none` 那条 PR #74 的教训）。
- `scout_ships[].count` **可以是 null，而 null 不是 0**。序列化那一层顺手
  `or 0` 是最容易发生的地方，因为 JSON 里两者只差一个词。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from evo_helper.application.report_ingest import to_scout_report
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
    CoordinateScan,
)
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.scout_reports import PirateScoutReading
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.database import scratch_database_url
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
BASE_TIME = datetime(2026, 8, 11, 3, 0, 0, tzinfo=UTC)
SPAN = {"start": "2:1", "end": "2:999"}

BOT = Coordinate(2, 320, 11)
PIRATE = Coordinate(2, 137, 4)
#: 扫描认出来的 bot，身上又挂着一份侦察报告。海盗离场之后，这是 `scout_ships`
#: 那条序列化路唯一还走得到的地方——而「null 不是 0」正是在那条路上最容易丢的。
SCOUTED_BOT = Coordinate(2, 330, 7)


@pytest.fixture
def client(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(scratch_database_url(tmp_path, "quick-api.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    _seed(factory)
    app = create_persistent_app(factory, local_token="test-token")
    client = TestClient(app)
    client.headers.update({"X-Evo-Helper-Token": "test-token"})
    return client


def _seed(factory) -> None:  # type: ignore[no-untyped-def]
    service = PersistentApplicationService(factory, now_utc=lambda: BASE_TIME)
    plan = service.create_plan(
        name="quick-api",
        enabled=True,
        window_start=datetime(2026, 1, 1, 8).time(),
        window_end=datetime(2026, 1, 1, 20).time(),
        ranges=(ScanRangeView(Coordinate(2, 1, 1), Coordinate(2, 999, 20), ORIGIN, "AAA", "x", 0),),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="quick-api-0001", created_at_utc=BASE_TIME
    )
    repository = SqlAlchemyRepository(factory)

    repository.save_scan(
        CoordinateScan(
            run_id=run_id,
            coordinate=BOT,
            scanned_at_utc=BASE_TIME,
            owner_name="bot_2_320_11",
            is_bot=True,
            confidence=1.0,
        )
    )
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=BOT,
        preset=FleetPresetRef(name="探路", signature="小型运输船:1"),
        cycle_start_utc=BASE_TIME,
        created_at_utc=BASE_TIME,
        target_kind=TARGET_KIND_BOT,
    )
    repository.save_attack_intent(intent)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=BASE_TIME,
            accepted=True,
            mission_kind=MISSION_KIND_ATTACK,
        )
    )
    repository.append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=BASE_TIME + timedelta(minutes=30),
            attacker_origin=ORIGIN,
            defender_target=BOT,
            fleet=(),
            defender_units=319,
            outcome="VICTORY",
        )
    )

    # 一个只被侦察过、又被派过一发的海盗。它**不该**出现在任何一个响应里，
    # 也不该给「预设」下拉框贡献出「AAA」这个选项。
    pirate_intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=PIRATE,
        preset=FleetPresetRef(name="AAA", signature="深空吞噬者:70"),
        cycle_start_utc=BASE_TIME,
        created_at_utc=BASE_TIME,
        target_kind=TARGET_KIND_PIRATE,
    )
    repository.save_attack_intent(pirate_intent)
    repository.append_scout_report(
        to_scout_report(
            PirateScoutReading(
                raw_time_text="11/08/2026 03:00:00",
                reported_at_utc=BASE_TIME,
                origin=ORIGIN,
                target=PIRATE,
                trigger_ships={"深空吞噬者": 0, "钛能守卫者": 7},
                missing=("噬能截击者", "收割者"),
            ),
            report_id=uuid4(),
        )
    )

    repository.save_scan(
        CoordinateScan(
            run_id=run_id,
            coordinate=SCOUTED_BOT,
            scanned_at_utc=BASE_TIME,
            owner_name="bot_2_330_7",
            is_bot=True,
            confidence=1.0,
        )
    )
    repository.append_scout_report(
        to_scout_report(
            PirateScoutReading(
                raw_time_text="11/08/2026 03:00:00",
                reported_at_utc=BASE_TIME,
                origin=ORIGIN,
                target=SCOUTED_BOT,
                trigger_ships={"深空吞噬者": 0, "钛能守卫者": 7},
                missing=("噬能截击者", "收割者"),
            ),
            report_id=uuid4(),
        )
    )


def _search(client: TestClient, **payload: Any) -> dict[str, Any]:
    response = client.post("/api/intel/search", json={"span": SPAN, **payload})
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def _row(client: TestClient, coordinate: Coordinate) -> dict[str, Any]:
    rows = {row["coordinate"]: row for row in _search(client)["rows"]}
    return rows[str(coordinate)]


class TestPiratesAreNotReturned:
    """海盗 24 小时刷新，进不了长期台账（见文件头）；它的记录仍在攻击日志里。"""

    def test_a_scouted_and_dispatched_pirate_is_absent(self, client: TestClient) -> None:
        page = _search(client)

        assert str(PIRATE) not in {row["coordinate"] for row in page["rows"]}

    def test_the_total_shrinks_with_the_rows(self, client: TestClient) -> None:
        """**这条钉的是「计数也跟着收了」。**

        `total` 是页码的分母，也是页脚「共 N 条」那个数。只在渲染那一层跳过海盗的话，
        它仍然按含海盗的集合算——于是标题写着共 3 条，翻到第 2 页却一行都没有。
        """
        page = _search(client)

        assert {row["coordinate"] for row in page["rows"]} == {str(BOT), str(SCOUTED_BOT)}
        assert page["total"] == 2


class TestBlankFiltersMeanNoFilter:
    def test_empty_strings_do_not_wipe_the_result(self, client: TestClient) -> None:
        """三个下拉框都停在「不限」时，提交的是空串——那必须是「全都要」。"""
        page = _search(client, preset="", dispatch_state="", battle_result="")

        assert {row["coordinate"] for row in page["rows"]} == {str(BOT), str(SCOUTED_BOT)}

    def test_an_unknown_state_is_a_422_with_a_readable_message(self, client: TestClient) -> None:
        """打错的档位当场说出来，而不是安静地筛出 0 条。"""
        response = client.post("/api/intel/search", json={"span": SPAN, "dispatch_state": "ALMOST"})

        assert response.status_code == 422
        assert "ALMOST" in response.json()["detail"]


class TestRowShape:
    def test_a_bot_row_carries_its_kind_and_latest_attempt(self, client: TestClient) -> None:
        row = _row(client, BOT)

        assert row["kind"] == TARGET_KIND_BOT
        assert row["preset_name"] == "探路"
        assert row["dispatch_state"] == "SENT"
        assert row["battle_result"] == "VICTORY"

    def test_a_scanned_bot_keeps_its_kind_even_with_a_scout_report(
        self, client: TestClient
    ) -> None:
        """列表要按类型上色，而类型现在还决定这一行进不进这张表。

        `bot_targets` 里那一行是坐标扫描认出来的**正面证据**，压过「有侦察报告 =
        海盗」那条推断；判反了就等于把一颗真的 bot 从台账里删掉。
        """
        assert _row(client, SCOUTED_BOT)["kind"] == TARGET_KIND_BOT


class TestScoutShipsKeepNullApartFromZero:
    def test_zero_is_zero_and_unread_is_null(self, client: TestClient) -> None:
        """**这条是这份文件的重点。**

        0 是「对方没有这种船」，null 是「这一格没看清」。序列化时顺手 `or 0`
        会把后者变成前者，而页面照着 0 显示就等于告诉用户「这里是空的」。
        """
        ships = {s["ship_type"]: s["count"] for s in _row(client, SCOUTED_BOT)["scout_ships"]}

        assert ships["深空吞噬者"] == 0
        assert ships["钛能守卫者"] == 7
        assert ships["噬能截击者"] is None
        assert ships["收割者"] is None

    def test_a_bot_row_has_no_scout_ships(self, client: TestClient) -> None:
        """侦察只对海盗做。bot 行凭空多出四格，页面就会把它们当成真读到的数。"""
        assert _row(client, BOT)["scout_ships"] == []


class TestPresetsEndpoint:
    def test_a_preset_only_used_on_pirates_is_not_offered(self, client: TestClient) -> None:
        """**这条钉的是「候选值也跟着收了」。**

        库里这两个预设一人一个：「探路」打的是 bot，「AAA」这一发打的是海盗。
        海盗不在这张表里了，「AAA」留在下拉框里就是留一个选下去必然是空页的选项——
        用户会以为记录被弄丢了，而它一直在攻击日志里。
        """
        response = client.get("/api/intel/presets")

        assert response.status_code == 200
        assert response.json() == ["探路"]

    def test_the_offered_preset_actually_has_rows(self, client: TestClient) -> None:
        """候选值与结果说的是同一批行：列出来的每一个选下去都得有东西。"""
        for name in client.get("/api/intel/presets").json():
            assert _search(client, preset=name)["total"] > 0


class TestQuickFilteringOverHttp:
    def test_filtering_by_preset_narrows_the_page(self, client: TestClient) -> None:
        page = _search(client, preset="探路")

        assert [row["coordinate"] for row in page["rows"]] == [str(BOT)]
        assert page["total"] == 1

    def test_filtering_by_battle_result_narrows_the_page(self, client: TestClient) -> None:
        page = _search(client, battle_result="VICTORY")

        assert [row["coordinate"] for row in page["rows"]] == [str(BOT)]


class TestTheIntelPageDeclaresItsSemantics:
    def test_the_page_says_the_filters_read_the_latest_attempt(self, client: TestClient) -> None:
        """口径必须写在页面上。

        「预设 / 结果 / 战果」挂在派遣上，而这张表一行是一个目标——同一个目标
        可能被打过很多次。不写清楚的话，用户会以为筛的是「历史上打过就算」，
        而那是另一套完全不同的结果。
        """
        body = client.get("/intel").text

        assert "最近一次派遣" in body

    def test_the_page_says_it_only_lists_bots_and_where_the_pirates_went(
        self, client: TestClient
    ) -> None:
        """页面上少了一半数据，就得在页面上说清楚少的是什么、去哪儿看。

        不写的话，用户看到的是「情报中心里的海盗没了」——那和「数据丢了」长得一样。
        """
        body = client.get("/intel").text

        assert "只列" in body
        assert "海盗" in body
        assert "/logs" in body
