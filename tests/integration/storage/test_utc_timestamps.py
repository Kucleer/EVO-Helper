"""钉住 ``UTCDateTime`` 的时区语义。

这批用例守的是**在 SQLite 上看不出来、换到 Postgres 才会退化**的那一点：
业务时刻列必须是 ``TIMESTAMP WITH TIME ZONE``，写进去带时区、读出来还带时区。
没有它们，退化要等到迁完库、实机跑歪了才被发现。
"""

from __future__ import annotations

import importlib.util
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.storage import models as orm
from evo_helper.storage.database import (
    Base,
    UTCDateTime,
    create_database_engine,
    create_session_factory,
)

SHANGHAI = timezone(timedelta(hours=8))

#: `models.py` 里所有走 `UTCDateTime` 的列。写死而不是从 metadata 反推：
#: 反推出来的清单会跟着模型一起变，那样这条断言永远成立，也就永远不告诉你任何事。
EXPECTED_TIMESTAMP_COLUMNS = frozenset(
    {
        "artifacts.created_at_utc",
        "attack_dispatches.dispatched_at_utc",
        "attack_dispatches.expected_report_at_utc",
        "attack_dispatches.line_free_at_utc",
        "attack_intents.created_at_utc",
        "attack_intents.cycle_start_utc",
        "battle_reports.reported_at_utc",
        "bot_targets.last_attack_at_utc",
        "bot_targets.last_dispatch_at_utc",
        "bot_targets.last_report_at_utc",
        "bot_targets.last_scanned_at_utc",
        "bot_targets.military_score_at_utc",
        "coordinate_scans.scanned_at_utc",
        "daily_reconciliations.reconciled_at_utc",
        "intel_filters.created_at_utc",
        "intel_filters.updated_at_utc",
        "mission_runs.ended_at_utc",
        "mission_runs.started_at_utc",
        "mission_tasks.created_at_utc",
        "mission_tasks.quota_exhausted_until_utc",
        "mission_tasks.round_started_at_utc",
        "mission_tasks.updated_at_utc",
        "planet_scout_alerts.delivered_at_utc",
        "planet_scout_alerts.reported_at_utc",
        "military_ranking_entries.observed_at_utc",
        "military_ranking_snapshots.captured_at_utc",
        "run_instances.created_at_utc",
        "run_instances.drained_at_utc",
        "run_instances.finished_at_utc",
        "run_instances.resume_at_utc",
        "run_instances.started_at_utc",
        "run_instances.target_date",
        "scan_plans.created_at_utc",
        "scan_plans.updated_at_utc",
        "scout_reports.reported_at_utc",
        "state_events.occurred_at_utc",
        "target_revisits.executed_at_utc",
        "target_revisits.requested_at_utc",
        "ui_observations.observed_at_utc",
    }
)

#: `planet_scout_alerts` is created after the one-off b6 migration.  Its DDL
#: already uses `UTCDateTime`, so asking that historical migration to alter a
#: table which did not exist yet would be both impossible and misleading.
POST_TIMESTAMP_MIGRATION_COLUMNS = frozenset(
    {
        "planet_scout_alerts.delivered_at_utc",
        "planet_scout_alerts.reported_at_utc",
        "military_ranking_snapshots.captured_at_utc",
        "military_ranking_entries.observed_at_utc",
    }
)


def _rendered_types(dialect: object) -> dict[str, str]:
    """每张表每一列在给定方言下的 DDL 类型。"""
    return {
        f"{table.name}.{column.name}": str(column.type.compile(dialect=dialect))  # type: ignore[arg-type]
        for table in Base.metadata.sorted_tables
        for column in table.columns
    }


