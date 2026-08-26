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
from evo_helper.domain.battle_resources import slot_label
from evo_helper.domain.models import Coordinate
from evo_helper.domain.overview import BASIC_SLOTS
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
from evo_helper.web import app as web_package
from evo_helper.web import overview_routes
from evo_helper.web.app import create_persistent_app
from support.database import scratch_database_url
from tests.integration.application.test_mission_scheduler import set_score_window

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
TOKEN = "test-token"

#: 用户实测的两颗星球与它们**各自**配的航线数（需求文档 8.3 点名的那两个数）。
HOME = Coordinate(4, 277, 15)
SECOND = Coordinate(9, 250, 8)
HOME_LINES = 5

#: 一颗星球都没配的银河。用来钉「新鲜读数那张卡列全部星系」——拿配着星球的银河
#: 试是试不出来的，那种银河两种口径下都会出现。
FAR_GALAXY = 6
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

    ⚠️ **任务还得勾上**（`enabled=True`）：种子行建出来是不参与调度的
    （`_MISSION_SEEDS` 里 BOT 那一行是 False），而自 2026-08-26 起页面只画
    **启用中**的任务配着的星球（用户口径：未启用的任务不显示在数据概览）。
    不勾的话这一份里凡是断言航线卡片的用例都会撞在空态卡上——那是这条新判据
    在起作用，不是它们各自守的东西坏了。
    """
    home = repository.create_attack_planet(HOME)
    second = repository.create_attack_planet(SECOND)
    task = next(row for row in repository.mission_tasks() if row.kind == MissionKind.BOT.value)
    repository.update_mission_task(task.id, params_json='{"by_military": true}', enabled=True)
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
    approximate: bool = False,
    uncertainty: int = 0,
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
                BattleReportResourceRow(
                    id=uuid4(),
                    report_id=report_id,
                    slot=slot,
                    amount=amount,
                    # 画面上超过一千的值是缩写显示的（`928K`），真值取不回来；
                    # `uncertainty` 记的是那一格差多少（半个末位刻度）。
                    approximate=approximate,
                    uncertainty=uncertainty,
                )
            )
        session.commit()


def _run(
    repository: SqlAlchemyRepository,
    *,
    started_at_utc: datetime,
    ended_at_utc: datetime | None = None,
    configured_lines: int | None = None,
) -> None:
    """一轮子进程记录。

    `configured_lines=None` 就是 2026-08-20 之前那些行的形状——那一列还不存在，
    所以那些天的航线数只能推算。
    """
    run = repository.begin_mission_run(
        MissionKind.BOT,
        task_id=None,
        command=["python"],
        pid=None,
        started_at_utc=started_at_utc,
        log_path="var/logs/mission-bot.log",
        configured_lines=configured_lines,
    )
    if ended_at_utc is not None:
        repository.finish_mission_run(
            run, ended_at_utc=ended_at_utc, exit_code=0, stopped_by="SELF"
        )


def _uptime(repository: SqlAlchemyRepository, *, start: datetime, last_beat: datetime) -> None:
    """一段挂机心跳。右端是**最后一拍**，不是「停止时刻」。"""
    segment = repository.open_uptime_segment(now_utc=start)
    repository.beat_uptime_segment(segment, now_utc=last_beat)


def _period_cells(html: str, label: str) -> list[str]:
    """周期统计表里那一行的各个格子（不含最左边的周期名）。"""
    row = re.search(rf"<tr[^>]*>\s*<th scope=\"row\">{re.escape(label)}</th>(.*?)</tr>", html, re.S)
    assert row is not None, f"周期统计表里没有 {label} 这一行"
    return re.findall(r"<td[^>]*>\s*([^<]*?)\s*</td>", row.group(1))


def _utilisation_card(html: str) -> str:
    """「今天收益」下面那一行的第一张卡——航线利用率。"""
    head = html.rindex('<div class="tile overview-card">', 0, html.index("航线利用率"))
    return html[head : html.index('<div class="tile overview-card">', head + 1)]


def _totals_card(html: str) -> str:
    """「此刻」区那张航线合计卡——调度器之后、各星球之前的那一张。"""
    head = html.rindex('<div class="tile overview-card">', 0, html.index("航线合计"))
    return html[head : html.index('<div class="tile overview-card">', head + 1)]


def _slot_classes(html: str) -> list[list[str]]:
    """每一张航线卡片上的格子，按出现顺序。"""
    return [
        re.findall(r'class="slot-(\w+)"', block)
        for block in re.findall(r'<div class="overview-slots".*?</div>', html, re.S)
    ]


#: 「此刻 / 今天收益」那个片段的模板。有一条用例读它的**源码**——
#: 「模板里不许抄槽位和资源名」这件事在渲染结果上看不出来。
_NOW_TEMPLATE = Path(web_package.__file__).parent / "templates" / "_overview_now.html"

#: 控制台的样式表。第四张卡的高度预算压在里面那条 `.overview-basics` 上。
_CONSOLE_CSS = Path(web_package.__file__).parent / "static" / "console.css"

_HAUL_ROW = '<div class="overview-grid overview-grid-four">'


def _today_haul_row(html: str) -> str:
    """「今天收益」那一行四张卡（稀有三样 + 常规资源）。

    下一行（利用率 / 派遣 / 撞保护期 / 读数龄）用的是同一个类名，所以按第二个
    出现位置截断，而不是截到片段末尾。
    """
    start = html.index(_HAUL_ROW, html.index("今天收益"))
    return html[start : html.index(_HAUL_ROW, start + 1)]


def _basics_card(html: str) -> str:
    """那一行里的第四张卡——三样常规资源。"""
    section = _today_haul_row(html)
    head = section.rindex('<div class="tile overview-card">', 0, section.index("常规资源"))
    return section[head:]


def _basic_rows(card: str) -> dict[str, str]:
    """那张卡上的「资源名 → 页面上写的那个数」。"""
    return {
        name: value for name, value in re.findall(r"<span>([^<]+)</span><b[^>]*>([^<]*)</b>", card)
    }


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


# -- 用户口径 2026-08-26：未启用的不显示 -----------------------------------------
#
# ⚠️ 「停用」有两级，页面必须两级都挡：**任务自己那个复选框**
# （`mission_tasks.enabled`）和**出发星球那个勾**（`mission_task_origins.enabled`）。
# 只挡后者是这次改之前的样子——用户把整个军力任务停掉，页面照旧给那几颗星球画着
# 「0 / N 条占用」的空卡片，看上去像是链路活着、只是这会儿没派。


def _bot_task_id(repository: SqlAlchemyRepository) -> int:
    return next(row.id for row in repository.mission_tasks() if row.kind == MissionKind.BOT.value)


def _planet_ids(repository: SqlAlchemyRepository) -> dict[Coordinate, int]:
    """坐标 → `attack_planets` 的行 id。按下标取会随排序规则漂。"""
    return {
        Coordinate(row.galaxy, row.system, row.position): row.id
        for row in repository.attack_planets()
    }


def test_a_disabled_task_draws_no_line_card(
    client: TestClient, repository: SqlAlchemyRepository, planets: None
) -> None:
    """任务停用之后，那几颗星球一张卡都不该剩（用户口径 2026-08-26 第一条）。

    ⚠️ 连合计卡也不许留：合计只加页面真的画出来的那几张，两边不一致的话
    「合计 9 条」和下面一张卡都没有会同时摆在一页上。
    """
    repository.update_mission_task(_bot_task_id(repository), enabled=False)

    html = client.get("/overview").text

    assert "航线 · " not in html
    assert "航线合计" not in html
    assert "没有启用的军力攻击出发星球" in html


def test_the_candidate_pool_still_counts_the_disabled_planets(
    client: TestClient, repository: SqlAlchemyRepository, planets: None
) -> None:
    """⚠️⚠️ **候选池那张卡故意不跟着「未启用不显示」走。**

    用户口径（2026-08-26，逐字）：

        「候选池不用跟着走，我就是根据候选池的情况来调整攻击航路以达到最大化」

    那张卡是**决策用的**，不是「此刻在跑什么」的镜子：要决定「9 系那条航路值不值得
    开」，就得先看得见「开了之后有多少目标落进 30–60 分那一档」。跟着过滤的话，
    停用的星球一从分母里消失，那个问题就再也问不出来了 —— 而那恰恰是用户停用它、
    又回来看这一页的原因。

    ⚠️ 这一条钉的是**两处口径故意不同**。少了它，下一个人看见
    `build_now_view` 里算了两份出发星球，会当成漏改，顺手并成一份 —— 页面不会报错，
    只是那几档数字悄悄变小，而用户正拿它做决定。

    构造：任务整个停用（航线卡片全没了），而池子的往返时长分档照旧算得出来。
    """
    repository.update_mission_task(_bot_task_id(repository), enabled=False)

    html = client.get("/overview").text

    assert "航线合计" not in html, "航线卡片这一侧该跟着停用走"
    assert "没有配着出发星球，算不出往返时长" not in html, "池子不该因为任务停用就算不出分档"


def _seed_score(repository: SqlAlchemyRepository, at: Coordinate, *, hours_ago: float) -> None:
    """给一个坐标写一条军力读数，时刻是 **`NOW` 之前几小时**。

    ⚠️ **必须相对 `NOW`，不是 `datetime.now()`。** 这个文件里页面的时钟是冻结的
    （`clock=lambda: NOW`），而真实当下是别的日子 —— 按真实时间种读数等于种到页面
    眼里的**未来**，于是不管配的有效期多短，它永远算「新鲜」，用例测不出任何东西。
    """
    from evo_helper.domain.records import RankingTarget

    repository.save_ranking_targets(
        [
            RankingTarget(
                coordinate=at,
                military_score=4321.0,
                military_score_at_utc=NOW - timedelta(hours=hours_ago),
                military_score_estimated=False,
                military_rank=None,
            )
        ]
    )


def _galaxy_row(html: str, galaxy: int) -> str:
    """候选池那张表里某个银河那一行的原文。"""
    import re

    match = re.search(rf'<td class="who">{galaxy} 系.*?</tr>', html, re.S)
    assert match, f"候选池表里没有 {galaxy} 系那一行"
    return match.group(0)


def test_the_three_cards_share_the_window_from_the_attack_config(
    client: TestClient, repository: SqlAlchemyRepository, planets: None
) -> None:
    """⚠️⚠️ **三格共用攻击配置里的「有效期」，不是这一页自己定的数。**

    用户口径（2026-08-26）：「统一为读取攻击配置，跟着配置走。我这里要看的就是
    动态数据来让我决策的」。从前「各银河新鲜读数」那格写死 6 小时 —— 一个只有这一页
    认的数。用户把有效期从 3 小时改成 1 小时，页面照旧按 6 小时报「读数很足」，
    而选靶那边早就一个都不认了：**页面说的话和派遣做的事相反**。

    两个不同的值各断言一次：只断言一个的话，写死成那个数照样绿。
    """
    set_score_window(repository, max_age_hours=3.0)
    assert client.get("/overview").text.count("有效期 3.0h 内") == 2

    set_score_window(repository, max_age_hours=1.5)
    assert client.get("/overview").text.count("有效期 1.5h 内") == 2


def test_a_stale_reading_is_left_out_of_every_card(
    client: TestClient, repository: SqlAlchemyRepository, planets: None
) -> None:
    """⚠️⚠️ 读数过期的目标**三格都不算**。

    用户口径（2026-08-26）：「候选池 · 按星系 这里的数据范围也要新鲜度一致」。

    ⚠️ 构造成读数 **4 小时前**：比配的有效期（2 小时）旧、却比从前写死的 6 小时新。
    只有这样才分得出「真读了配置」和「照旧按 6 小时算」—— 两个窗口都覆盖得到的读数，
    两种实现给出的答案一样，测不出任何东西。
    """
    set_score_window(repository, max_age_hours=2.0)
    _seed_score(repository, Coordinate(FAR_GALAXY, 123, 7), hours_ago=4.0)

    html = client.get("/overview").text

    assert f"{FAR_GALAXY} 系" not in html, "过期读数还留在页面上（写死 6 小时的话它会在）"


def test_a_fresh_reading_shows_up_in_the_pool_table(
    client: TestClient, repository: SqlAlchemyRepository, planets: None
) -> None:
    """反过来：窗口内的读数要出现在候选池那张表里，并且带上银河的配置状态。

    这一条和上一条是一对 —— 少了它，「一个都不显示」也能让上一条绿。
    """
    set_score_window(repository, max_age_hours=2.0)
    _seed_score(repository, Coordinate(FAR_GALAXY, 123, 7), hours_ago=0.2)

    row = _galaxy_row(client.get("/overview").text, FAR_GALAXY)

    assert "未配" in row, "没配星球的银河要标出来"


def test_the_pool_table_still_lists_a_disabled_galaxy(
    client: TestClient, repository: SqlAlchemyRepository, planets: None
) -> None:
    """⚠️⚠️ **任务停用的银河照样列在候选池表里。**

    用户口径（2026-08-26）：「候选池不用跟着走，我就是根据候选池的情况来调整攻击
    航路以达到最大化」。要判断那条航路值不值得开，就得先看得见它开了之后有多少目标
    ——跟着「未启用不显示」走的话，那个问题再也问不出来。

    ⚠️ 和航线卡片那一侧**故意不同口径**：那边停用就不画（用户口径第一条），
    这边停用照列。两处不一致是有意的，不是漏改。
    """
    set_score_window(repository, max_age_hours=2.0)
    _seed_score(repository, SECOND, hours_ago=0.2)
    repository.update_mission_task(_bot_task_id(repository), enabled=False)

    html = client.get("/overview").text

    assert "航线 · " not in html, "航线卡片那一侧该跟着停用走"
    assert "停" in _galaxy_row(html, SECOND.galaxy), "候选池表里该标着「停」"


def test_the_galaxy_freshness_card_lists_every_galaxy(
    client: TestClient, repository: SqlAlchemyRepository, planets: None
) -> None:
    """⚠️⚠️ **「各银河新鲜读数」列全部星系，不按配了哪几颗星球筛。**

    用户口径（2026-08-26，逐字）：

        「新鲜读数，我也需要全部星系，不根据我的星球配置来」

    和候选池那张卡同一个道理：这两张都是**决策用的**。这一张回答的是「哪个银河
    扫不到」，而那正是决定下一趟军力榜往哪儿扫的依据 —— 按已配的星球筛掉之后，
    没配星球的银河就永远显示不出「读数不新鲜」，于是永远轮不到被扫。

    `galaxy_freshness()` 本来就不接 `origins`，所以今天它是对的。钉这一条是因为
    **没人守着**：2026-08-26 候选池就是这么被顺手一起过滤掉的（同一次改动里），
    而那一处直到用户说出用途才被发现。

    构造：在**一颗星球都没配的银河**（`FAR_GALAXY`）里放一个新鲜读数，再把任务
    整个停用。航线卡片全没了，而那个银河照旧要出现在这一列里。
    """
    from datetime import UTC, datetime, timedelta

    from evo_helper.domain.records import RankingTarget

    repository.save_ranking_targets(
        [
            RankingTarget(
                coordinate=Coordinate(FAR_GALAXY, 123, 7),
                military_score=4321.0,
                military_score_at_utc=datetime.now(UTC) - timedelta(minutes=5),
                military_score_estimated=False,
                military_rank=None,
            )
        ]
    )
    repository.update_mission_task(_bot_task_id(repository), enabled=False)

    html = client.get("/overview").text

    assert "航线合计" not in html, "航线卡片这一侧该跟着停用走"
    assert f"{FAR_GALAXY} 系" in html, "没配星球的银河也必须列出来"


def test_a_disabled_origin_draws_no_line_card_either(
    client: TestClient, repository: SqlAlchemyRepository, planets: None
) -> None:
    """星球那个勾去掉，那一颗也不该再画（两级里的第二级）。

    这一条和上一条分开写：两级停用各有各的入口，合成一条用例的话，其中一道闸
    漏掉时另一条还照样绿。
    """
    ids = _planet_ids(repository)
    repository.replace_mission_task_origins(
        _bot_task_id(repository),
        [(ids[HOME], HOME_LINES, True), (ids[SECOND], SECOND_LINES, False)],
    )

    html = client.get("/overview").text

    assert str(SECOND) not in html
    assert [len(cells) for cells in _slot_classes(html)] == [HOME_LINES]
    # 合计跟着只剩一颗——第一条与第二条同口径，否则合计和下面几张卡加起来对不上。
    assert "共 1 颗星球" in _totals_card(html)
    assert f"/ {HOME_LINES} 条占用" in _totals_card(html)


def test_the_empty_line_card_does_not_claim_nothing_is_configured(
    client: TestClient, repository: SqlAlchemyRepository, planets: None
) -> None:
    """⚠️ 空态那句话在「配着、但停用了」的情形下必须仍然是真的。

    原先写的是「没有配着出发星球的军力攻击任务」——任务配得好好的、两颗星球
    也都在，只是勾去掉了，那句话就是假的。

    ⚠️ **候选池那张卡不在此列。** 它按所有配着的星球算往返时长
    （用户口径 2026-08-26：「候选池不用跟着走，我就是根据候选池的情况来调整攻击
    航路以达到最大化」），所以任务停用时它照旧算得出分档、根本走不到空态。
    这里连带断言它**没有**空态，是为了钉住这个差别——两张卡的空态措辞一度被
    一起改掉过。
    """
    repository.update_mission_task(_bot_task_id(repository), enabled=False)

    html = client.get("/overview").text

    assert "没有配着出发星球的军力攻击任务" not in html
    assert "没有启用的军力攻击出发星球" in html
    assert "算不出往返时长" not in html, "池子不该因为任务停用就走空态"


# -- 用户口径 2026-08-26：总航线卡片 ---------------------------------------------


def test_the_totals_card_sits_between_the_scheduler_and_the_planets(
    client: TestClient, planets: None
) -> None:
    """合计是总览：调度器那张卡之后、各星球卡片之前。"""
    html = client.get("/overview").text
    section = html[html.index("此刻") :]

    assert section.index("调度器") < section.index("航线合计") < section.index("航线 · ")


def test_the_totals_add_up_the_cards_the_page_draws(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """配置数、占用数各自相加，「最早空出」取各星球里**最早**的那一个。

    两颗星球的返航时刻故意差得远：取成最晚的那个（或者取成第一颗的）都能过一个
    只种一颗星球的用例，而页面上那句话会指向一个还早得很的时刻——读的人据此
    以为「这会儿派不出去」。
    """
    for _ in range(2):
        _dispatch(
            factory,
            run_id,
            origin=HOME,
            dispatched_at_utc=NOW - timedelta(minutes=5),
            line_free_at_utc=NOW + timedelta(hours=2),
        )
    _dispatch(
        factory,
        run_id,
        origin=SECOND,
        dispatched_at_utc=NOW - timedelta(minutes=5),
        line_free_at_utc=NOW + timedelta(minutes=30),
    )

    card = _totals_card(client.get("/overview").text)

    assert "共 2 颗星球" in card
    assert f'3<span class="overview-unit">/ {HOME_LINES + SECOND_LINES} 条占用' in card
    # UTC+8：10:30Z 就是 18:30（全站同一口径）。
    assert "2026-08-19 18:30:00" in card


def test_the_totals_card_draws_no_slot_grid(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ 合计卡**不画格子**。

    格子说的是「这颗星球上的这几条航线」；把两颗星球的格子并成一排等于说它们能
    互相顶替，而一颗星球上空着的航线派不了另一颗星球的舰队。
    """
    _dispatch(factory, run_id, origin=HOME, dispatched_at_utc=NOW - timedelta(minutes=5))

    html = client.get("/overview").text

    assert 'class="overview-slots"' not in _totals_card(html)
    # 格子仍然只有两排：两颗星球各一排。
    assert [len(cells) for cells in _slot_classes(html)] == [HOME_LINES, SECOND_LINES]


