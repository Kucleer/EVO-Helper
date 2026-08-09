"""调度器的三张表。

`mission_tasks` 三行固定（每种任务一行），`mission_runs` 一次子进程一行，
`scheduler_config` 单行。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

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