def test_every_business_timestamp_is_timestamptz_on_postgres() -> None:
    """Postgres 上必须是 ``TIMESTAMP WITH TIME ZONE``。

    ``WITHOUT TIME ZONE`` 会把 tzinfo 静默截掉，读回来变 naive——不报错，
    只是把「按 UTC 日切配额」「按轮起始时刻分战报」这些判据一起变得可疑。
    """
    rendered = _rendered_types(postgresql.dialect())
    timestamp_columns = {name for name, ddl in rendered.items() if ddl.startswith("TIMESTAMP")}

    assert timestamp_columns == EXPECTED_TIMESTAMP_COLUMNS
    naive = sorted(
        name for name in timestamp_columns if rendered[name] != "TIMESTAMP WITH TIME ZONE"
    )
    assert naive == []


def test_sqlite_ddl_is_unchanged_so_the_migration_is_a_no_op() -> None:
    """SQLite 上建表类型仍是 ``DATETIME``——这正是迁移敢在 SQLite 上什么都不做的依据。

    哪天 SQLAlchemy 改了这一点，这条会先红，而不是等到某次 `alembic upgrade`
    在生产库上重建 15 张表。
    """
    rendered = _rendered_types(sqlite.dialect())

    assert {rendered[name] for name in EXPECTED_TIMESTAMP_COLUMNS} == {"DATETIME"}


def test_the_migration_covers_every_timestamp_column() -> None:
    """迁移里那份清单必须与模型一一对应。

    这条在 SQLite 上永远看不出差别（迁移在 SQLite 上什么都不做），所以清单漏一列
    要到 Postgres 上才暴露：那一列会留在 ``TIMESTAMP WITHOUT TIME ZONE``，
    而 ORM 已经按 ``TIMESTAMPTZ`` 在写它。
    """
    original = _load_migration("b6e0a4f21c98_timestamps_with_timezone")
    ranking = _load_migration("e8b7c1d23a40_ranking_military_scores")
    covered = {f"{table}.{column}" for table, column, _ in original._COLUMNS}
    covered.update(f"{table}.{column}" for table, column in ranking._TIMESTAMP_COLUMNS)

    assert covered == EXPECTED_TIMESTAMP_COLUMNS - POST_TIMESTAMP_MIGRATION_COLUMNS


