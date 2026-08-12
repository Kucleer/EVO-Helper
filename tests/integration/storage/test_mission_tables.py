"""调度器的三张表。

`mission_tasks` 三行固定（每种任务一行），`mission_runs` 一次子进程一行，
`scheduler_config` 单行。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from evo_helper.domain.fleet_tier import DEFAULT_TIER_THRESHOLDS, TierThresholds
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
        # 用户口径（2026-08-11）。⚠️ 中间那道是 4000，不是原先写死的 5000。
        assert (row.tier_alpha_from, row.tier_beta_from, row.tier_gamma_from) == (
            2000,
            4000,
            8000,
        )


def test_tier_thresholds_round_trip_through_the_repository(repository) -> None:  # type: ignore[no-untyped-def]
    """页面保存 → 读回来是同一套。"""
    repository.ensure_mission_rows(now_utc=datetime.now(UTC))

    assert repository.tier_thresholds() == DEFAULT_TIER_THRESHOLDS

    repository.update_tier_thresholds(
        TierThresholds(alpha_from=1500, beta_from=5000, gamma_from=9000)
    )

    assert repository.tier_thresholds().edges == (1500, 5000, 9000)


def test_tier_thresholds_fall_back_to_the_defaults_on_a_fresh_database(repository) -> None:  # type: ignore[no-untyped-def]
    """配置行还没建出来时给默认值，不抛。

    控制台开机会补这一行，但 runner 是独立进程：手工跑一次 `tools.bot_loop`
    完全可能撞上一个还没被控制台碰过的库。那时按默认值分档，与新建库拿到的
    取值一致。
    """
    assert repository.tier_thresholds() == DEFAULT_TIER_THRESHOLDS


# -- 三行任务与单行配置的初始化 ------------------------------------------------


def test_seeding_creates_one_row_per_chain_and_one_config(repository) -> None:  # type: ignore[no-untyped-def]
    """迁移里没有 `bulk_insert`，所以这几行现在没人保证存在。

    少一行不会报错，只会让那条链路凭空消失在调度台上。
    """
    repository.ensure_mission_rows(now_utc=datetime.now(UTC))

    kinds = [row.kind for row in repository.mission_tasks()]
    assert sorted(kinds) == ["BOT", "PIRATE", "SCAN"]
    assert repository.scheduler_config().pirate_daily_quota == 32


def test_seeding_puts_scan_last(repository) -> None:  # type: ignore[no-untyped-def]
    """扫描永远有活干，排在谁前面谁就永远轮不到。"""
    repository.ensure_mission_rows(now_utc=datetime.now(UTC))

    by_kind = {row.kind: row.priority for row in repository.mission_tasks()}
    assert by_kind["SCAN"] > max(by_kind["PIRATE"], by_kind["BOT"])


def test_only_the_read_only_chain_is_enabled_by_default(repository) -> None:  # type: ignore[no-untyped-def]
    """扫描不派遣，默认开着无害；两条攻击链路默认关着。

    与 `evo_bot.AUTO_ENABLED` 默认 False 同一个理由：装好就会派舰队不是好默认。
    """
    repository.ensure_mission_rows(now_utc=datetime.now(UTC))

    by_kind = {row.kind: row.enabled for row in repository.mission_tasks()}
    assert by_kind == {"PIRATE": False, "BOT": False, "SCAN": True}


def test_seeding_twice_does_not_duplicate_or_overwrite(repository) -> None:  # type: ignore[no-untyped-def]
    """每次开机都会调一遍。第二遍要是覆盖，用户拖出来的优先级每次重启都被抹掉。"""
    now = datetime.now(UTC)
    repository.ensure_mission_rows(now_utc=now)
    repository.update_mission_task(MissionKind.PIRATE, priority=7)

    repository.ensure_mission_rows(now_utc=now)

    rows = repository.mission_tasks()
    assert len(rows) == 3
    assert next(row.priority for row in rows if row.kind == "PIRATE") == 7
