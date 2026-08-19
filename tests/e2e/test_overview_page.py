"""「数据概览」页。

断言全部打在 `/overview` 返回的 HTML 上：这一页的价值就在那几个数摆对了没有，
取不到 HTML 就什么都守不住。

**这里不真的 Popen 任何 runner**：`launch` 一律注入假的、后台 tick 推到一小时
一次。真起一个会去点用户的真实鼠标、派真实舰队。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_freeze import MissionFreezeLog
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.models import (
    AttackDispatchRow,
    AttackIntentRow,
    BattleReportResourceRow,
    BattleReportRow,
    MissionTaskRow,
    RunInstance,
    ScanPlan,
)
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app
from support.database import scratch_database_url

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
TOKEN = "test-token"

#: 用户实测的两颗星球与它们**各自**配的航线数（需求文档 8.3 点名的那两个数）。
HOME = Coordinate(4, 277, 15)
SECOND = Coordinate(9, 250, 8)
HOME_LINES = 5
SECOND_LINES = 4


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _FakeLauncher:
    def __call__(self, kind: MissionKind, command: Sequence[str], log_path: Path) -> _FakeProcess:
        return _FakeProcess(pid=9001)


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(scratch_database_url(tmp_path, "overview.db"))
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def run_id(factory: sessionmaker[Session]) -> UUID:
    with factory() as session:
        plan = ScanPlan(name="overview-fixture", created_at_utc=NOW)
        session.add(plan)
        session.flush()
        run = RunInstance(
            plan_id=plan.id,
            idempotency_key="overview-fixture-001",
            state="SCANNING",
            created_at_utc=NOW,
        )
        session.add(run)
        session.commit()
        return run.id


@pytest.fixture
def repository(factory: sessionmaker[Session]) -> SqlAlchemyRepository:
    repository = SqlAlchemyRepository(factory)
    repository.ensure_mission_rows(now_utc=NOW)
    return repository


@pytest.fixture
def planets(repository: SqlAlchemyRepository) -> None:
    """把两颗出发星球配上，**各自的航线数不同**。

    两颗配成同一个数的话，「格子按每颗星球各自的配置画」这条就守不住了——
    写死一个常量也能过。
    """
    home = repository.create_attack_planet(HOME)
    second = repository.create_attack_planet(SECOND)
    task = next(row for row in repository.mission_tasks() if row.kind == MissionKind.BOT.value)
    repository.update_mission_task(task.id, params_json='{"by_military": true}')
    repository.replace_mission_task_origins(
        task.id, [(home.id, HOME_LINES, True), (second.id, SECOND_LINES, True)]
    )


@pytest.fixture
def client(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[TestClient]:
    supervisor = MissionSupervisor(
        launch=_FakeLauncher(), clock=lambda: NOW, log_dir=tmp_path / "logs"
    )
    app = create_persistent_app(
        factory,
        local_token=TOKEN,
        mission_scheduler=MissionScheduler(
            SqlAlchemyRepository(factory),
            supervisor,
            clock=lambda: NOW,
            freeze_log=MissionFreezeLog(tmp_path / "freezes.jsonl"),
        ),
        # 后台 tick 先 sleep 再 tick，推到一小时就等于「测试期间不会自己跑」。
        tick_interval_s=3600.0,
    )
    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as test_client:
        yield test_client


_CYCLE = datetime(2020, 1, 1, tzinfo=UTC)


def _dispatch(
    factory: sessionmaker[Session],
    run_id: UUID,
    *,
    origin: Coordinate = HOME,
    dispatched_at_utc: datetime,
    line_free_at_utc: datetime | None = None,
    expected_report_at_utc: datetime | None = None,
) -> UUID:
    global _CYCLE
    _CYCLE += timedelta(seconds=1)
    intent_id = uuid4()
    dispatch_id = uuid4()
    with factory() as session:
        session.add(
            AttackIntentRow(
                id=intent_id,
                run_id=run_id,
                origin_galaxy=origin.galaxy,
                origin_system=origin.system,
                origin_position=origin.position,
                target_galaxy=2,
                target_system=130,
                target_position=4,
                preset_name="AAA",
                preset_signature="sig",
                cycle_start_utc=_CYCLE,
                created_at_utc=dispatched_at_utc,
                target_kind="bot",
            )
        )
        session.flush()
        session.add(
            AttackDispatchRow(
                id=dispatch_id,
                intent_id=intent_id,
                dispatched_at_utc=dispatched_at_utc,
                accepted=True,
                line_free_at_utc=line_free_at_utc,
                expected_report_at_utc=expected_report_at_utc,
            )
        )
        session.commit()
    return dispatch_id


def _report(
    factory: sessionmaker[Session],
    *,
    reported_at_utc: datetime,
    dispatch_id: UUID | None = None,
    resources: tuple[tuple[int, int], ...] = (),
) -> None:
    report_id = uuid4()
    with factory() as session:
        session.add(
            BattleReportRow(
                id=report_id,
                reported_at_utc=reported_at_utc,
                attacker_origin_galaxy=HOME.galaxy,
                attacker_origin_system=HOME.system,
                attacker_origin_position=HOME.position,
                defender_target_galaxy=2,
                defender_target_system=130,
                defender_target_position=4,
                dispatch_id=dispatch_id,
            )
        )
        session.flush()
        for slot, amount in resources:
            session.add(
                BattleReportResourceRow(id=uuid4(), report_id=report_id, slot=slot, amount=amount)
            )
        session.commit()


def _slot_classes(html: str) -> list[list[str]]:
    """每一张航线卡片上的格子，按出现顺序。"""
    return [
        re.findall(r'class="slot-(\w+)"', block)
        for block in re.findall(r'<div class="overview-slots".*?</div>', html, re.S)
    ]


# -- 页面能打开 -----------------------------------------------------------------


def test_the_page_opens_while_the_scheduler_is_stopped(client: TestClient) -> None:
    """⚠️ 这一页**不依赖 runner**（需求文档第七节）。

    调度器停着时它照常打开——读的全是库里已有的行。
    """
    response = client.get("/overview")

    assert response.status_code == 200
    assert "数据概览" in response.text
    assert "已停止" in response.text


def test_the_page_opens_on_an_empty_database(client: TestClient) -> None:
    """一条数据都没有时也不该 500。新装的控制台第一眼看到的就是这一页。"""
    assert client.get("/overview").status_code == 200


def test_the_navigation_puts_the_overview_first(client: TestClient) -> None:
    """用户口径（2026-08-19）：「数据概览」放侧边栏**最上面**。"""
    html = client.get("/overview").text
    links = re.findall(r'<a href="(/[a-z-]+)"', html)

    assert links[0] == "/overview"
    assert links.index("/overview") < links.index("/missions")
    assert 'href="/overview" aria-current="page"' in html


def test_the_header_says_the_statistics_are_cut_on_utc(client: TestClient) -> None:
    """⚠️ 页面按 UTC+0 切天，而全站其它页面的时刻是 UTC+8。

    不写清楚会被当成同一套时区读——那次「以为出了 10 倍资源计算错误」的排查
    就是这么来的。两个统计起点也要写出来：它们不是同一天开始有数的。
    """
    html = client.get("/overview").text

    assert "统计按 <b>UTC+0</b> 切天" in html
    assert "计数类起点 2026-08-17" in html
    assert "资源类起点 2026-08-18" in html


# -- 8.3 航线格子按配置画 --------------------------------------------------------


def test_each_planet_draws_exactly_its_configured_number_of_line_cells(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ **格子按配置的航线数画，不按占用数画**（需求文档 8.3）。

    这里让 `4:277:15` 占满 7 条（配的只有 5 条），`9:250:8` 一条不占。
    原型第一版按「在飞 + 时长未知」画，那颗星球会画出 7 格——也就是在说
    「这颗星球有 7 条航线」。
    """
    for _ in range(7):
        _dispatch(factory, run_id, origin=HOME, dispatched_at_utc=NOW - timedelta(minutes=5))

    html = client.get("/overview").text
    grids = _slot_classes(html)

    assert [len(cells) for cells in grids] == [HOME_LINES, SECOND_LINES]
    # 超出的那两条要说出来，不许靠加格子表达。
    assert "超出配置 2 条" in html