def _load_migration(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_binding_normalises_to_utc_and_keeps_the_offset() -> None:
    """写入值必须是**带时区**的 UTC。

    - 丢了 tzinfo：Postgres 的 ``TIMESTAMPTZ`` 会拿会话时区去解释它，整库偏时差。
    - 没换算成 UTC：SQLite 方言把偏移量直接丢掉而不换算，``03:04+08:00``
      会落盘成 ``03:04``，比真实 UTC 时刻早 8 小时。
    """
    moment = datetime(2026, 8, 12, 3, 4, 5, tzinfo=SHANGHAI)

    bound = UTCDateTime().process_bind_param(moment, sqlite.dialect())

    assert bound is not None
    assert bound.tzinfo is not None, "tzinfo 被剥掉了：Postgres 会按会话时区解释它"
    assert bound.utcoffset() == timedelta(0)
    assert bound == moment
    assert (bound.year, bound.month, bound.day, bound.hour) == (2026, 8, 11, 19)


def test_a_naive_value_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        UTCDateTime().process_bind_param(datetime(2026, 8, 12, 3, 4, 5), sqlite.dialect())


def test_a_naive_result_is_labelled_utc_not_read_as_local_time() -> None:
    """SQLite（以及尚未迁移的 Postgres 列）返回 naive 值，一律**贴** UTC 标签。

    ⚠️ 这条必须在**非 UTC** 的本地时区下跑才有意义：把 ``replace(tzinfo=UTC)``
    写成 ``astimezone(UTC)``（即「naive 当本地时间」）在 UTC 主机上是同一个函数，
    差别一点也看不出来，而 CI 恰好跑在 UTC 的 Linux 上。所以这里先把本地时区
    掰到 UTC+8 再验——掰不动才退回去只验标签。
    """
    with _local_timezone_shifted() as shifted:
        result = UTCDateTime().process_result_value(
            datetime(2026, 8, 11, 19, 4, 5), sqlite.dialect()
        )
        assert shifted, "既没有 tzset、本机又在 UTC 上：这条在此环境下无从区分"

    assert result == datetime(2026, 8, 11, 19, 4, 5, tzinfo=UTC)
    assert result is not None and result.utcoffset() == timedelta(0)


@contextmanager
def _local_timezone_shifted() -> Iterator[bool]:
    """把进程的本地时区掰到非 UTC，退出时还原。

    POSIX 上靠 ``TZ`` + ``time.tzset()``；Windows 没有 ``tzset``，那就看本机
    自己是不是已经不在 UTC 上（开发机在 UTC+8）。两条都不成立时返回 False。
    """
    if not hasattr(time, "tzset"):
        yield datetime.now().astimezone().utcoffset() != timedelta(0)
        return
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        yield True
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_a_shifted_result_is_converted_not_relabelled() -> None:
    """Postgres 的 ``TIMESTAMPTZ`` 按**会话时区**把值交回来，必须换算。

    ``replace(tzinfo=UTC)`` 会把 ``03:04+08:00`` 谎报成 ``03:04+00:00``——
    同一个数字，早了 8 小时，而且不报错。
    """
    result = UTCDateTime().process_result_value(
        datetime(2026, 8, 12, 3, 4, 5, tzinfo=SHANGHAI), postgresql.dialect()
    )

    assert result == datetime(2026, 8, 11, 19, 4, 5, tzinfo=UTC)
    assert result is not None and result.utcoffset() == timedelta(0)
    assert (result.year, result.month, result.day, result.hour) == (2026, 8, 11, 19)


def test_a_non_utc_moment_round_trips_as_the_same_instant(tmp_path: Path) -> None:
    """存进去是 aware、读出来还是 aware，而且是同一个时刻。"""
    session_factory = _session_factory(tmp_path)
    artifact_id = uuid4()
    moment = datetime(2026, 8, 12, 3, 4, 5, tzinfo=SHANGHAI)
    _insert_artifact(session_factory, artifact_id, moment)

    with session_factory() as session:
        stored = session.get(orm.ArtifactRow, artifact_id)

    assert stored is not None
    assert stored.created_at_utc.tzinfo is not None, "读出来是 naive：时区语义在库里丢了"
    assert stored.created_at_utc.utcoffset() == timedelta(0)
    assert stored.created_at_utc == moment
    assert stored.created_at_utc == datetime(2026, 8, 11, 19, 4, 5, tzinfo=UTC)


def test_the_stored_text_stays_utc_wall_clock(tmp_path: Path) -> None:
    """落盘字符串必须是 UTC 挂钟时间，且不带偏移量。

    ``repository.count_dispatches_since`` 与 ``_accepted_attacks_on`` 用
    ``func.date(dispatched_at_utc)`` 直接切日，切的就是这串字符。多出一个 ``+08:00``
    后缀、或者存成本地挂钟，海盗每天 32 次的日界就会挪位——而 SQLite 不会为此报错。
    """
    session_factory = _session_factory(tmp_path)
    artifact_id = uuid4()
    # UTC+8 的 8 月 12 日凌晨 3 点，是 UTC 的 8 月 11 日 19 点：跨日，切错日会当场看出来。
    _insert_artifact(session_factory, artifact_id, datetime(2026, 8, 12, 3, 4, 5, tzinfo=SHANGHAI))

    with session_factory() as session:
        raw = session.scalar(text("SELECT created_at_utc FROM artifacts"))
        day = session.scalar(select(func.date(orm.ArtifactRow.created_at_utc)))

    assert raw == "2026-08-11 19:04:05.000000"
    assert day == "2026-08-11"


def _insert_artifact(
    session_factory: sessionmaker[Session], artifact_id: object, moment: datetime
) -> None:
    with session_factory() as session:
        session.add(
            orm.ArtifactRow(
                id=artifact_id,
                path=f"captures/{artifact_id}.png",
                sha256="0" * 64,
                media_type="image/png",
                source="test",
                created_at_utc=moment,
            )
        )
        session.commit()


def _session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine: Engine = create_database_engine(f"sqlite:///{tmp_path / 'timestamps.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)
