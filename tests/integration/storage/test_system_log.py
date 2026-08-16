"""`system_log` 的落盘、筛选、分页与保留期清理。

用真库（临时 SQLite 文件）而不是假仓储：这一批要验的正是「筛选下推到 SQL 之后
数出来的总数还对不对」，在内存里过滤的假实现验不出这件事。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.scheduler import MissionKind
from evo_helper.infrastructure.system_log import SystemLogRecord, SystemLogSink
from evo_helper.infrastructure.system_log_db import database_sink, purge_system_log
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.storage.system_log import SystemLogRepository

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
