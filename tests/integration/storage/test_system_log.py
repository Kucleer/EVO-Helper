"""`system_log` 的落盘、筛选、分页与保留期清理。

用真库（临时 SQLite 文件）而不是假仓储：这一批要验的正是「筛选下推到 SQL 之后
数出来的总数还对不对」，在内存里过滤的假实现验不出这件事。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.scheduler import MissionKind
from evo_helper.infrastructure.system_log import SystemLogRecord, SystemLogSink
from evo_helper.infrastructure.system_log_db import database_sink, purge_system_log
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.storage.system_log import (
    FACET_CACHE_TTL_S,
    PAYLOAD_INLINE_LIMIT,
    SystemLogRepository,
)

BASE = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def record(
    *,
    minute: int = 0,
    level: str = "INFO",
    source: str = "tools.bot_loop",
    host: str = "live-pc",
    message: str = "一句话",
    mission_kind: str | None = "bot",
    task_id: int | None = 1,
    run_id: object | None = None,
    payload_json: str = "{}",
) -> SystemLogRecord:
    return SystemLogRecord(
        logged_at_utc=BASE + timedelta(minutes=minute),
        level=level,
        source=source,
        host=host,
        pid=4321,
        message=message,
        run_id=run_id,  # type: ignore[arg-type]
        task_id=task_id,
        mission_kind=mission_kind,
        payload_json=payload_json,
    )


@pytest.fixture
def logs(session_factory: sessionmaker[Session]) -> SystemLogRepository:
    return SystemLogRepository(session_factory)


def test_ids_are_assigned_in_arrival_order(logs: SystemLogRepository) -> None:
    """同一进程 FIFO 入库，`id` 递增就是发生顺序——这正是不要 `seq` 列的依据。

    ⚠️ 主键是 `BigInteger().with_variant(Integer, "sqlite")`。少了那个变体，
    SQLite 上建出来是 `BIGINT`，不是 rowid 别名，这一句 insert 会当场
    `IntegrityError: NOT NULL constraint failed`。
    """
    logs.append([record(minute=0, message="先"), record(minute=0, message="后")])

    rows = logs.query(limit=10).rows

    assert [row.message for row in rows] == ["后", "先"], "同一时刻要按 id 倒序，不能靠数据库随缘"
    assert rows[0].id > rows[1].id


def test_an_empty_batch_touches_nothing(logs: SystemLogRepository) -> None:
    logs.append([])

    assert logs.query().total == 0


def test_newest_first_with_server_side_paging(logs: SystemLogRepository) -> None:
    """总数在 SQL 里数，不许拿本页行数冒充。"""
    logs.append([record(minute=index, message=f"m{index}") for index in range(25)])

    first = logs.query(limit=10)
    second = logs.query(limit=10, offset=10)

    assert first.total == 25 and second.total == 25
    assert [row.message for row in first.rows] == [f"m{index}" for index in range(24, 14, -1)]
    assert [row.message for row in second.rows] == [f"m{index}" for index in range(14, 4, -1)]
    assert first.has_more and second.has_more
    assert not logs.query(limit=10, offset=20).has_more


def test_every_filter_narrows_in_sql(logs: SystemLogRepository) -> None:
    logs.append(
        [
            record(minute=0, level="INFO", host="live-pc", source="tools.pirate_loop"),
            record(minute=1, level="ERROR", host="live-pc", source="tools.pirate_loop"),
            record(minute=2, level="ERROR", host="console-pc", source="web.app"),
            record(minute=3, level="WARNING", host="console-pc", mission_kind="scan"),
        ]
    )

    assert logs.query(level="ERROR").total == 2
    assert logs.query(host="console-pc").total == 2
    assert logs.query(source="tools.pirate_loop").total == 2
    assert logs.query(mission_kind="scan").total == 1
    assert logs.query(level="error").total == 2, "级别大小写不该影响筛选"


def test_a_time_window_is_inclusive_on_both_ends(logs: SystemLogRepository) -> None:
    logs.append([record(minute=index, message=f"m{index}") for index in range(5)])

    page = logs.query(since=BASE + timedelta(minutes=1), until=BASE + timedelta(minutes=3))

    assert [row.message for row in page.rows] == ["m3", "m2", "m1"]


def test_the_keyword_reaches_into_the_payload(logs: SystemLogRepository) -> None:
    """坐标、预设名常常只在 payload 里。只搜正文的话「查 2:137」一条都搜不到。"""
    logs.append(
        [
            record(minute=0, message="派出去了", payload_json='{"coordinate": "2:137:1"}'),
            record(minute=1, message="2:200:3 拦下"),
            record(minute=2, message="无关"),
        ]
    )

    assert logs.query(keyword="2:137").total == 1
    assert logs.query(keyword="2:200").total == 1
    assert logs.query(keyword="没有这个").total == 0


def test_blank_filters_mean_no_filter(logs: SystemLogRepository) -> None:
    """空串是「全部」那一项的 value，绝不能被当成「等于空字符串」。"""
    logs.append([record(minute=0)])

    assert logs.query(level="", host="  ", source="", mission_kind="", keyword="").total == 1


def test_a_run_id_survives_deleting_the_run(
    session_factory: sessionmaker[Session], logs: SystemLogRepository
) -> None:
    """外键没有 CASCADE：日志是账，一轮记录清掉不该顺手把它一起删了。"""
    repository = SqlAlchemyRepository(session_factory)
    run_id = repository.begin_mission_run(
        MissionKind.BOT,
        task_id=1,
        command=["python", "-m", "evo_helper.tools.bot_loop"],
        pid=999,
        started_at_utc=BASE,
        log_path="var/logs/mission-bot.log",
    )
    logs.append([record(minute=0, run_id=run_id), record(minute=1, run_id=None)])

    assert logs.query(run_id=run_id).total == 1
    assert logs.query().total == 2


def test_the_page_offers_the_hosts_and_sources_it_knows(logs: SystemLogRepository) -> None:
    """两个下拉框的候选值由库给，页面不许自己猜有哪几台机器。"""
    logs.append(
        [
            record(minute=0, host="live-pc", source="tools.bot_loop"),
            record(minute=1, host="console-pc", source="web.app"),
            record(minute=2, host="live-pc", source="web.app"),
        ]
    )

    page = logs.query(host="live-pc")

    assert page.hosts == ("console-pc", "live-pc"), "候选值必须是全局的，不该跟着当前筛选缩水"
    assert page.sources == ("tools.bot_loop", "web.app")


def test_the_limit_is_clamped_rather_than_refused(logs: SystemLogRepository) -> None:
    """手改链接要 100 万行时夹到上限，而不是 422——那是一页 HTML。"""
    logs.append([record(minute=0)])

    assert logs.query(limit=10**6).limit == 1000
    assert logs.query(limit=0).limit == 1
    assert logs.query(offset=-5).offset == 0


# -- 现场图与大 payload ------------------------------------------------------


def _screenshot_payload(chars: int = 120_000) -> str:
    """一条带现场图的 payload，量级同实机那批（8 万到 16 万字符）。"""
    return json.dumps({"note": "画面认不出", "thumbnail_png_base64": "A" * chars})


def test_the_list_page_does_not_carry_the_screenshot_bytes(logs: SystemLogRepository) -> None:
    """⚠️ 本组核心判据：**超限的 payload 一个字节都不许随这一页搬回来。**

    判据落在「那一大串字符在不在取回来的行里」，不是「有没有调某个函数」。
    量级不是省几毫秒：生产库上一页 200 行全落在带图的行上时，全量是 27 MB /
    3.9 秒，只带长度是 0.11 秒（实测 2026-09-09）。
    """
    logs.append([record(minute=0, payload_json=_screenshot_payload())])

    row = logs.query(payload_inline_limit=PAYLOAD_INLINE_LIMIT).rows[0]

    assert "A" * 40 not in row.payload_json
    assert row.payload_json == "", "这一次没取 ≠ 库里没有"
    assert row.payload_bytes > 120_000, "省掉的是多大一段，页面要说得出"
    assert row.payload_withheld
    assert row.has_screenshot, "不搬那几万字符，不等于不知道有图"


def test_without_a_limit_the_whole_payload_still_comes_back(logs: SystemLogRepository) -> None:
    """⚠️ `payload_json` 是排障的命根子。`GET /api/system-log` 走的就是这条，
    不传上限就必须是整段，含那张图。"""
    payload = _screenshot_payload()
    logs.append([record(minute=0, payload_json=payload)])

    row = logs.query().rows[0]

    assert row.payload_json == payload
    assert not row.payload_withheld
    assert row.has_screenshot


def test_an_ordinary_payload_still_travels_with_the_page(logs: SystemLogRepository) -> None:
    """让路的是库里 0.5% 的大行，剩下 99.5% 不许跟着多一次点击。"""
    logs.append([record(minute=0, payload_json='{"coordinate": "2:137:1"}')])

    row = logs.query(payload_inline_limit=PAYLOAD_INLINE_LIMIT).rows[0]

    assert row.payload_json == '{"coordinate": "2:137:1"}'
    assert not row.payload_withheld
    assert not row.has_screenshot


def test_an_empty_screenshot_key_does_not_count_as_a_screenshot(
    logs: SystemLogRepository,
) -> None:
    """抓不到画面时 `tools.screen_diagnostics` 写进去的是空串。

    页面上一个点开是 404 的「现场图」比不显示更糟，所以取回了 payload 就按内容
    确切地判，不看那个键在不在。
    """
    logs.append([record(minute=0, payload_json=json.dumps({"thumbnail_png_base64": ""}))])

    assert not logs.query(payload_inline_limit=PAYLOAD_INLINE_LIMIT).rows[0].has_screenshot


def test_one_row_can_still_be_fetched_whole_by_id(logs: SystemLogRepository) -> None:
    """列表页不搬图之后，「点开再取」必须走得通——否则省下的字节就是丢掉的证据。"""
    payload = _screenshot_payload()
    logs.append([record(minute=0, payload_json=payload)])
    entry_id = logs.query().rows[0].id

    fetched = logs.entry(entry_id)

    assert fetched is not None
    assert fetched.payload_json == payload
    assert logs.entry(entry_id + 10_000) is None, "认不出的 id 是「没有」，不是报错"


def test_capping_the_payload_does_not_disturb_the_order_or_the_paging(
    logs: SystemLogRepository,
) -> None:
    """这一页的 `id` 先由子查询定下来，再去投影 payload 那几列（否则 `length()`
    会对扫过的每一行求值，深翻页时是 10 万次）。子查询套 `JOIN` 之后**顺序要靠
    外层那个 `ORDER BY` 重新钉**，漏了它翻页就会重复或漏行。
    """
    logs.append([record(minute=index, message=f"第 {index} 句") for index in range(6)])

    walked = []
    for offset in (0, 2, 4):
        page = logs.query(offset=offset, limit=2, payload_inline_limit=PAYLOAD_INLINE_LIMIT)
        assert page.offset == offset
        walked.extend(row.message for row in page.rows)

    assert walked == [f"第 {index} 句" for index in (5, 4, 3, 2, 1, 0)]


# -- 筛选下拉框的候选值 ------------------------------------------------------


def test_a_machine_that_starts_logging_shows_up_in_the_dropdown_at_once(
    logs: SystemLogRepository,
) -> None:
    """⚠️ **少一个 host 就等于一台机器的日志「查不到了」。**

    两个 `distinct` 现在带缓存（原先各自全表扫一遍，占服务端每页 360 ms 里的
    276 ms）。这一条钉的是缓存**已经热**之后新来一台机器的情形：它写下的第一条
    就是最新的那一行，所以当场就得在框里。
    """
    logs.append([record(minute=0, host="console-pc")])
    assert logs.query().hosts == ("console-pc",), "先把缓存烧热"

    logs.append([record(minute=1, host="live-pc", source="tools.pirate_loop")])

    page = logs.query()
    assert page.hosts == ("console-pc", "live-pc")
    assert page.sources == ("tools.bot_loop", "tools.pirate_loop")
    assert logs.query(host="live-pc").hosts == ("console-pc", "live-pc"), (
        "候选值是全局事实，不跟着当前筛选缩水"
    )


def test_the_dropdown_catches_up_when_the_new_rows_are_off_this_page(
    session_factory: sessionmaker[Session],
) -> None:
    """写日志的机器在**另一台**上，本进程作废不了缓存——所以缓存必须会过期。

    这里刻意让新机器那一行落在当前筛选之外（按 level 筛掉了），「并上这一页」
    救不了，只有过期重取能救。
    """
    now = [1000.0]
    logs = SystemLogRepository(session_factory, clock=lambda: now[0])
    logs.append([record(minute=0, host="console-pc", level="ERROR")])
    assert logs.query(level="ERROR").hosts == ("console-pc",)

    # 另一台机器、另一个进程：换一个仓储实例写，本实例的缓存不知情。
    SystemLogRepository(session_factory).append([record(minute=1, host="live-pc")])

    assert logs.query(level="ERROR").hosts == ("console-pc",), "还没到期，照旧"
    now[0] += FACET_CACHE_TTL_S
    assert logs.query(level="ERROR").hosts == ("console-pc", "live-pc")


# -- 保留策略 ----------------------------------------------------------------


def test_purge_drops_only_what_is_older_than_the_cutoff(logs: SystemLogRepository) -> None:
    logs.append([record(minute=-100, message="旧"), record(minute=0, message="新")])

    deleted = logs.purge_before(BASE - timedelta(minutes=50))

    assert deleted == 1
    assert [row.message for row in logs.query().rows] == ["新"]


def test_purge_refuses_a_naive_cutoff(logs: SystemLogRepository) -> None:
    """naive 时刻会被 Postgres 按会话时区解释——整批日志按错的边界删掉。"""
    with pytest.raises(ValueError, match="timezone-aware"):
        logs.purge_before(datetime(2026, 8, 16, 12, 0, 0))


def test_retention_days_convert_to_a_cutoff(
    session_factory: sessionmaker[Session], logs: SystemLogRepository
) -> None:
    logs.append(
        [
            record(minute=-60 * 24 * 20, message="二十天前"),
            record(minute=-60 * 24 * 3, message="三天前"),
        ]
    )

    deleted = purge_system_log(session_factory, retention_days=14, now=BASE)

    assert deleted == 1
    assert [row.message for row in logs.query().rows] == ["三天前"]


def test_zero_retention_means_keep_everything_not_delete_everything(
    session_factory: sessionmaker[Session], logs: SystemLogRepository
) -> None:
    """把 0 当成「全删」太危险：一个手滑的配置值就能清空事后唯一能翻的东西。"""
    logs.append([record(minute=-60 * 24 * 900, message="很旧")])

    assert purge_system_log(session_factory, retention_days=0, now=BASE) == 0
    assert logs.query().total == 1


def test_a_failing_purge_reports_zero_instead_of_raising() -> None:
    """清理失败不该把控制台的启动拖垮。"""

    def broken() -> Session:
        raise RuntimeError("库连不上")

    assert purge_system_log(broken, retention_days=14, now=BASE) == 0  # type: ignore[arg-type]


# -- sink 接到真库上 ----------------------------------------------------------


def test_the_sink_writes_through_to_the_table(
    session_factory: sessionmaker[Session], logs: SystemLogRepository
) -> None:
    """端到端一遍：`emit` → 后台线程 → 表里真的多了行。"""
    sink: SystemLogSink = database_sink(session_factory, flush_interval_s=0.01)
    try:
        for index in range(5):
            sink.emit(record(minute=index, message=f"m{index}"))
    finally:
        sink.close(timeout=5)

    assert logs.query().total == 5
    assert sink.stats.written == 5


def test_a_batch_that_violates_the_schema_does_not_escape_the_sink(
    session_factory: sessionmaker[Session],
) -> None:
    """写库真的失败时（这里给一个不存在的 run_id，撞外键），异常不许漏出来。

    ⚠️ 这条和单元测试里那个注入的写入器不是重复：那边验的是 sink 的边界，
    这边验的是**真正的 SQLAlchemy 异常**也被同一道边界挡住了。
    """
    sink: SystemLogSink = database_sink(session_factory, flush_interval_s=0.01)
    try:
        sink.emit(record(minute=0, run_id=uuid4()))  # 没有这一轮，外键约束会拒绝
        sink.flush(timeout=5)
    finally:
        sink.close(timeout=5)

    assert sink.stats.failed_batches == 1
    assert sink.stats.written == 0
