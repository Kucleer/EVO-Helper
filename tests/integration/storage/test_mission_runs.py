"""子进程台账：起停记录、孤儿标记、连续失败自停。

这些是调度循环唯一的记忆。记漏一条，重启后的控制台就说不清上一次到底停在哪。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.scheduler import MissionKind

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def test_a_run_is_recorded_when_it_starts_and_closed_when_it_ends(repository) -> None:  # type: ignore[no-untyped-def]
    run_id = repository.begin_mission_run(
        MissionKind.SCAN,
        command=["python", "-m", "evo_helper.tools.scan_coordinates"],
        pid=4242,
        started_at_utc=NOW,
        log_path="var/logs/mission-scan.log",
    )

    repository.finish_mission_run(
        run_id, ended_at_utc=NOW + timedelta(minutes=3), exit_code=0, stopped_by="SELF"
    )

    row = repository.mission_runs(limit=10)[0]
    assert row.pid == 4242
    assert row.exit_code == 0
    assert row.stopped_by == "SELF"
    # 命令行原样存下来，事后翻账「那一轮到底打了谁」全靠它。
    assert "scan_coordinates" in row.command


def test_the_last_start_per_chain_is_what_the_cooldown_reads(repository) -> None:  # type: ignore[no-untyped-def]
    """冷却按**启动**算，所以取的是每种 kind 最新的 `started_at_utc`。"""
    for minutes in (30, 5):
        repository.begin_mission_run(
            MissionKind.PIRATE,
            command=["python"],
            pid=None,
            started_at_utc=NOW - timedelta(minutes=minutes),
            log_path="var/logs/mission-pirate.log",
        )
    repository.begin_mission_run(
        MissionKind.SCAN,
        command=["python"],
        pid=None,
        started_at_utc=NOW - timedelta(hours=1),
        log_path="var/logs/mission-scan.log",
    )

    starts = repository.last_mission_starts()

    assert starts[MissionKind.PIRATE] == NOW - timedelta(minutes=5)
    assert starts[MissionKind.SCAN] == NOW - timedelta(hours=1)
    assert MissionKind.BOT not in starts


def test_orphans_are_marked_unknown_rather_than_shot_by_pid(repository) -> None:  # type: ignore[no-untyped-def]
    """控制台重启时，`ended_at_utc` 为空的行说明上次没走正常的关闭路径。

    **不按 pid 自动杀**——pid 会被系统回收复用，照着一个可能已经换了主人的号码
    开枪比留个警告更糟。所以只标记，剩下的交给页面上的红条和「强制结束」。
    """
    repository.begin_mission_run(
        MissionKind.BOT,
        command=["python"],
        pid=9999,
        started_at_utc=NOW - timedelta(minutes=20),
        log_path="var/logs/mission-bot.log",
    )

    assert repository.mark_orphan_mission_runs(ended_at_utc=NOW) == 1

    row = repository.mission_runs(limit=1)[0]
    assert row.stopped_by == "UNKNOWN"
    # 结束时刻也补上，否则这一行永远显示成「运行中」。
    assert row.ended_at_utc == NOW
    assert row.pid == 9999


def test_a_closed_run_is_not_marked_as_an_orphan(repository) -> None:  # type: ignore[no-untyped-def]
    run_id = repository.begin_mission_run(
        MissionKind.SCAN,
        command=["python"],
        pid=None,
        started_at_utc=NOW - timedelta(minutes=20),
        log_path="var/logs/mission-scan.log",
    )
    repository.finish_mission_run(run_id, ended_at_utc=NOW, exit_code=0, stopped_by="USER")

    assert repository.mark_orphan_mission_runs(ended_at_utc=NOW) == 0


# -- 连续失败自停 --------------------------------------------------------------


def test_three_consecutive_failures_disable_the_task(repository) -> None:  # type: ignore[no-untyped-def]
    """没有这条，调度循环会在一个坏掉的任务上变成满速空转的重启循环。"""
    repository.ensure_mission_rows(now_utc=NOW)

    for _ in range(3):
        repository.record_mission_failure(MissionKind.PIRATE, exit_code=1, limit=3)

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.consecutive_failures == 3
    assert row.disabled_reason is not None


def test_two_failures_are_not_enough(repository) -> None:  # type: ignore[no-untyped-def]
    repository.ensure_mission_rows(now_utc=NOW)

    for _ in range(2):
        repository.record_mission_failure(MissionKind.PIRATE, exit_code=1, limit=3)

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.disabled_reason is None


def test_a_clean_exit_resets_the_streak(repository) -> None:  # type: ignore[no-untyped-def]
    """「连续」是连续。中间成功过一次，之前那两次就不该再算数。"""
    repository.ensure_mission_rows(now_utc=NOW)
    for _ in range(2):
        repository.record_mission_failure(MissionKind.PIRATE, exit_code=1, limit=3)

    repository.clear_mission_failures(MissionKind.PIRATE)
    repository.record_mission_failure(MissionKind.PIRATE, exit_code=1, limit=3)

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.consecutive_failures == 1
    assert row.disabled_reason is None


def test_disabling_for_bad_parameters_says_why(repository) -> None:  # type: ignore[no-untyped-def]
    """参数不合格是配置问题，重试一万次也一样。写清原因，页面上标红给人看。"""
    repository.ensure_mission_rows(now_utc=NOW)

    repository.disable_mission_task(MissionKind.BOT, reason="该范围内没有已记录的 bot；先跑扫描")

    row = next(item for item in repository.mission_tasks() if item.kind == "BOT")
    assert row.disabled_reason == "该范围内没有已记录的 bot；先跑扫描"
