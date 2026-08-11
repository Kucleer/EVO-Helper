"""攻击日志页的日期筛选与「预计战报」时区。

两件事在这里钉死：

1. **日期按游戏时间 UTC+0 的自然日切。** 页面第一列写的就是 UTC+0，筛选换个口径
   就会自相矛盾；更要命的是拿现实时间 UTC+8 的日期去切，会把每天最早的八小时
   （UTC+0 16:00–24:00）划到后一天，而海盗每日 32 次配额正是按游戏日算的。
   所以样本里必须有一发压在 UTC+8 日界上：UTC+0 08-09 20:30 = 现实 08-10 04:30。
2. **`?date=` 空串是「不筛」，不是 422。** 日期框天生带「清空」这个动作，清空之后
   浏览器照样提交 `date=`。PR #74 已经在星球列表页的「全部银河系」上踩过一次：
   声明成 `date | None` 直接返回一页 JSON 报错。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import TARGET_KIND_PIRATE, AttackDispatch, AttackIntent
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView

ORIGIN = Coordinate(2, 137, 18)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
PRESET = FleetPresetRef(name="AAA", signature="深空吞噬者:70")
NOW = datetime(2026, 8, 10, 6, tzinfo=UTC)

#: 前一个游戏日创建、当天凌晨才真正派出。第一列显示的是派遣时刻，筛选也得跟着它。
EARLY = Coordinate(2, 137, 5)
EARLY_CREATED = datetime(2026, 8, 8, 23, 50, tzinfo=UTC)
EARLY_DISPATCHED = datetime(2026, 8, 9, 0, 30, tzinfo=UTC)

#: 压在 UTC+8 日界上的那一发：游戏时间 08-09 20:30，现实时间已经是 08-10 04:30。
LATE = Coordinate(2, 137, 4)
LATE_DISPATCHED = datetime(2026, 8, 9, 20, 30, tzinfo=UTC)
LATE_FLIGHT = timedelta(minutes=45)

#: 下一个游戏日。
NEXT = Coordinate(2, 137, 6)
NEXT_DISPATCHED = datetime(2026, 8, 10, 1, tzinfo=UTC)

#: 被闸门拦下、根本没派出去的意图，按创建时刻归日。
INTENT_ONLY = Coordinate(2, 137, 7)
INTENT_ONLY_CREATED = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _seed(tmp_path: Path) -> tuple[PersistentApplicationService, TestClient]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'log-filters.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: NOW)
    plan = service.create_plan(
        name="海盗攻击",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(
            ScanRangeView(Coordinate(2, 137, 1), LATE, ORIGIN, PRESET.name, PRESET.signature, 0),
        ),
    )
    run = service.start_run(plan.id, "log-filter-0001")
    repository = SqlAlchemyRepository(factory)

    def _intent(target: Coordinate, created_at_utc: datetime) -> AttackIntent:
        intent = AttackIntent(
            intent_id=uuid4(),
            run_id=run.run_id,
            origin=ORIGIN,
            target=target,
            preset=PRESET,
            cycle_start_utc=CYCLE,
            created_at_utc=created_at_utc,
            target_kind=TARGET_KIND_PIRATE,
        )
        repository.save_attack_intent(intent)
        return intent

    def _dispatch(intent: AttackIntent, moment: datetime, flight: timedelta | None = None) -> None:
        dispatch = AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=moment,
            accepted=True,
        )
        repository.save_dispatch(dispatch)
        if flight is not None:
            repository.record_flight_time(dispatch.dispatch_id, flight, moment)

    _dispatch(_intent(EARLY, EARLY_CREATED), EARLY_DISPATCHED)
    _dispatch(_intent(LATE, LATE_DISPATCHED), LATE_DISPATCHED, LATE_FLIGHT)
    _dispatch(_intent(NEXT, NEXT_DISPATCHED), NEXT_DISPATCHED)
    _intent(INTENT_ONLY, INTENT_ONLY_CREATED)
    return service, TestClient(create_persistent_app(factory))


def _link(target: Coordinate) -> str:
    return f"/targets/{target.galaxy}:{target.system}:{target.position}"


def test_an_empty_date_means_no_filter_instead_of_422(tmp_path: Path) -> None:
    """日期框清空后提交的就是 `date=`——它必须是「全部日期」，不是错误页。"""
    _, client = _seed(tmp_path)

    response = client.get("/logs", params={"kind": "all", "date": ""})

    assert response.status_code == 200
    body = response.text
    for target in (EARLY, LATE, NEXT, INTENT_ONLY):
        assert _link(target) in body


def test_the_date_filter_cuts_on_the_utc0_game_day(tmp_path: Path) -> None:
    """UTC+0 08-09 这一天：含凌晨 00:30 与晚间 20:30，不含次日 01:00。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"date": "2026-08-09"}).text

    assert _link(EARLY) in body
    assert _link(LATE) in body
    assert _link(INTENT_ONLY) in body
    assert _link(NEXT) not in body


def test_a_dispatch_past_the_utc8_midnight_still_belongs_to_the_game_day(
    tmp_path: Path,
) -> None:
    """08-09 20:30 UTC 的现实时间是 08-10 04:30——按现实日期切就会跑到 08-10 去。

    这一条是整个日期口径的判据：只有按 UTC+0 切，它才留在 08-09。
    """
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"date": "2026-08-10"}).text

    # 页面确实把它的现实时间显示成了 08-10，可它不属于 08-10 这个游戏日。
    assert "2026-08-10 04:30:00" in client.get("/logs", params={"date": "2026-08-09"}).text
    assert _link(LATE) not in body
    assert _link(NEXT) in body


def test_an_undispatched_intent_is_dated_by_when_it_was_created(tmp_path: Path) -> None:
    """08-08 创建、08-09 才派出的那一发归 08-09——和第一列显示的时刻一致。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"date": "2026-08-08"}).text

    assert _link(EARLY) not in body
    assert "还没有攻击记录" in body


def test_the_date_filter_reaches_past_the_row_limit(tmp_path: Path) -> None:
    """日期筛选下推到 SQL：先砍 limit 再筛日期的话，翻旧账永远是空页。"""
    service, _ = _seed(tmp_path)

    # limit=1 时，最新的一条是 08-10 那发；08-09 仍必须取得到自己的记录。
    entries = service.list_attack_log(1, day_utc=date(2026, 8, 9))

    assert [entry.target for entry in entries] == [LATE]


def test_the_expected_report_column_is_shown_in_utc8(tmp_path: Path) -> None:
    """预计战报按现实时间 UTC+8 显示，表头也得跟着写 UTC+8。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"date": "2026-08-09"}).text

    assert "预计战报（现实 UTC+8）" in body
    assert "预计战报（游戏 UTC+0）" not in body
    # 出发 08-09 20:30 UTC + 45 分钟 = 21:15 UTC，也就是现实时间 08-10 05:15。
    assert "2026-08-10 05:15:00" in body
    assert "2026-08-09 21:15:00" not in body


def test_switching_event_kind_keeps_the_chosen_date(tmp_path: Path) -> None:
    """筛完日期再点「海盗」不能把日期甩掉，否则每种视图都没有可分享的链接。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"date": "2026-08-09"}).text

    assert "kind=pirate&amp;date=2026-08-09" in body