def test_the_page_follows_the_configured_unknown_line_hold(
    client: TestClient,
    factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
    run_id: UUID,
    planets: None,
) -> None:
    """⚠️ **`hold` 不许写死 90 分钟**（需求文档 8.1）。

    它是用户在攻击配置页上改的那个值。这里种一发 60 分钟前派出、飞行时间没读
    出来的：按默认 90 分钟它还占着航线，把闸门调到 45 分钟之后它就该放手了。
    写死 90 的话，第二次请求仍然画着一个「飞」格——用户改了设置，页面纹丝不动。
    """
    _dispatch(factory, run_id, origin=HOME, dispatched_at_utc=NOW - timedelta(minutes=60))

    assert _slot_classes(client.get("/overview").text)[0].count("unk") == 1

    repository.replace_military_attack_tiers("[]", unknown_line_hold_minutes=45)

    assert _slot_classes(client.get("/overview").text)[0].count("unk") == 0


def test_the_unknown_duration_lines_are_shown_apart_from_the_flying_ones(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ 「时长未知」那一档必须单独显示，不许并进「在飞」。

    混在一起，页面就说不出「为什么明明没派几发却没航线了」。
    """
    _dispatch(
        factory,
        run_id,
        origin=SECOND,
        dispatched_at_utc=NOW - timedelta(minutes=5),
        line_free_at_utc=NOW + timedelta(hours=1),
    )
    for _ in range(2):
        _dispatch(factory, run_id, origin=SECOND, dispatched_at_utc=NOW - timedelta(minutes=5))

    html = client.get("/overview").text
    second_grid = _slot_classes(html)[1]

    assert second_grid == ["fly", "unk", "unk", "free"]
    assert "2 条是「时长未知」" in html


# -- 8.4 资源列旁边必须有读回战报数与回收率 ---------------------------------------


def test_the_period_table_shows_reports_and_recovery_beside_the_resources(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ **「读回战报数」与「回收率」必须和资源列并排**（需求文档 8.4）。

    资源列只统计已读回的战报，而战报是滞后读回来的——同一天的数会一直涨
    （实测 08-18 隔 11 小时从 67,594 变成 166,194）。少了这两列，用户明天再看
    同一天的数会以为出了 bug。
    """
    html = client.get("/overview").text
    header = html[html.index("<thead>") : html.index("</thead>")]
    columns = re.findall(r'<th scope="col">([^<]+)</th>', header)

    assert columns == [
        "周期",
        "派遣",
        "读回战报",
        "回收率",
        "合金碎片",
        "泰坦立方",
        "收割者碎片",
        "利用率",
    ]
    # 并排，不是分在两张表里。
    assert columns.index("读回战报") < columns.index("合金碎片")
    assert columns.index("回收率") < columns.index("合金碎片")


def test_the_period_table_reports_the_measured_numbers(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """今天派 4 发、读回 3 份、收了 27,500 合金碎片。"""
    for _ in range(4):
        _dispatch(factory, run_id, dispatched_at_utc=NOW - timedelta(hours=1))
    for amount in (20_000, 7_000, 500):
        _report(
            factory, reported_at_utc=NOW - timedelta(minutes=30), resources=((5, amount), (8, 1))
        )

    html = client.get("/overview").text
    row = re.search(r"<tr[^>]*>\s*<th scope=\"row\">08-19 今天</th>(.*?)</tr>", html, re.S)
    assert row is not None
    cells = re.findall(r"<td[^>]*>\s*([^<]*?)\s*</td>", row.group(1))

    assert cells[0] == "4"
    assert cells[1] == "3"
    assert cells[2] == "75%"
    assert cells[3] == "27,500"


def test_a_day_before_the_resource_start_still_reports_its_dispatches(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ **两个统计起点必须分开**（需求文档第四节）。

    08-13 那天真的派了几发，但一份资源明细都没有——资源识别是 08-18 才修好的。
    把两个起点合并成一个的话，要么这一天的派遣数被截成 0（合成资源起点），
    要么它凭空长出一段没有明细的资源（合成计数起点）。
    """
    for _ in range(3):
        _dispatch(factory, run_id, dispatched_at_utc=datetime(2026, 8, 13, 12, tzinfo=UTC))
    # 同一天真的入了一份战报，但那时候还读不出资源——库里就是这个形状。
    _report(factory, reported_at_utc=datetime(2026, 8, 13, 13, tzinfo=UTC))

    html = client.get("/overview").text
    row = re.search(r"<tr[^>]*>\s*<th scope=\"row\">08-13</th>(.*?)</tr>", html, re.S)
    assert row is not None
    cells = re.findall(r"<td[^>]*>\s*([^<]*?)\s*</td>", row.group(1))

    assert cells[0] == "3"
    assert cells[1] == "1"
    assert cells[3] == "0"


# -- 第九节：未读战报只算当天 ----------------------------------------------------


def test_the_unread_card_ignores_the_historic_backlog(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ 用户口径（2026-08-19）：**只统计当天，不统计历史积压。**

    实测积压 713 发、最老派于 08-09。这里种 5 发十天前的、1 发今天的，
    页面上那个大数只能是 **1**。
    """
    for index in range(5):
        moment = NOW - timedelta(days=10, minutes=index)
        _dispatch(
            factory,
            run_id,
            dispatched_at_utc=moment,
            expected_report_at_utc=moment + timedelta(minutes=40),
        )
    today = NOW - timedelta(hours=2)
    _dispatch(
        factory,
        run_id,
        dispatched_at_utc=today,
        expected_report_at_utc=today + timedelta(minutes=40),
    )

    html = client.get("/overview").text
    card = html[html.index("未读战报") : html.index("候选池按往返时长")]
    big = re.search(r'<div class="value[^"]*">\s*(\d+)', card)

    assert big is not None
    assert big.group(1) == "1"


# -- 粒度与片段 -----------------------------------------------------------------


def test_the_granularity_is_switched_by_a_link_so_it_survives_a_refresh(
    client: TestClient, planets: None
) -> None:
    html = client.get("/overview?granularity=week").text

    assert 'href="/overview?granularity=week"' in html
    assert re.search(r'class="overview-tab on"[^>]*href="/overview\?granularity=week"', html)
    assert "本周" in html


def test_an_unreadable_granularity_falls_back_to_days_instead_of_422(
    client: TestClient, planets: None
) -> None:
    response = client.get("/overview?granularity=weeek")

    assert response.status_code == 200
    assert "08-19 今天" in response.text


def test_the_two_fragments_can_be_fetched_on_their_own(client: TestClient, planets: None) -> None:
    """「此刻」几秒一轮、周期统计一分钟一轮，所以两块要能各取各的。

    整页重取会把周期统计那几趟聚合查询也按 5 秒一次地跑起来。
    """
    now_fragment = client.get("/overview?fragment=now")
    periods_fragment = client.get("/overview?granularity=month&fragment=periods")

    assert now_fragment.status_code == 200
    assert "此刻" in now_fragment.text
    assert "<thead>" not in now_fragment.text

    assert periods_fragment.status_code == 200
    assert "<thead>" in periods_fragment.text
    assert "此刻" not in periods_fragment.text


def test_the_total_row_is_anchored_at_the_count_start(client: TestClient, planets: None) -> None:
    html = client.get("/overview?granularity=total").text

    assert "合计 自 08-17" in html


# -- 只读 -----------------------------------------------------------------------


def test_the_page_offers_no_write_action_at_all(client: TestClient, planets: None) -> None:
    """⚠️ 这一页**只读**：不许有任何写操作、不许触发游戏动作（需求文档第七节）。

    判据落在「页面上有没有能按下去的东西」：表单、按钮、以及任何指向起停接口的
    地址。少了这一条，日后有人「顺手」在这里加一个「清理航线占用」，而这一页
    正是用户开着不动、每 5 秒自己刷新的那一页。
    """
    html = client.get("/overview").text
    body = html[html.index('<div class="content">') :]

    assert "<form" not in body
    assert "<button" not in body
    assert "/api/scheduler/start" not in body
    assert "/api/scheduler/stop" not in body
    assert "method='post'" not in body.lower() and 'method="post"' not in body.lower()


def test_the_page_pulls_in_no_external_resource(client: TestClient, planets: None) -> None:
    """不引入任何前端框架或外部资源——这一页没有构建步骤，加库就是给部署添麻烦。"""
    html = client.get("/overview").text

    assert "http://" not in html
    assert "https://" not in html
    assert "cdn" not in html.lower()


# -- 资源图标 -------------------------------------------------------------------


def test_the_icon_endpoint_says_no_content_when_the_database_has_no_panel(
    client: TestClient,
) -> None:
    """图标是装饰。库里还没有战报面板时返回 204，页面上那个 `<img>` 自己消失。"""
    response = client.get("/api/overview/resource-icon/5")

    assert response.status_code == 204


def test_the_rare_cards_ask_for_their_icons(client: TestClient, planets: None) -> None:
    """三个稀有资源各有一张图，地址按槽位寻址（不是按资源名）。"""
    html = client.get("/overview").text

    for slot in (5, 8, 9):
        assert f'src="/api/overview/resource-icon/{slot}"' in html
    assert 'onerror="this.remove()"' in html


def test_the_page_never_ships_a_game_asset_from_the_repository(client: TestClient) -> None:
    """图标**运行时从库里切**，不进仓库（公开仓库 + 游戏素材）。

    页面上不许出现内联的 base64 图，也不许指向 `/static/` 下的图片。
    """
    html = client.get("/overview").text

    assert "data:image" not in html
    assert not re.search(r'/static/[^"\']*\.(png|jpe?g|webp|gif)', html)


# -- 折叠 -----------------------------------------------------------------------


def test_the_other_nine_resources_are_folded_away(client: TestClient, planets: None) -> None:
    """用户口径：首屏只放稀有三样，其余九种折叠。"""
    html = client.get("/overview").text

    assert "其余九种" in html
    assert '<details class="overview-others">' in html
    # 九种全在里面，一样不少——折叠不等于丢掉。
    folded = html[html.index("其余九种") : html.index("</details>")]
    for label in ("金属", "晶体", "气体", "暗能量", "银河素", "晶体矿石"):
        assert label in folded


def test_the_review_only_amber_explainers_are_gone(client: TestClient, planets: None) -> None:
    """用户口径（2026-08-19）：**不留小字**。

    原型里那几个琥珀色说明框是给用户评审看的，成品页面不要。
    """
    html = client.get("/overview").text

    assert 'class="flag"' not in html
    assert "口径：只统计已读回战报的那部分" not in html
    assert "过去某一天的数会一直变" not in html


def test_the_scheduler_card_comes_first(client: TestClient, planets: None) -> None:
    """用户口径（2026-08-19）：调度器放「此刻」区的**第一位**。"""
    html = client.get("/overview").text
    section = html[html.index("此刻") :]

    assert section.index("调度器") < section.index("航线 · ")


def test_the_task_row_kind_is_not_needed_for_the_page_to_render(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    """没有军力攻击任务时，航线那一块说「没有配着出发星球」，而不是 500。"""
    with factory() as session:
        for row in session.query(MissionTaskRow).all():
            session.delete(row)
        session.commit()

    response = client.get("/overview")

    assert response.status_code == 200
    assert "没有配着出发星球的军力攻击任务" in response.text
