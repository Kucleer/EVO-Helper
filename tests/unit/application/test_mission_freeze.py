"""配置固化记录：抄下来、写下去、读回来。

这份记录存在的理由是「事后能查」，所以这里守的全是**跨进程还认不认得出来**：
写进文件的那一行，换一个控制台进程读回来，必须还是同一套配置。记不住的记录
等于没记——而它记不住这件事，只有在出事之后要翻账时才会被发现。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evo_helper.application.mission_freeze import (
    MAX_REMEMBERED,
    FrozenTask,
    MissionConfigFreeze,
    MissionFreezeLog,
    freeze_now,
)
from evo_helper.domain.fleet_tier import TierThresholds
from evo_helper.domain.scheduler import MissionKind

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _task(kind: MissionKind, *, params: str = "{}", priority: int = 0) -> FrozenTask:
    return FrozenTask(kind=kind, enabled=True, priority=priority, params_json=params)


def _freeze(*tasks: FrozenTask, at: datetime = NOW) -> MissionConfigFreeze:
    return freeze_now(list(tasks), frozen_at_utc=at)


# -- 抄下来 ---------------------------------------------------------------------


def test_the_tasks_are_ordered_by_kind_not_by_priority() -> None:
    """两条记录里同一条链路必须落在同一格。

    次序跟着 priority 走的话，用户把 bot 拖到海盗前面之后，整张表会错位——
    看起来像是三条链路全改了，而实际只改了优先级那一项。
    """
    freeze = _freeze(
        _task(MissionKind.SCAN, priority=9),
        _task(MissionKind.BOT, priority=0),
        _task(MissionKind.PIRATE, priority=5),
    )

    assert [task.kind for task in freeze.tasks] == list(MissionKind)


def test_a_naive_moment_is_refused() -> None:
    """不带时区的时刻事后对不上是哪一分钟改的。"""
    with pytest.raises(ValueError, match="时区"):
        freeze_now([_task(MissionKind.SCAN)], frozen_at_utc=datetime(2026, 8, 11, 12, 0))


def test_a_missing_chain_reads_back_as_none() -> None:
    assert _freeze(_task(MissionKind.SCAN)).task(MissionKind.BOT) is None


# -- 写下去、读回来 -------------------------------------------------------------


def test_a_record_survives_a_console_restart(tmp_path: Path) -> None:
    """**这条记录的全部意义就在这里。**

    控制台重启后调度器一律停在「已停止」，内存里的一切都没了。固化记录要是
    也跟着没，那它就只能回答「刚才那一轮」，而用户来翻它的时候多半已经重启过。
    """
    path = tmp_path / "freezes.jsonl"
    MissionFreezeLog(path).append(
        _freeze(_task(MissionKind.PIRATE, params='{"radius": 8}', priority=3))
    )

    reloaded = MissionFreezeLog(path).latest()

    assert reloaded is not None
    assert reloaded.frozen_at_utc == NOW
    task = reloaded.task(MissionKind.PIRATE)
    assert task is not None
    assert task.params_json == '{"radius": 8}'
    assert task.priority == 3
    assert task.enabled is True


def test_records_are_appended_not_overwritten(tmp_path: Path) -> None:
    """一次「开始」一行。覆盖写的话，「改了什么」就永远只剩最后一次。"""
    path = tmp_path / "freezes.jsonl"
    log = MissionFreezeLog(path)
    log.append(_freeze(_task(MissionKind.PIRATE, params='{"radius": 5}')))
    log.append(_freeze(_task(MissionKind.PIRATE, params='{"radius": 8}'), at=NOW + timedelta(1)))

    moments = [record.frozen_at_utc for record in MissionFreezeLog(path).records()]
    assert moments == [NOW, NOW + timedelta(1)]


def test_the_records_read_oldest_first(tmp_path: Path) -> None:
    """「与上一次相比」要拿相邻两条比，次序反了就会把改动说反。"""
    path = tmp_path / "freezes.jsonl"
    log = MissionFreezeLog(path)
    for day in range(3):
        log.append(_freeze(_task(MissionKind.SCAN), at=NOW + timedelta(days=day)))

    records = log.records()

    assert [record.frozen_at_utc for record in records] == sorted(
        record.frozen_at_utc for record in records
    )


def test_a_hand_edited_broken_line_does_not_take_the_console_down(tmp_path: Path) -> None:
    """这个文件是给人看的，也就意味着它会被人编辑。

    一行手改坏了的记录不该让整台控制台起不来——那一行本来也只是历史表里的
    一格，而丢掉它的代价远小于打不开控制台。
    """
    path = tmp_path / "freezes.jsonl"
    MissionFreezeLog(path).append(_freeze(_task(MissionKind.SCAN)))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{ 这不是 JSON\n")
        handle.write('{"frozen_at_utc": "2026-08-11T12:00:00", "tasks": []}\n')  # 没有时区
        handle.write("\n")

    records = MissionFreezeLog(path).records()

    assert len(records) == 1
    assert records[0].frozen_at_utc == NOW


def test_the_tier_thresholds_survive_a_console_restart(tmp_path: Path) -> None:
    """阈值决定这一轮每一发用哪套预设，所以它也要写进这份记录。

    只留在 `mission_runs.command` 里是不够的：那是一轮一行的命令行，回答不了
    「点开始那一刻页面上填的是什么」。
    """
    path = tmp_path / "freezes.jsonl"
    MissionFreezeLog(path).append(
        freeze_now(
            [_task(MissionKind.BOT)],
            frozen_at_utc=NOW,
            tier_thresholds=TierThresholds(alpha_from=1500, beta_from=5000, gamma_from=9000),
        )
    )

    record = MissionFreezeLog(path).records()[0]

    assert record.tier_thresholds is not None
    assert record.tier_thresholds.edges == (1500, 5000, 9000)


def test_an_older_record_without_thresholds_reads_back_as_unrecorded() -> None:
    """已有的历史行没有这个字段。**不给它编一个默认值。**

    那几轮实际用的是当时写死在代码里的 2K/5K/8K，回填一个今天的默认值会把
    记录变成一份看起来完整的假账——翻账的人会对着它找一个不存在的原因。
    """
    line = '{"frozen_at_utc": "2026-08-11T12:00:00+00:00", "tasks": []}'

    record = MissionConfigFreeze.from_json(line)

    assert record is not None
    assert record.tier_thresholds is None


def test_a_hand_broken_threshold_list_is_dropped_not_repaired() -> None:
    """手改坏的阈值一律读成「没记」，同样不回落到默认值。

    一条写着 2K/4K/8K 的记录必须真的来自那一轮的配置。
    """
    head = '{"frozen_at_utc": "2026-08-11T12:00:00+00:00", "tasks": []'
    for broken in ("[2000, 9000, 8000]", "[2000, 4000]", '[2000, "4000", 8000]', "2000"):
        record = MissionConfigFreeze.from_json(f'{head}, "tier_thresholds": {broken}}}')

        assert record is not None
        assert record.tier_thresholds is None


def test_a_memory_only_log_writes_no_file(tmp_path: Path) -> None:
    """测试与假服务那条路上不该往仓库里落文件。"""
    log = MissionFreezeLog()
    log.append(_freeze(_task(MissionKind.SCAN)))

    assert log.path is None
    assert log.latest() is not None
    assert list(tmp_path.iterdir()) == []


def test_a_write_failure_never_takes_the_start_button_down(tmp_path: Path) -> None:
    """账丢一条是遗憾，调度器起不来是事故。

    这里拿一个「父目录是文件」的路径制造必然失败的写入：`mkdir` 与 `open`
    都会 `OSError`，而 `append` 必须照样把记录留在内存里、不往上抛。
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("我不是目录", encoding="utf-8")
    log = MissionFreezeLog(blocker / "freezes.jsonl")

    log.append(_freeze(_task(MissionKind.SCAN)))

    assert log.latest() is not None


def test_the_memory_copy_is_bounded(tmp_path: Path) -> None:
    """一条一行不占地方，但内存里那份不能无限长。"""
    log = MissionFreezeLog()
    for minute in range(MAX_REMEMBERED + 5):
        log.append(_freeze(_task(MissionKind.SCAN), at=NOW + timedelta(minutes=minute)))

    records = log.records()
    assert len(records) == MAX_REMEMBERED
    # 砍掉的是老的那头，最近一次「开始」永远留得住。
    assert records[-1].frozen_at_utc == NOW + timedelta(minutes=MAX_REMEMBERED + 4)


def test_a_task_row_with_a_bogus_priority_is_dropped(tmp_path: Path) -> None:
    """`"priority": true` 会被当成优先级 1——`bool` 是 `int` 的子类。"""
    path = tmp_path / "freezes.jsonl"
    path.write_text(
        '{"frozen_at_utc": "2026-08-11T12:00:00+00:00", "tasks": ['
        '{"kind": "SCAN", "enabled": true, "priority": true, "params_json": "{}"}]}\n',
        encoding="utf-8",
    )

    latest = MissionFreezeLog(path).latest()

    assert latest is not None
    assert latest.tasks == ()
