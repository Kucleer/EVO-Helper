"""「数据概览」页上的「按星球效率」那一段。

断言全部打在 `/overview/origin-efficiency` 返回的 HTML 上：这一段的价值就在那几个
数摆对了没有、以及那几条限制有没有被说出来。

**这里不真的 Popen 任何 runner**：`launch` 一律注入假的、后台 tick 推到一小时
一次。真起一个会去点用户的真实鼠标、派真实舰队。

⚠️ **坐标全是编造的**（公开仓库，真实出发坐标不进夹具）；**时刻全部注入**
（`clock=lambda: NOW`），一处都不读真实时钟。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
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
from evo_helper.domain.origin_efficiency import LOW_RECOVERY_THRESHOLD
from evo_helper.domain.overview import BASIC_SLOTS, RARE_SLOTS
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import origin_efficiency as storage_origin_efficiency
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.models import (
    AttackDispatchRow,
    AttackIntentRow,
    BattleReportResourceRow,
    BattleReportRow,
    RunInstance,
    ScanPlan,
)
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web import app as web_package
from evo_helper.web.app import create_persistent_app
from support.database import scratch_database_url

NOW = datetime(2026, 8, 20, 21, 0, tzinfo=UTC)
DAY = datetime(2026, 8, 20, tzinfo=UTC)
TOKEN = "test-token"

#: 三颗编造的出发星球。位置号在 5 以上（1–4 号位是海盗位，选靶那侧另有判据挡它们）。
#: `EARLY` 天亮就开工、`LATE` 快收工才开工、`STOPPED` 当天被停用过。
EARLY = Coordinate(1, 111, 6)
LATE = Coordinate(2, 222, 7)
STOPPED = Coordinate(3, 333, 8)
#: 当前配置里压根没有它，但它当天真派出过。
REMOVED = Coordinate(4, 444, 9)

EARLY_LINES = 2
LATE_LINES = 2
STOPPED_LINES = 3


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
    def __call__(self, kind: MissionKind, command: object, log_path: Path) -> _FakeProcess:
        return _FakeProcess(pid=9001)


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(scratch_database_url(tmp_path, "origins.db"))
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def run_id(factory: sessionmaker[Session]) -> UUID:
    with factory() as session:
        plan = ScanPlan(name="origins-fixture", created_at_utc=NOW)
        session.add(plan)
        session.flush()
        run = RunInstance(
            plan_id=plan.id,
            idempotency_key="origins-fixture-001",
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


def _configure(repository: SqlAlchemyRepository) -> None:
    """三颗星球，第三颗是**停用**状态、而且它配的航线数和前两颗不同。

    第三颗配成同一个数的话，「每颗星球各按自己的航线数算」这条写死一个常量也能过。
    """
    task = next(row for row in repository.mission_tasks() if row.kind == MissionKind.BOT.value)
    repository.update_mission_task(task.id, params_json='{"by_military": true}')
    planets = [repository.create_attack_planet(item) for item in (EARLY, LATE, STOPPED)]
    repository.replace_mission_task_origins(
        task.id,
        [
            (planets[0].id, EARLY_LINES, True),
            (planets[1].id, LATE_LINES, True),
            (planets[2].id, STOPPED_LINES, False),
        ],
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
    origin: Coordinate,
    dispatched_at_utc: datetime,
    line_free_at_utc: datetime | None = None,
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
                target_galaxy=5,
                target_system=140,
                target_position=9,
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
            )
        )
        session.commit()
    return dispatch_id


def _report(
    factory: sessionmaker[Session],
    *,
    reported_at_utc: datetime,
    dispatch_id: UUID,
    resources: tuple[tuple[int, int], ...] = (),
) -> None:
    report_id = uuid4()
    with factory() as session:
        session.add(
            BattleReportRow(
                id=report_id,
                reported_at_utc=reported_at_utc,
                attacker_origin_galaxy=EARLY.galaxy,
                attacker_origin_system=EARLY.system,
                attacker_origin_position=EARLY.position,
                defender_target_galaxy=5,
                defender_target_system=140,
                defender_target_position=9,
                dispatch_id=dispatch_id,
            )
        )
        session.flush()
        for slot, amount in resources:
            session.add(
                BattleReportResourceRow(id=uuid4(), report_id=report_id, slot=slot, amount=amount)
            )
        session.commit()


def _run(repository: SqlAlchemyRepository, *, configured_lines: int | None) -> None:
    """一轮子进程记录。`configured_lines=None` 就是 2026-08-20 之前那些行的形状。"""
    run = repository.begin_mission_run(
        MissionKind.BOT,
        task_id=None,
        command=["python"],
        pid=None,
        started_at_utc=DAY + timedelta(minutes=1),
        log_path="var/logs/mission-bot.log",
        configured_lines=configured_lines,
    )
    repository.finish_mission_run(run, ended_at_utc=NOW, exit_code=0, stopped_by="SELF")


def _earn(
    factory: sessionmaker[Session],
    run_id: UUID,
    *,
    origin: Coordinate,
    at: datetime,
    rare: int,
    read_back: bool = True,
) -> None:
    """从 `origin` 派一发，并（可选）读回一份带 `rare` 收获的战报。"""
    dispatch = _dispatch(
        factory,
        run_id,
        origin=origin,
        dispatched_at_utc=at,
        line_free_at_utc=at + timedelta(hours=1),
    )
    if read_back:
        _report(
            factory,
            reported_at_utc=at + timedelta(minutes=30),
            dispatch_id=dispatch,
            resources=((RARE_SLOTS[0], rare),),
        )


def _fragment(client: TestClient, *, day: str | None = None) -> str:
    url = "/overview/origin-efficiency"
    if day is not None:
        url += f"?origin_day={day}"
    response = client.get(url)
    assert response.status_code == 200
    return response.text


def _row_order(html: str) -> list[str]:
    """表里各行的出发星球，按页面上的先后。"""
    return re.findall(r'<th scope="row">([\d:]+)', html)


def _headers(html: str) -> list[str]:
    """表头那一排列名。

    ⚠️ **必须按表头判，不能只搜文字**：页脚那几段说明里也写着「回收率」「在岗」
    这些词，光搜文字的话，把那一列整个删掉用例照样是绿的（这一条是变异测试第
    6a 组抓出来的）。
    """
    return [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r'<th scope="col"[^>]*>(.*?)</th>', html, re.S)
    ]


def _cells(html: str, origin: Coordinate) -> list[str]:
    """某一行的各个格子（不含最左边的星球名）。"""
    match = re.search(rf'<th scope="row">{re.escape(str(origin))}.*?</th>(.*?)</tr>', html, re.S)
    assert match is not None, f"表里没有 {origin} 这一行"
    return [
        re.sub(r"<[^>]+>", "", cell).strip()
        for cell in re.findall(r"<td[^>]*>(.*?)</td>", match.group(1), re.S)
    ]


_TEMPLATE = Path(web_package.__file__).parent / "templates" / "_overview_origins.html"


# -- 排序：名次以「每线小时」为准 -----------------------------------------------


def test_the_planet_with_the_best_per_line_hour_comes_first(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """⚠️ **排序键是「每线小时」，不是「每线」。**

    两颗星球的收成和线数完全一样（各 4,000 / 2 条），只差在岗时长：`EARLY` 从
    零点就开工（21 小时），`LATE` 一小时前才开工。按「每线」两行相等，按
    「每线小时」`LATE` 领先 21 倍——**页面上 `LATE` 必须在最前**。

    排序反了、或者退回按「每线」排，这一条就红。
    """
    _configure(repository)
    # 记下当天的账号航线总数，好让两行的线数都是真值（否则两行各退一个下界，
    # 「每线」就不再相等，这条用例也就分不开两个排序键了）。
    _run(repository, configured_lines=EARLY_LINES + LATE_LINES)
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(minutes=5), rare=4_000)
    _earn(factory, run_id, origin=LATE, at=NOW - timedelta(hours=1), rare=4_000)

    html = _fragment(client)

    assert _row_order(html)[0] == str(LATE)
    early = _cells(html, EARLY)
    late = _cells(html, LATE)
    # 倒数第二格是「每线」，最后一格是「每线小时」。两行的「每线」一样。
    assert early[-2] == late[-2] == "2,000"
    assert late[-1] != early[-1]


# -- 分子：只算稀有三样 ---------------------------------------------------------


def test_the_basic_three_never_reach_the_numerator(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """⚠️ 金属/晶体/气体由我方货舱容量决定、与目标无关，掺进来会让「预设大的
    星球」无脑领先。这一发另收了三样基础各 720,000，页面上一格都不许多。
    """
    _configure(repository)
    dispatch = _dispatch(factory, run_id, origin=EARLY, dispatched_at_utc=DAY + timedelta(hours=1))
    _report(
        factory,
        reported_at_utc=DAY + timedelta(hours=2),
        dispatch_id=dispatch,
        resources=((RARE_SLOTS[0], 1_000),) + tuple((slot, 720_000) for slot in BASIC_SLOTS),
    )

    html = _fragment(client)

    assert "1,000" in _cells(html, EARLY)
    assert "720,000" not in html
    assert "2,161,000" not in html


def test_the_rare_labels_come_from_the_slot_table(
    client: TestClient, repository: SqlAlchemyRepository
) -> None:
    """三样的名字由 `slot_label` 翻译，页面上写的就是那三个。"""
    _configure(repository)

    html = _fragment(client)

    for slot in RARE_SLOTS:
        assert slot_label(slot) in html


def test_neither_the_query_nor_the_template_carries_a_second_copy_of_the_slots(
    client: TestClient,
) -> None:
    """⚠️ **槽位号与资源名只许有一份。**

    `SLOT_LABELS` 的顺序与游戏「太空舱」页不一致（那张表上写着是哪两格对调），
    抄第二份出去，对不上的症状是「数字全对、只是安在了别的资源名下」——
    页面上一点异样都没有。这件事在渲染结果上看不出来，所以读源码。
    """
    sql = Path(storage_origin_efficiency.__file__).read_text(encoding="utf-8")
    template = _TEMPLATE.read_text(encoding="utf-8")
    for text in (sql, template):
        assert "(5, 8, 9)" not in text
        for slot in RARE_SLOTS + BASIC_SLOTS:
            assert slot_label(slot) not in text
    # 反过来：查询必须真的引用那一份常量，而模板里的名字必须是服务端翻译好送来的。
    assert "RARE_SLOTS" in sql
    assert "rare_labels" in template
    assert "basic_labels" in template


# -- 回收率与「不可信」 ---------------------------------------------------------


def test_the_recovery_column_sits_next_to_the_efficiency_columns(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """⚠️ **回收率必须并列在表里，不能做成小字。**

    分子只数已读回的战报，所以回收率就是分子的覆盖率；少了这一列，用户看历史
    某一天会得出「那天效率崩了」这个错结论（实测 08-17 是 39 发读回 13 发）。
    """
    _configure(repository)
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(hours=1), rare=1_000)
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(hours=2), rare=0, read_back=False)

    html = _fragment(client)

    # 表头这一整排都钉住：少一列、或者调了次序，这一条就红。
    assert _headers(html) == [
        "出发星球",
        "派出",
        "读回战报",
        "回收率",
        "稀有三样",
        "航线",
        "在岗",
        "每线",
        "每线小时",
    ]
    # 2 发派出、1 发读回 = 50%。
    assert "50%" in _cells(html, EARLY)


def test_a_low_recovery_row_is_marked_untrustworthy(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """⚠️ 回收率低到能让相邻两行换位时，效率数必须显式标成**不可信**。

    这里 4 发只读回 1 发（25%），远低于阈值。
    """
    _configure(repository)
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(hours=1), rare=1_000)
    for hour in (2, 3, 4):
        _earn(
            factory,
            run_id,
            origin=EARLY,
            at=DAY + timedelta(hours=hour),
            rare=0,
            read_back=False,
        )

    html = _fragment(client)

    # 页脚那段说明里也写着「不可信」三个字，所以认的是那一行上的标记本身。
    assert "data-untrustworthy" in html
    assert f"{LOW_RECOVERY_THRESHOLD * 100:.0f}%" in html


def test_a_healthy_recovery_row_is_not_marked_untrustworthy(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """反面：全部读回来的那一天不该被打上「不可信」，否则那个标记就没有信息量。"""
    _configure(repository)
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(hours=1), rare=1_000)

    html = _fragment(client)

    assert "data-untrustworthy" not in html
    # 页脚那句解释照旧在——「这一行被标了」和「页面解释了这个标记」是两件事。
    assert "不可信" in html


# -- 行集：停用的、被删的、配了没派的 -------------------------------------------


def test_a_disabled_origin_still_shows_the_work_it_did(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """⚠️ **只看 `enabled` 会漏掉它当天真打出去的活。**

    实测 2026-08-20 有一颗星球中途被自动停用过。那一行必须在，而且要标出
    「已停用」——否则「被停用了」和「这颗星球被删了」在页面上长得一模一样。
    """
    _configure(repository)
    _earn(factory, run_id, origin=STOPPED, at=DAY + timedelta(hours=1), rare=24_020)

    html = _fragment(client)

    assert str(STOPPED) in _row_order(html)
    assert "已停用" in html
    assert "24,020" in _cells(html, STOPPED)


def test_an_origin_that_left_the_config_still_shows_the_work_it_did(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """配置里已经没有它了，但它那天真打出去过——照样列出来，并标「已移除」。"""
    _configure(repository)
    _earn(factory, run_id, origin=REMOVED, at=DAY + timedelta(hours=1), rare=777)

    html = _fragment(client)

    assert str(REMOVED) in _row_order(html)
    assert "已移除" in html


def test_a_configured_origin_that_never_dispatched_still_gets_a_row(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """配了却一发没派也要有一行——那是这一页最该喊出来的一种浪费。"""
    _configure(repository)
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(hours=1), rare=1_000)

    html = _fragment(client)

    assert str(LATE) in _row_order(html)
    # 一发没派 ⇒ 回收率是「—」，不是 0%。
    assert "—" in _cells(html, LATE)


# -- 线数：真值 vs 下界 ---------------------------------------------------------


def test_a_recorded_account_total_that_still_matches_gives_an_exact_line_count(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """那一天记下的账号总数（4）和此刻启用的总数（2 + 2）一致 ⇒ 线数当真值用。

    真值那一档不带「≤」。
    """
    _configure(repository)
    _run(repository, configured_lines=EARLY_LINES + LATE_LINES)
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(hours=1), rare=4_000)

    cells = _cells(_fragment(client), EARLY)

    assert f"{EARLY_LINES} 条" in cells
    assert cells[-2] == "2,000"


def test_a_day_without_a_recorded_total_falls_back_to_a_marked_lower_bound(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """⚠️ `mission_runs.configured_lines` 是 2026-08-20 才加的列，更早的行为 NULL。

    NULL 的那些天退到「当天最大并发在飞数」这个**下界**，页面上带「≥ / ≤」：
    分母偏小 ⇒ 效率是上界。不标的话，那个数会假装自己是真值。
    """
    _configure(repository)
    _run(repository, configured_lines=None)
    # 两发首尾重叠 ⇒ 最大并发 2 条。
    _dispatch(
        factory,
        run_id,
        origin=EARLY,
        dispatched_at_utc=DAY + timedelta(hours=1),
        line_free_at_utc=DAY + timedelta(hours=3),
    )
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(hours=2), rare=4_000)

    cells = _cells(_fragment(client), EARLY)

    assert "≥ 2 条" in cells
    assert cells[-2].startswith("≤ ")


# -- 日期与限制说明 -------------------------------------------------------------


def test_the_day_is_cut_at_utc_midnight(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """⚠️ 统计按 **UTC+0** 切天。

    这一发派于 UTC 08-20 21:30（UTC+8 已是 08-21 早上 5:30），它属于 08-20。
    按本机 / 会话时区切天，它会整个挪到次日去。
    """
    _configure(repository)
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(hours=20, minutes=30), rare=5_555)

    assert "5,555" in _cells(_fragment(client), EARLY)
    assert "5,555" not in _fragment(client, day="2026-08-19")


def test_a_past_day_can_be_selected_and_a_bad_date_falls_back_to_today(
    client: TestClient,
    repository: SqlAlchemyRepository,
    factory: sessionmaker[Session],
    run_id: UUID,
) -> None:
    """日期选择器认不出来时静默回落到今天，**不 422**。"""
    _configure(repository)
    _earn(factory, run_id, origin=EARLY, at=DAY - timedelta(hours=3), rare=1_234)
    _earn(factory, run_id, origin=EARLY, at=DAY + timedelta(hours=3), rare=8_888)

    assert "1,234" in _fragment(client, day="2026-08-19")
    assert "8,888" in _fragment(client, day="not-a-date")
    assert "8,888" in _fragment(client, day="2026-08-17")


def test_the_page_spells_out_the_three_limits_it_lives_with(
    client: TestClient, repository: SqlAlchemyRepository
) -> None:
    """三条限制各自对应一个会让人得出错结论的读法，一条都不许删。

    1. 按**派出日**归属 ⇒ 当天的数永远不完整。
    2. **在岗时长**＝首发 → 现在，停用过的小时也算在分母里。
    3. **线数**只有在账号总数对得上时才是真值，否则是下界。
    """
    _configure(repository)

    html = _fragment(client)

    assert "派出日" in html
    assert "在岗" in html
    assert "首发" in html
    assert "下界" in html


def test_the_overview_page_reaches_this_section(
    client: TestClient, repository: SqlAlchemyRepository
) -> None:
    """整页上要有这一段的容器，并且真的去取那条片段路由。

    ⚠️ 这一段**不走 `/overview?fragment=` 那一套**（理由在
    `web/origin_efficiency.py` 头上），所以整页只放一个空容器 + 一次 fetch。
    容器或者那次 fetch 少一个，这一段在页面上就整个不存在——而两个模块各自的
    用例都照样是绿的。
    """
    _configure(repository)

    page = client.get("/overview")

    assert page.status_code == 200
    assert 'id="overview-origins"' in page.text
    assert "/overview/origin-efficiency" in page.text
