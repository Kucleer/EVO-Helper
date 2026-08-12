"""攻击日志的目标坐标筛选（支持范围）。

三件事在这里钉死：

1. **筛选下推到 SQL。** 这一页只取最近 `ATTACK_LOG_LIMIT` 条，在内存里筛坐标
   等于「先砍掉历史再问历史」——查一个旧坐标会得到空页，而空页读起来和
   「那个坐标一发没打」一模一样。日期筛选（PR #77）已经踩过一次同样的坑，
   事件类型那一档当时漏了，这次一并搬下去。
2. **空串是「不筛」，不是错误。** 两个坐标框走表单提交，必然带上
   `target_start=&target_end=`（PR #74 的教训）。
3. **区间比的是打包坐标。** 逐分量比较会把 2:130:14 排除在 2:130 – 2:140 之外，
   而那正是用户会写的那种范围。
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from evo_helper.domain.intel_query import parse_coordinate_span
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
)
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
PRESET = FleetPresetRef(name="AAA", signature="深空吞噬者:70")
NOW = datetime(2026, 8, 10, 6, tzinfo=UTC)

#: 区间里的三发，**故意让位号在区间之外**：2:130:14 与 2:140:2 都不满足
#: 「position 在 1 与 20 之间」这种逐分量比较，可它们确实落在 2:130 – 2:140 里。
INSIDE_LOW = Coordinate(2, 130, 14)
INSIDE_MID = Coordinate(2, 135, 3)
INSIDE_HIGH = Coordinate(2, 140, 2)

#: 紧贴区间外的两发，一边一个。
BELOW = Coordinate(2, 129, 19)
ABOVE = Coordinate(2, 141, 1)

#: 最老的一发，用来验证「筛选下推到 SQL」——它排在 limit 之外。
OLD = Coordinate(2, 135, 9)

ORDER = (ABOVE, INSIDE_HIGH, INSIDE_MID, INSIDE_LOW, BELOW, OLD)


def _seed(tmp_path: Path) -> tuple[PersistentApplicationService, TestClient]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'log-target.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: NOW)
    plan = service.create_plan(
        name="海盗攻击",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(ScanRangeView(Coordinate(2, 1, 1), Coordinate(2, 999, 20), ORIGIN, "AAA", "x", 0),),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="log-target-0001", created_at_utc=NOW
    )
    repository = SqlAlchemyRepository(factory)

    # 越靠后创建的越新；日志按创建时刻倒序，所以 ORDER 就是页面上的顺序。
    for index, target in enumerate(reversed(ORDER)):
        moment = datetime(2026, 8, 9, tzinfo=UTC) + timedelta(minutes=index)
        intent = AttackIntent(
            intent_id=uuid4(),
            run_id=run_id,
            origin=ORIGIN,
            target=target,
            preset=PRESET,
            cycle_start_utc=CYCLE,
            created_at_utc=moment,
            # 只有 BELOW 是 bot，其余是海盗——事件类型那一档也要下推 SQL。
            target_kind=TARGET_KIND_BOT if target == BELOW else TARGET_KIND_PIRATE,
        )
        repository.save_attack_intent(intent)
        repository.save_dispatch(
            AttackDispatch(
                dispatch_id=uuid4(),
                intent_id=intent.intent_id,
                dispatched_at_utc=moment,
                accepted=True,
            )
        )
    return service, TestClient(create_persistent_app(factory))


def _link(target: Coordinate) -> str:
    return f"/targets/{target.galaxy}:{target.system}:{target.position}"


def test_an_empty_target_range_means_no_filter_instead_of_an_error(tmp_path: Path) -> None:
    """两个坐标框都空着提交的就是 `target_start=&target_end=`——那是「全部坐标」。"""
    _, client = _seed(tmp_path)

    response = client.get("/logs", params={"kind": "all", "target_start": "", "target_end": ""})

    assert response.status_code == 200
    body = response.text
    for target in ORDER:
        assert _link(target) in body


def test_the_range_includes_both_ends_whole_systems(tmp_path: Path) -> None:
    """`2:130` – `2:140` = 这十一个星系的所有位号，两端都含。

    2:130:14 与 2:140:2 是判据：逐分量比较会把它们排除掉。
    """
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"target_start": "2:130", "target_end": "2:140"}).text

    assert _link(INSIDE_LOW) in body
    assert _link(INSIDE_MID) in body
    assert _link(INSIDE_HIGH) in body
    assert _link(BELOW) not in body
    assert _link(ABOVE) not in body


def test_a_full_three_part_range_narrows_to_positions(tmp_path: Path) -> None:
    """`2:130:1` – `2:135:3` 精确到位号，不含 2:140:2。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"target_start": "2:130:1", "target_end": "2:135:3"}).text

    assert _link(INSIDE_LOW) in body
    assert _link(INSIDE_MID) in body
    assert _link(INSIDE_HIGH) not in body


