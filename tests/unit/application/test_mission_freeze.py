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


def test_a_record_written_before_the_tier_thresholds_were_removed_still_reads(
    tmp_path: Path,
) -> None:
    """**旧行必须照样读得出来。**

    `var/mission-config-freezes.jsonl` 里已经写进过 `tier_thresholds`
    （PR #105 加的字段，2026-08-13 随分档删掉）。这份记录的**全部用意**就是
    事后知道当时用的哪套参数——为了几个不认识的键把整行读成 `None`，
    等于把历史毁掉。

    这条盯的是 `from_json` 逐个 `data.get(...)` 取字段这个写法：改成
    「先校验键集合」「用 dataclass 直接反序列化」之类的做法都会让它变红。
    """
    path = tmp_path / "freezes.jsonl"
    legacy = (
        '{"frozen_at_utc": "2026-08-12T14:30:31+00:00", '
        '"tasks": [{"kind": "BOT", "enabled": true, "priority": 1, "params_json": "{}"}], '
        '"tier_thresholds": [1000, 4000, 8000]}'
    )
    path.write_text(legacy + "\n", encoding="utf-8")

    records = MissionFreezeLog(path).records()

    assert len(records) == 1
    task = records[0].task(MissionKind.BOT)
    assert task is not None
    assert (task.enabled, task.priority) == (True, 1)


def test_an_unknown_key_never_costs_the_whole_line() -> None:
    """同一条规则的一般形式：多出来的键一律无视，不是丢行的理由。"""
    line = (
        '{"frozen_at_utc": "2026-08-11T12:00:00+00:00", "tasks": [], '
        '"something_added_later": {"a": 1}}'
    )

    assert MissionConfigFreeze.from_json(line) is not None


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