def test_the_totals_say_full_only_when_every_planet_is_full(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ 「全满」按**每颗都满**判，不按「占用合计 ≥ 配置合计」。

    先把主星占到 7 条（配的只有 5 条）、另一颗占 2 条（配 4 条）：合计正好
    9 / 9，按合计判就会写「全满」——而那颗星球上还空着两条，这会儿明明派得出去。
    """
    for _ in range(7):
        _dispatch(factory, run_id, origin=HOME, dispatched_at_utc=NOW - timedelta(minutes=5))
    for _ in range(2):
        _dispatch(factory, run_id, origin=SECOND, dispatched_at_utc=NOW - timedelta(minutes=5))

    card = _totals_card(client.get("/overview").text)

    assert f'9<span class="overview-unit">/ {HOME_LINES + SECOND_LINES} 条占用' in card
    assert "全满" not in card

    for _ in range(2):
        _dispatch(factory, run_id, origin=SECOND, dispatched_at_utc=NOW - timedelta(minutes=5))

    assert "全满" in _totals_card(client.get("/overview").text)


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
        "挂机",
        "利用率",
    ]
    # 并排，不是分在两张表里。
    assert columns.index("读回战报") < columns.index("合金碎片")
    assert columns.index("回收率") < columns.index("合金碎片")
    # ⚠️ 「挂机」必须紧挨着「利用率」：分母换成「周期总时长 × 线数」之后，
    # 「为什么低」这个问题的答案就在挂机那一列里。分开摆等于把一对数拆散。
    assert columns.index("挂机") == columns.index("利用率") - 1


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
    card = html[html.index("未读战报") : html.index("候选池 · 按星系")]
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


# -- 第四张卡：三样常规资源 ------------------------------------------------------


def test_the_fourth_card_holds_the_three_basic_resources_together(
    client: TestClient, planets: None
) -> None:
    """用户口径（2026-08-19）：「只显示 金属/晶体/气体，**整合进一个标签**」。

    所以是**一张**卡里三个数，不是拆成三张——「今天收益」那一行仍旧只有四张卡。
    原先那张「其余九种」连同它那个加总数一起没了：把千万级的金属和个位数的
    银河石能量加在一起，量纲都不一样，那个数不回答任何问题。
    """
    html = client.get("/overview").text
    section = _today_haul_row(html)

    assert section.count('<div class="tile overview-card">') == 4
    assert "其余九种" not in html
    assert "<details" not in html

    card = _basics_card(html)
    positions = [card.index(slot_label(slot)) for slot in BASIC_SLOTS]
    assert positions == sorted(positions), "三样的次序和 `BASIC_SLOTS` 对不上"


def test_the_basic_card_writes_a_zero_for_a_slot_the_haul_does_not_mention(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ 缺项的口径照 PR #217，**别另立一套**。

    入库是全有或全无：12 格但凡一格读不出来，那份战报一行都不写。所以
    「有若干行、偏偏缺 slot 1」只有一种解释——那一格读到了，就是 0。
    这里种一份只有金属与气体的战报，晶体那一行必须是 `0` 而不是「—」。
    """
    _report(factory, reported_at_utc=NOW - timedelta(hours=1), resources=((0, 900), (2, 700)))

    card = _basics_card(client.get("/overview").text)
    rows = _basic_rows(card)

    assert rows == {slot_label(0): "900", slot_label(1): "0", slot_label(2): "700"}


def test_the_basic_card_writes_a_dash_when_there_is_no_haul_at_all(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ 反过来，**一条收获记录都没有时不许写 0**（同 PR #217）。

    那种情况库里分不开「12 格全 0」和「这些战报根本没读过资源」（存量战报全是
    后者）。写 0 就是拿「不知道」冒充 0，而这一页的读者正是拿它判断今天收成。
    这里种一份没有任何资源行的战报——三样全写「—」。
    """
    _report(factory, reported_at_utc=NOW - timedelta(hours=1))

    card = _basics_card(client.get("/overview").text)
    rows = _basic_rows(card)

    assert rows == {slot_label(slot): "—" for slot in BASIC_SLOTS}


def test_the_basic_amounts_keep_the_approximate_word_and_the_error_range(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ **近似值一律带「约」，误差范围放在 `title` 上。**

    画面上超过一千的值是缩写显示的（`928K`），真值取不回来了。用户接受这个精度，
    但接受误差不等于可以把近似值渲染得像精确读数（先例 `military_score_estimated`）。
    误差按当初显示了几位有效数字算，所以它不能只写一个「约」了事。
    """
    for amount in (900_000, 28_000):
        _report(
            factory,
            reported_at_utc=NOW - timedelta(hours=1),
            resources=((0, amount),),
            approximate=True,
            uncertainty=500,
        )

    card = _basics_card(client.get("/overview").text)

    assert _basic_rows(card)[slot_label(0)] == "约 928,000"
    # 两份各 ±500，合计最坏就是 ±1,000——误差是累加的，不是取最大。
    assert 'title="画面上是缩写显示的，误差不超过 ±1,000"' in card


def test_the_basic_card_stays_within_the_height_of_the_rare_cards(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ **这一区是四张等高卡并排，行高由最高的那张决定。**

    用户这次点名「保持高度」。所以第四张卡只能是「标题 + 三行紧凑行」：
    多一行说明、换成 26px 的大字号 `.value`、或者添一张图标，整行都会跟着长高。
    """
    _report(factory, reported_at_utc=NOW - timedelta(hours=1), resources=((0, 900),))

    card = _basics_card(client.get("/overview").text)

    assert card.count("overview-kv") == 3
    assert 'class="value' not in card
    assert "<img" not in card
    assert "overview-note" not in card
    # 三行按默认的 `.overview-kv` 行距排是 66px，比稀有卡那 61.5px 高——
    # 补偿的那条 CSS 一旦被顺手删掉，整行就长高几个像素。⚠️ 这里只守得住
    # 「那条规则还在」，**真正的像素高度没有自动化手段量**（跑不起浏览器）。
    assert ".overview-kv.overview-basics" in _CONSOLE_CSS.read_text(encoding="utf-8")


def test_neither_the_slots_nor_the_resource_names_are_copied_into_the_template() -> None:
    """⚠️ 槽位从 `BASIC_SLOTS` 来、名字由 `SLOT_LABELS` 翻译，**模板里一份都不许抄**。

    那张对照表的顺序与游戏「太空舱」页并不一致（银河素与合金碎片对调）。抄一份
    出去，日后对不上的症状是「数字全对、只是安在了别的资源名下」——页面上一点
    异样都没有，谁也不会发现。
    """
    source = _NOW_TEMPLATE.read_text(encoding="utf-8")

    for slot in BASIC_SLOTS:
        assert slot_label(slot) not in source
    assert "0, 1, 2" not in source


def test_the_card_follows_the_constant_instead_of_a_copy_of_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, planets: None
) -> None:
    """⚠️ 上一条只管模板；**这一条管页面那一侧有没有抄第二份**。

    把常量换成另外两格，卡片必须跟着换。查询里写死 `(0, 1, 2)` 的话，渲染结果
    和现在一模一样——那种「抄一份」在任何断言里都看不出来，除非把常量动一动。
    """
    monkeypatch.setattr(overview_routes, "BASIC_SLOTS", (3, 4))

    card = _basics_card(client.get("/overview").text)

    assert set(_basic_rows(card)) == {slot_label(3), slot_label(4)}


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
    """没有军力攻击任务时，航线那一块说「没有启用的出发星球」，而不是 500。"""
    with factory() as session:
        for row in session.query(MissionTaskRow).all():
            session.delete(row)
        session.commit()

    response = client.get("/overview")

    assert response.status_code == 200
    assert "没有启用的军力攻击出发星球" in response.text


# -- 利用率的分母：周期总时长 × 航线数（用户口径 2026-08-20） ---------------------
#
# ⚠️ 这一节整体反转了 08-17 那条「任务实际运行时间 × 航线数」。旧口径下「关了
# 一晚上」和「开着一整天却一发没派」在页面上长得一样（都接近 100%）；新口径把
# 没开工的那段显示成损失，而「到底开没开工」由「挂机」那一列单独回答。


def test_the_utilisation_denominator_is_the_whole_period_not_the_run_time(
    client: TestClient,
    factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
    run_id: UUID,
    planets: None,
) -> None:
    """今天已过 10 小时、配着 5 条线、占了 1 条线 1 小时 ⇒ 1 / (10 × 5) = 2%。

    ⚠️ 换回旧分母（那一轮只跑了 1 小时 × 5 条 = 5 航线小时）会算出 20%，
    这一条立刻转红。
    """
    _run(
        repository,
        started_at_utc=NOW - timedelta(hours=2),
        ended_at_utc=NOW - timedelta(hours=1),
        configured_lines=5,
    )
    _dispatch(
        factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(hours=2),
        line_free_at_utc=NOW - timedelta(hours=1),
    )

    html = client.get("/overview").text

    assert _period_cells(html, "08-19 今天")[-1] == "2%"


def test_a_period_whose_line_count_was_recorded_is_not_marked_as_a_bound(
    client: TestClient,
    factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
    run_id: UUID,
    planets: None,
) -> None:
    """有真值的那一行照实写百分比，不加「≤」。"""
    _run(
        repository,
        started_at_utc=NOW - timedelta(hours=2),
        ended_at_utc=NOW - timedelta(hours=1),
        configured_lines=5,
    )
    _dispatch(
        factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(hours=2),
        line_free_at_utc=NOW - timedelta(hours=1),
    )

    cells = _period_cells(client.get("/overview").text, "08-19 今天")

    assert "≤" not in cells[-1]


def test_a_period_without_a_recorded_line_count_falls_back_to_the_peak_and_says_so(
    client: TestClient,
    factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
    run_id: UUID,
    planets: None,
) -> None:
    """⚠️ **历史天：线数用「当天最大并发在飞数」当下界，页面必须标出来。**

    两发并行各占 1 小时 ⇒ 最大并发 2 ⇒ 分母 10 × 2 = 20 航线小时 ⇒ 2 / 20 = 10%。
    方向是**线数偏小 ⇒ 分母偏小 ⇒ 利用率偏高**，所以写「≤ 10%」：真实值不高于它。

    ⚠️ 换成「用此刻配着的 9 条」会算出 2%，换成「填 0」会变成「—」，
    两种都会让这一条转红。
    """
    for _ in range(2):
        _dispatch(
            factory,
            run_id,
            dispatched_at_utc=NOW - timedelta(hours=2),
            line_free_at_utc=NOW - timedelta(hours=1),
        )

    html = client.get("/overview").text
    cells = _period_cells(html, "08-19 今天")

    assert cells[-1] == "≤ 10%"
    # 方向必须写在页面上，不只是写在注释里。
    assert "利用率因此偏高" in html


def test_a_period_estimated_from_the_peak_never_exceeds_one_hundred_percent(
    client: TestClient,
    factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
    run_id: UUID,
    planets: None,
) -> None:
    """⚠️ **方案 C 的自检性质**：用最大并发当线数时，利用率在构造上 ≤ 100%。

    这里派的全是「时长未知」那一档（航线钟为 NULL，按 hold 兜底占 90 分钟），
    起点各不相同、互相重叠 —— 最容易把并发算错的形状。算出 >100% 就是实现有 bug
    （最典型的是把重叠区间合并了，那会让最大并发恒等于 1）。
    """
    for minutes in (0, 20, 40, 60, 80, 100):
        _dispatch(
            factory, run_id, dispatched_at_utc=NOW - timedelta(hours=5) + timedelta(minutes=minutes)
        )

    percent = _period_cells(client.get("/overview").text, "08-19 今天")[-1]

    assert percent.startswith("≤ ")
    assert int(percent.removeprefix("≤ ").removesuffix("%")) <= 100


def test_the_today_card_spells_out_the_new_denominator(
    client: TestClient,
    factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
    run_id: UUID,
    planets: None,
) -> None:
    """「今天收益」那张卡上的小字要跟着改口径：可用 = 已过时长 × 线数。"""
    _run(
        repository,
        started_at_utc=NOW - timedelta(hours=2),
        ended_at_utc=NOW - timedelta(hours=1),
        configured_lines=5,
    )
    _dispatch(
        factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(hours=2),
        line_free_at_utc=NOW - timedelta(hours=1),
    )

    card = _utilisation_card(client.get("/overview").text)

    assert "占用 1.0 / 可用 50.0 航线小时" in card
    assert "今天已过时长 × 5 条" in card


# -- 挂机运行时长 ---------------------------------------------------------------


def test_a_period_without_any_heartbeat_says_no_data_instead_of_zero(
    client: TestClient, factory: sessionmaker[Session], run_id: UUID, planets: None
) -> None:
    """⚠️ **心跳之前的那些天必须写「—」，不许写 0。**

    写 0 等于说「那天没开机」，而事实是「那天没人在记」——心跳是 2026-08-20 才加的，
    历史补不回来（用户口径 2026-08-20）。
    """
    _dispatch(factory, run_id, dispatched_at_utc=NOW - timedelta(hours=2))

    html = client.get("/overview").text

    # 挂机那一格在利用率左边。
    assert _period_cells(html, "08-19 今天")[-2] == "—"
    assert "—" in _utilisation_card(html)


def test_the_uptime_column_reports_the_hours_the_scheduler_was_up(
    client: TestClient,
    factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
    run_id: UUID,
    planets: None,
) -> None:
    """今天 00:30 起、04:00 最后一拍 ⇒ 3.5 小时。

    ⚠️ 这个数**不是**轮次时长之和：实测 2026-08-20 那 41 分钟调度器开着却一轮都
    没起（扫描间隔挡住 RANKING、`waiting_for_a_line` 压住 BOT），拿轮次覆盖冒充
    挂机时长会把它误报成关机。所以这一条**一轮 `mission_runs` 都不造**。
    """
    day = datetime(2026, 8, 19, tzinfo=UTC)
    # 前一天也落过拍：这样「今天」整段都在观测范围里，那个数才是确数而不是下界
    # （下界那一档另有用例，见 test_a_day_before_the_first_beat_still_says_no_data）。
    _uptime(repository, start=day - timedelta(hours=6), last_beat=day - timedelta(hours=5))
    _uptime(repository, start=day + timedelta(minutes=30), last_beat=day + timedelta(hours=4))
    _dispatch(factory, run_id, dispatched_at_utc=NOW - timedelta(hours=2))

    html = client.get("/overview").text

    assert _period_cells(html, "08-19 今天")[-2] == "3.5h"
    assert "3.5 小时" in _utilisation_card(html)


def test_a_killed_process_does_not_keep_the_uptime_growing(
    client: TestClient,
    factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
    run_id: UUID,
    planets: None,
) -> None:
    """⚠️ **进程被杀的情形。** 崩溃时不会有人写「已停止」。

    01:00 起、02:00 最后一拍，之后进程被 kill；现在是 10:00。挂机时长必须是
    1 小时，不是 9 小时。换成「起了就一直算到现在」的写法，这一条立刻转红。
    """
    day = datetime(2026, 8, 19, tzinfo=UTC)
    _uptime(repository, start=day - timedelta(hours=6), last_beat=day - timedelta(hours=5))
    _uptime(repository, start=day + timedelta(hours=1), last_beat=day + timedelta(hours=2))
    _dispatch(factory, run_id, dispatched_at_utc=NOW - timedelta(hours=2))

    assert _period_cells(client.get("/overview").text, "08-19 今天")[-2] == "1.0h"


def test_a_day_before_the_first_beat_still_says_no_data(
    client: TestClient,
    factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
    run_id: UUID,
    planets: None,
) -> None:
    """⚠️ 库里已经有心跳了，但**更早的那些天**照样是「—」。

    这一条和「有观测、那天真的没开机 = 0」必须分得开，否则「没开机」和
    「没记录」在页面上长得一样，而这个指标存在的全部意义就是分开它们。

    顺带钉住**心跳上线那一天**：第一拍落在 01:00，00:00–01:00 那一小时没人在记，
    所以那天的挂机时长是**下界**，页面写「≥」。
    """
    day = datetime(2026, 8, 19, tzinfo=UTC)
    _uptime(repository, start=day + timedelta(hours=1), last_beat=day + timedelta(hours=2))
    _dispatch(factory, run_id, dispatched_at_utc=datetime(2026, 8, 18, 12, tzinfo=UTC))

    html = client.get("/overview").text

    assert _period_cells(html, "08-18")[-2] == "—"
    assert _period_cells(html, "08-19 今天")[-2] == "≥ 1.0h"
