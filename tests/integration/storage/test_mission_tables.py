"""调度器的三张表。

`mission_tasks` 三行固定（每种任务一行），`mission_runs` 一次子进程一行，
`scheduler_config` 单行。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import models as orm


def test_a_mission_task_row_round_trips(session_factory) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        session.add(
            orm.MissionTaskRow(
                kind="PIRATE",
                enabled=True,
                priority=0,
                params_json='{"radius": 10}',
                created_at_utc=datetime.now(UTC),
                updated_at_utc=datetime.now(UTC),
            )
        )
        session.commit()

    with session_factory() as session:
        row = session.scalar(select(orm.MissionTaskRow).where(orm.MissionTaskRow.kind == "PIRATE"))
        assert row is not None
        assert row.params_json == '{"radius": 10}'
        assert row.consecutive_failures == 0
        assert row.disabled_reason is None
        assert row.quota_exhausted_until_utc is None


def test_a_mission_run_row_records_how_it_ended(session_factory) -> None:  # type: ignore[no-untyped-def]
    started = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            orm.MissionRunRow(
                kind="SCAN",
                command="python -m evo_helper.tools.scan_coordinates",
                pid=4242,
                started_at_utc=started,
                log_path="var/logs/mission-scan.log",
            )
        )
        session.commit()

    with session_factory() as session:
        row = session.scalar(select(orm.MissionRunRow))
        assert row is not None
        # 还在跑：结束相关的列全空，页面据此判断「运行中」。
        assert row.ended_at_utc is None
        assert row.exit_code is None
        assert row.stopped_by is None


def test_scheduler_config_carries_the_tunables(session_factory) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        session.add(orm.SchedulerConfigRow(id=1))
        session.commit()

    with session_factory() as session:
        row = session.get(orm.SchedulerConfigRow, 1)
        assert row is not None
        assert row.pirate_daily_quota == 32
        assert row.min_dwell_seconds == 60
        assert row.report_grace_minutes == 30
        assert row.restart_cooldown_seconds == 300


def test_the_tier_thresholds_are_gone_from_the_config_row() -> None:
    """分档删干净了：这张单行配置表上不该再有那三列。

    列在、代码不在是最难查的那种不一致——有值、有默认值，看起来像还生效的
    配置，而实际上没有任何地方读它（用户口径 2026-08-13：分档功能可以移除）。
    迁移 `c1f70b8a26d4` 把它们 drop 掉，这条守住 ORM 这一侧不再声明它们。
    """
    columns = set(orm.SchedulerConfigRow.__table__.columns.keys())

    assert not {name for name in columns if name.startswith("tier_")}


# -- 三行任务与单行配置的初始化 ------------------------------------------------


def test_seeding_creates_one_row_per_chain_and_one_config(repository) -> None:  # type: ignore[no-untyped-def]
    """迁移里没有 `bulk_insert`，所以这几行现在没人保证存在。

    少一行不会报错，只会让那条链路凭空消失在调度台上。
    """
    repository.ensure_mission_rows(now_utc=datetime.now(UTC))

    kinds = [row.kind for row in repository.mission_tasks()]
    assert sorted(kinds) == ["BOT", "PIRATE", "RANKING", "SCAN"]
    assert repository.scheduler_config().pirate_daily_quota == 32


def test_seeding_puts_scan_last(repository) -> None:  # type: ignore[no-untyped-def]
    """扫描永远有活干，排在谁前面谁就永远轮不到。"""
    repository.ensure_mission_rows(now_utc=datetime.now(UTC))

    by_kind = {row.kind: row.priority for row in repository.mission_tasks()}
    assert by_kind["SCAN"] > max(by_kind["PIRATE"], by_kind["BOT"])


def test_only_the_read_only_chains_are_enabled_by_default(repository) -> None:  # type: ignore[no-untyped-def]
    """扫描不派遣，默认开着无害；两条攻击链路默认关着。

    与 `evo_bot.AUTO_ENABLED` 默认 False 同一个理由：装好就会派舰队不是好默认。
    """
    repository.ensure_mission_rows(now_utc=datetime.now(UTC))

    by_kind = {row.kind: row.enabled for row in repository.mission_tasks()}
    assert by_kind == {"PIRATE": False, "BOT": False, "SCAN": True, "RANKING": True}


def test_seeding_twice_does_not_duplicate_or_overwrite(repository) -> None:  # type: ignore[no-untyped-def]
    """每次开机都会调一遍。第二遍要是覆盖，用户拖出来的优先级每次重启都被抹掉。"""
    now = datetime.now(UTC)
    repository.ensure_mission_rows(now_utc=now)
    pirate = next(row.id for row in repository.mission_tasks() if row.kind == "PIRATE")
    repository.update_mission_task(pirate, priority=7)

    repository.ensure_mission_rows(now_utc=now)

    rows = repository.mission_tasks()
    assert len(rows) == 4
    assert next(row.priority for row in rows if row.kind == "PIRATE") == 7


def test_seeding_does_not_re_add_a_row_when_that_chain_already_has_two(repository) -> None:  # type: ignore[no-untyped-def]
    """开机补行的判据是「这条链路一行都没有」，不是「行数不对」。

    用户新建的第二个 bot 任务不该让下次开机又补一行出来——那样每重启一次就多
    一行，而且多出来的那一行还带着种子的默认参数。
    """
    from evo_helper.domain.models import Coordinate

    now = datetime.now(UTC)
    repository.ensure_mission_rows(now_utc=now)
    repository.create_mission_task(
        MissionKind.BOT,
        name="2 号星",
        priority=5,
        params_json="{}",
        origin=Coordinate(9, 250, 8),
        fleet_lines=2,
        now_utc=now,
    )

    repository.ensure_mission_rows(now_utc=now)

    assert [row.kind for row in repository.mission_tasks()].count("BOT") == 2


def test_seeded_rows_follow_the_global_origin_and_line_limit(repository) -> None:  # type: ignore[no-untyped-def]
    """种下来的三行**不填**出发星球与航线数。

    NULL 的含义是「用全局主星 / 用 `scheduler_config.fleet_line_limit`」。种一个
    值进去等于在仓储层替用户做主，还会让「改了 `EVO_HELPER_ORIGIN` 却不生效」
    变成一个查不出来的毛病——舰队会继续按上一个账号的星球算飞行时间。
    """
    repository.ensure_mission_rows(now_utc=datetime.now(UTC))

    for row in repository.mission_tasks():
        assert row.origin_galaxy is None
        assert row.origin_system is None
        assert row.origin_position is None
        assert row.fleet_lines is None