def test_only_one_end_means_that_one_system(tmp_path: Path) -> None:
    """只填起点 = 「只看这一个星系」，不是「从这里到宇宙尽头」。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"target_start": "2:135", "target_end": ""}).text

    assert _link(INSIDE_MID) in body
    assert _link(OLD) in body
    assert _link(INSIDE_LOW) not in body
    assert _link(INSIDE_HIGH) not in body


def test_the_page_spells_out_the_range_it_actually_used(tmp_path: Path) -> None:
    """`2:130` 补成 `2:130:1`，页面要写补完的那一对。

    不写的话，「为什么 2:130:14 也在里面」用户只能从结果里反推。
    """
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"target_start": "2:130", "target_end": "2:140"}).text

    assert "2:130:1 – 2:140:999" in body


def test_the_target_filter_reaches_past_the_row_limit(tmp_path: Path) -> None:
    """**这条是这份文件的重点**：筛选下推到 SQL。

    limit=1 时，最新的一条是 ABOVE 那发。要是先取 1 条再在内存里筛坐标，
    查 2:135:9 会得到空页——而空页读起来就是「那个坐标没打过」。
    """
    service, _ = _seed(tmp_path)

    entries = service.list_attack_log(1, target_span=parse_coordinate_span("2:135:9", "2:135:9"))

    assert [entry.target for entry in entries] == [OLD]


def test_the_event_kind_filter_also_reaches_past_the_row_limit(tmp_path: Path) -> None:
    """事件类型原先是在内存里筛的——同一个坑，一并搬下去。

    limit=1 且只看 bot 时，必须取到 BELOW（唯一那条 bot 记录），而不是
    「最新那条恰好不是 bot，所以空」。
    """
    service, _ = _seed(tmp_path)

    entries = service.list_attack_log(1, kind=TARGET_KIND_BOT)

    assert [entry.target for entry in entries] == [BELOW]


def test_an_unparsable_range_says_so_instead_of_returning_an_error_page(
    tmp_path: Path,
) -> None:
    """坐标写错时不返回 422（那是一页 JSON，读起来就是「控制台坏了」）。

    照常渲染，但必须在顶上说清楚**这一页没有按坐标筛**——默默不筛才是最坏的
    一种，用户会以为下面那些行就是筛出来的结果。
    """
    _, client = _seed(tmp_path)

    response = client.get("/logs", params={"target_start": "两点一三零", "target_end": "2:140"})

    assert response.status_code == 200
    body = response.text
    assert "未按坐标筛选" in body
    assert _link(ABOVE) in body


def test_the_page_actually_applies_the_event_kind_filter(tmp_path: Path) -> None:
    """事件类型这一档搬进 SQL 之后，页面这一侧还得真的把它传下去。

    只测服务层是不够的：`attack_log_page` 原先在取回若干条之后自己筛一遍，
    把参数漏掉的话，那一行也跟着没了，而页面上「只看 bot」会安静地显示全部。
    """
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"kind": TARGET_KIND_BOT}).text

    assert _link(BELOW) in body
    for pirate in (ABOVE, INSIDE_LOW, INSIDE_MID, INSIDE_HIGH, OLD):
        assert _link(pirate) not in body


def test_switching_event_kind_keeps_the_chosen_target_range(tmp_path: Path) -> None:
    """筛完坐标再点「海盗」不能把坐标甩掉，否则每种视图都没有可分享的链接。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"target_start": "2:130", "target_end": "2:140"}).text

    assert "kind=pirate&amp;target_start=2%3A130&amp;target_end=2%3A140" in body


def test_the_date_form_carries_the_target_range_along(tmp_path: Path) -> None:
    """两个筛选是两张表单，各自都得把对方的值带上，否则一提交就互相清空。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"target_start": "2:130", "target_end": "2:140"}).text

    assert '<input type="hidden" name="target_start" value="2:130">' in body
    assert '<input type="hidden" name="target_end" value="2:140">' in body


def test_the_target_range_composes_with_the_date_filter(tmp_path: Path) -> None:
    """两个筛选一起用是 AND，不是谁覆盖谁。"""
    _, client = _seed(tmp_path)

    body = client.get(
        "/logs",
        params={"target_start": "2:130", "target_end": "2:140", "date": "2026-08-08"},
    ).text

    assert "还没有攻击记录" in body
