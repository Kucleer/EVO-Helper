"""钉住 ``UTCDateTime`` 的时区语义。

这批用例守的是**在 SQLite 上看不出来、换到 Postgres 才会退化**的那一点：
业务时刻列必须是 ``TIMESTAMP WITH TIME ZONE``，写进去带时区、读出来还带时区。
没有它们，退化要等到迁完库、实机跑歪了才被发现。
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta, timezone, tzinfo
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
from support.database import scratch_database_url

SHANGHAI = timezone(timedelta(hours=8))

#: `models.py` 里所有走 `UTCDateTime` 的列。写死而不是从 metadata 反推：
#: 反推出来的清单会跟着模型一起变，那样这条断言永远成立，也就永远不告诉你任何事。
EXPECTED_TIMESTAMP_COLUMNS = frozenset(
    {
        # 61eb261c5a09（AI 选靶影子观测）随表建的，那条迁移里写着 `timezone=True`。
        "ai_target_decisions.decided_at_utc",
        "ai_target_decisions.cycle_start_utc",
        "artifacts.created_at_utc",
        "attack_dispatches.dispatched_at_utc",
        "attack_dispatches.expected_report_at_utc",
        "attack_dispatches.line_free_at_utc",
        "attack_dispatches.line_hold_until_utc",
        "attack_dispatches.line_released_at_utc",
        "attack_intents.created_at_utc",
        "attack_intents.cycle_start_utc",
        # 派这一发时那个军力读数是什么时候读到的（PR #183 的快照列）。
        "attack_intents.target_military_score_at_utc",
        "battle_report_screenshots.captured_at_utc",
        "battle_reports.reported_at_utc",
        "bot_targets.last_attack_at_utc",
        "bot_targets.last_dispatch_at_utc",
        "bot_targets.last_report_at_utc",
        "bot_targets.last_scanned_at_utc",
        "bot_targets.military_score_at_utc",
        "bot_targets.protection_seen_at_utc",
        "bot_targets.unreadable_seen_at_utc",
        "coordinate_scans.scanned_at_utc",
        "daily_reconciliations.reconciled_at_utc",
        "intel_filters.created_at_utc",
        "intel_filters.updated_at_utc",
        "mission_runs.ended_at_utc",
        "mission_runs.started_at_utc",
        "mission_tasks.created_at_utc",
        "mission_tasks.enabled_from_utc",
        "mission_tasks.enabled_until_utc",
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
        # a3c81f5d2b64（挂机心跳）随表建的。⚠️ 这两列全都在比时刻——写成
        # `WITHOUT TIME ZONE` 的话 Postgres 上 tzinfo 会被静默截掉，挂机时长会算错
        # 8 小时（服务器在 UTC+8）而页面上一点异样都看不出来。
        "scheduler_uptime_segments.started_at_utc",
        "scheduler_uptime_segments.last_beat_at_utc",
        "state_events.occurred_at_utc",
        "system_log.logged_at_utc",
        "target_revisits.executed_at_utc",
        "target_revisits.requested_at_utc",
        "ui_observations.observed_at_utc",
    }
)

#: 这几列是在一次性的 b6 迁移**之后**才加进来的（多数是随新表一起建的），
#: 各自的 DDL 本身就已经写了 `DateTime(timezone=True)`。要求那条历史迁移去 alter
#: 一张当时还不存在的表、或者一列当时还不存在的列，既做不到也会把「迁移清单必须
#: 与模型一一对应」这条判据讲错。
#:
#: `attack_dispatches.line_released_at_utc` 是「列版本」的例子：表是老的，列是 b6
#: 之后加的，由它自己那条迁移（`a9d5f31c0e77`）按方言建成 `TIMESTAMPTZ`。往 b6 的
#: 清单里补它，会让那条历史迁移在**已经升过级的**库上去 alter 一列当时还不存在的列。
#:
#: ⚠️ **往这里加一行之前先确认那条新迁移真的写了 `timezone=True`。** 免掉的是
#: 「历史迁移要覆盖它」，不是「它可以是 `WITHOUT TIME ZONE`」——后者在 Postgres 上
#: 会静默截掉 tzinfo，正是这一整个文件要防的那件事。
POST_TIMESTAMP_MIGRATION_COLUMNS = frozenset(
    {
        # 61eb261c5a09（AI 选靶影子观测）随新表建的，那条迁移里也写着 `timezone=True`。
        "ai_target_decisions.decided_at_utc",
        "ai_target_decisions.cycle_start_utc",
        "attack_dispatches.line_released_at_utc",
        "battle_report_screenshots.captured_at_utc",
        "planet_scout_alerts.delivered_at_utc",
        "planet_scout_alerts.reported_at_utc",
        "military_ranking_snapshots.captured_at_utc",
        "military_ranking_entries.observed_at_utc",
        "system_log.logged_at_utc",
        # b3f5c8d10a27（任务定时开关）加的两列，那条迁移里写着 `timezone=True`。
        "mission_tasks.enabled_from_utc",
        "mission_tasks.enabled_until_utc",
        # c3f7a2b81d54（派遣时的目标军力快照）加的，那条迁移里也写着 `timezone=True`。
        "attack_intents.target_military_score_at_utc",
        # b7e4d0c93a15（撞上保护期的时刻）加的，那条迁移里也写着 `timezone=True`。
        "bot_targets.protection_seen_at_utc",
        # d1a7f4b26c93（航线按距离兜底占到几点）加的，那条迁移里也写着 `timezone=True`。
        "attack_dispatches.line_hold_until_utc",
        # d4b6e0f19c73（面板名读不出的时刻）加的，那条迁移里也写着 `timezone=True`。
        "bot_targets.unreadable_seen_at_utc",
        # a3c81f5d2b64（挂机心跳）随新表建的，那条迁移里也写着 `timezone=True`。
        "scheduler_uptime_segments.started_at_utc",
        "scheduler_uptime_segments.last_beat_at_utc",
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

    「贴标签」和「按本地时间换算」在 UTC 主机上是同一个函数：把 ``replace(tzinfo=UTC)``
    写成 ``astimezone(UTC)``，在 CI 那台 UTC 的 Linux 上一点差别也看不出来。

    所以这里不去掰进程的本地时区——掰得动与否取决于操作系统（Windows 没有
    ``time.tzset()``），那样判据就落在「开发机恰好在 UTC+8」这种机器设置上，
    换一台设成 UTC 的机器会直接红。改成把**输入值**换成探针：谁按本地时间换算它，
    谁就当场炸掉，与主机时区无关。
    """
    naive = _NaiveLocalTimeProbe(2026, 8, 11, 19, 4, 5)

    result = UTCDateTime().process_result_value(naive, sqlite.dialect())

    assert result == datetime(2026, 8, 11, 19, 4, 5, tzinfo=UTC)
    assert result is not None and result.utcoffset() == timedelta(0)


def test_the_naive_probe_still_catches_a_local_time_conversion() -> None:
    """探针得是活的：它哑了，上面那条就退化成「在任何主机上都什么也没验」。"""
    with pytest.raises(AssertionError, match="本地时间"):
        _NaiveLocalTimeProbe(2026, 8, 11, 19, 4, 5).astimezone(UTC)

    with pytest.raises(AssertionError, match="本地时间"):
        _NaiveLocalTimeProbe(2026, 8, 11, 19, 4, 5).timestamp()


class _NaiveLocalTimeProbe(datetime):
    """一个 naive 时刻：凡是「拿本地时区去解释它」的动作都在这里当场炸掉。

    naive 的 ``datetime`` 只有两条路会去问本地时区——``astimezone()`` 和
    ``timestamp()``——两条都堵上，就把「贴标签 vs. 换算」这个判据从主机时区里摘了出来。
    贴完标签（``replace(tzinfo=UTC)``）之后它就是 aware 的，再换算是正当的，放行。
    """

    def astimezone(self, tz: tzinfo | None = None) -> datetime:
        if self.tzinfo is None:
            raise AssertionError(
                "naive 值被当成了本地时间：应当贴 UTC 标签（replace）而不是 astimezone 换算"
            )
        return super().astimezone(tz)

    def timestamp(self) -> float:
        if self.tzinfo is None:
            raise AssertionError("naive 值被当成了本地时间去取 POSIX 时间戳")
        return super().timestamp()


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

    SQLite 上这一列就是这串字符，读出来之后 ``UTCDateTime`` 直接给它贴 UTC 标签
    （见 ``process_result_value``）——**不换算**。所以多一个 ``+08:00`` 后缀、或者
    存成本地挂钟，读回来的时刻就整体错 8 小时，海盗每天 32 次的日界跟着挪位，
    而 SQLite 不会为此报任何错。

    ⚠️ 这条**必须**自己开一个 SQLite 库。断言的是 SQLite 把时刻存成什么样的一串
    字符；Postgres 上那一列是 ``TIMESTAMPTZ``，读回来是 ``datetime`` 而不是字符串，
    ``func.date`` 给的也是 ``date`` 对象——同一个断言到那边没有意义。Postgres 那侧
    的时区语义由本文件其余几条守。
    """
    session_factory = _sqlite_session_factory(tmp_path)
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
    engine: Engine = create_database_engine(scratch_database_url(tmp_path, "timestamps.db"))
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _sqlite_session_factory(tmp_path: Path) -> sessionmaker[Session]:
    """给「断言 SQLite 落盘长什么样」的那一条用，不跟随全局方言。"""
    engine: Engine = create_database_engine(f"sqlite:///{tmp_path / 'sqlite-text.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)
