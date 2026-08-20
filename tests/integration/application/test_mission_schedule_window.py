"""任务定时开启 / 定时关闭在真实调度循环里的接线。

判据本身在 `tests/unit/domain/test_schedule_window.py` 里已经钉死；这里守的是
**接线与副作用**——那两列有没有真的读进快照、到点有没有去动不该动的东西、
日志写没写、写了几条。接线错了不报错，只会静默地多跑一轮或者少跑一轮。

三条硬约束（用户口径 2026-08-17），每条各有一段：

1. **定时器绝不写 `enabled`**：那一列是用户的意志。
2. **到点不抢停**：只挡「开新的一轮」，正在跑的 runner 不碰。
3. **每 tick 现算**：判定不依赖任何内存定时器，进程重启后照样成立。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.mission_progress import STALL_TIMEOUT
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.scheduler import GAP_FILLERS, MissionKind
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


def task_id(repository: SqlAlchemyRepository, kind: MissionKind) -> int:
    return next(row.id for row in repository.mission_tasks() if row.kind == kind.value)


def row_of(repository: SqlAlchemyRepository, kind: MissionKind):  # type: ignore[no-untyped-def]
    return next(row for row in repository.mission_tasks() if row.kind == kind.value)


def only(repository: SqlAlchemyRepository, kept: MissionKind, **fields: object) -> None:
    """只留 `kept` 一个任务参与调度，其余全关掉。

    填空隙的那几种（扫描 / 军力榜）尤其要关：它们永远有活干，留着的话
    「一个都没起」这类断言会看到一次它们的启动，而那与定时窗口无关。
    """
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == kept.value)
    repository.update_mission_task(task_id(repository, kept), **fields)  # type: ignore[arg-type]


# -- 到点开、到点关 ------------------------------------------------------------


def test_a_task_before_its_start_time_is_not_launched(scheduler, repository, launcher) -> None:  # type: ignore[no-untyped-def]
    only(repository, MissionKind.PIRATE, enabled_from_utc=NOW + timedelta(hours=1))
    scheduler.start()

    for _ in range(5):
        scheduler.tick()

    assert launcher.spawned == []


def test_the_task_starts_by_itself_once_the_clock_reaches_the_start_time(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """判定是每 tick **现算**的，不靠任何内存里的定时器。

    这一条是「重启后照样成立」那条约束在测试里握得住的形状：时钟一往前走，
    下一个 tick 就该起——中间没有任何人被通知过、也没有谁在等一个闹钟。
    """
    only(repository, MissionKind.PIRATE, enabled_from_utc=NOW + timedelta(hours=1))
    scheduler.start()
    scheduler.tick()
    assert launcher.spawned == []

    clock.now = NOW + timedelta(hours=1)
    scheduler.tick()

    assert launcher.kinds == [MissionKind.PIRATE]


def test_no_new_round_starts_after_the_stop_time(scheduler, repository, launcher, clock) -> None:  # type: ignore[no-untyped-def]
    only(repository, MissionKind.PIRATE, enabled_until_utc=NOW + timedelta(minutes=30))
    scheduler.start()

    clock.now = NOW + timedelta(hours=1)
    for _ in range(5):
        scheduler.tick()

    assert launcher.spawned == []


@pytest.mark.parametrize("kind", sorted(GAP_FILLERS, key=lambda item: item.value))
def test_gap_fillers_stop_at_their_window_too(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, kind: MissionKind
) -> None:
    """填空隙的那几种跟着停（用户口径 2026-08-17）。

    它们在判据里有一条「恒有活干」的早退，所以最容易成为唯一一个不受定时管的
    任务——而它们恰恰是那种会一直占着鼠标的链路。
    """
    only(repository, kind, enabled_until_utc=NOW + timedelta(minutes=30))
    scheduler.start()

    clock.now = NOW + timedelta(hours=1)
    for _ in range(5):
        scheduler.tick()

    assert launcher.spawned == []


# -- 绝不写 `enabled` ----------------------------------------------------------


def test_passing_the_stop_time_leaves_the_enabled_column_untouched(  # type: ignore[no-untyped-def]
    scheduler, repository, clock
) -> None:
    """到了关闭时刻，`mission_tasks.enabled` 一个字都不能动。

    ⚠️ 这条钉的是**库里那一列**，不是行为。改成「定时器直接写 `enabled`」的话，
    任务同样不跑、上面那些用例全绿，但用户的意志被悄悄改掉了：他自己勾上的那一下
    没了，而且事后翻库分不清是谁关的——用户关的和定时器关的在列上长得一模一样。
    再往后一步更糟：窗口过完之后就算把定时清掉，任务也不会自己回来。
    """
    only(repository, MissionKind.PIRATE, enabled_until_utc=NOW + timedelta(minutes=30))
    scheduler.start()

    clock.now = NOW + timedelta(hours=1)
    for _ in range(5):
        scheduler.tick()

    assert row_of(repository, MissionKind.PIRATE).enabled is True


def test_an_open_window_does_not_switch_a_task_the_user_turned_off_back_on(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """「与」的另一半：窗口开着也拉不起一个用户勾掉的任务。"""
    only(repository, MissionKind.PIRATE, enabled_from_utc=NOW + timedelta(minutes=30))
    repository.update_mission_task(task_id(repository, MissionKind.PIRATE), enabled=False)
    scheduler.start()

    clock.now = NOW + timedelta(hours=1)
    for _ in range(5):
        scheduler.tick()

    assert launcher.spawned == []
    assert row_of(repository, MissionKind.PIRATE).enabled is False


# -- 到点不抢停 ----------------------------------------------------------------


def test_the_running_child_survives_its_own_stop_time(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """关闭时刻到了，**正在跑的那一轮照跑**（用户口径 2026-08-17）。

    中途抢停会留下半截状态——runner 可能正停在派遣面板上——而且已经派出去的
    舰队本来也停不了。改成「到点抢停」的话，`terminate()` 会被调到那个假进程上，
    下面两条断言一起转红。

    ⚠️ 跑着的这个是 `SCAN`：**它是唯一可被抢占的那一种**。拿一个本来就抢不动的
    任务来测，等于什么都没测。

    ⚠️ 时钟只往前推 `STALL_TIMEOUT` 以内：「跑着不动」的看门狗到点也会把它掐掉
    （那是另一条既有规则），推过头的话这条断言会因为一个与定时窗口无关的原因转红。
    """
    only(repository, MissionKind.SCAN, enabled_until_utc=NOW + timedelta(minutes=5))
    scheduler.start()
    scheduler.tick()
    assert launcher.kinds == [MissionKind.SCAN]

    clock.now = NOW + timedelta(minutes=10)
    assert clock.now - NOW < STALL_TIMEOUT
    for _ in range(5):
        scheduler.tick()

    assert launcher.latest.terminated is False
    assert repository.mission_runs(limit=1)[0].ended_at_utc is None


def test_a_hungry_task_still_cannot_preempt_a_scan_whose_window_closed(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """窗口关掉不会凭空给别人制造出「值得抢占」的理由。

    上一条只有一个任务在场，就算实现真的去抢停，也可能被「没有别人可换」掩盖。
    这里让海盗同时有活干、且它的窗口也关着：抢占那一路完全够得着。
    """
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == MissionKind.SCAN.value)
    scheduler.start()
    scheduler.tick()
    assert launcher.kinds == [MissionKind.SCAN]

    # 起来之后才给两条链路配上窗口，免得扫描根本起不来。
    closes = NOW + timedelta(minutes=5)
    repository.update_mission_task(task_id(repository, MissionKind.SCAN), enabled_until_utc=closes)
    repository.update_mission_task(
        task_id(repository, MissionKind.PIRATE), enabled=True, enabled_until_utc=closes
    )

    # 同上：停在看门狗那条 60 分钟的线以内。
    clock.now = NOW + timedelta(minutes=10)
    for _ in range(5):
        scheduler.tick()

    assert launcher.latest.terminated is False
    assert launcher.kinds == [MissionKind.SCAN]


# -- 写 `system_log` -----------------------------------------------------------


class RecordingLog:
    """把 `record_system_log` 的调用记下来。签名与真的那一个一致。

    ⚠️ **挂机心跳写的行一律不收**（同 `test_line_shortage_recovery.RecordingLog`，
    理由整段写在那里）：这里钉的是「某一条链路写了几条」，而这几条用例都会把时钟
    往前跳十分钟，正好跳过心跳的断线阈值。心跳自己那份留痕由
    `test_scheduler_uptime_heartbeat.py` 钉着。
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.payloads: list[dict[str, object]] = []

    def __call__(self, level, source, message, *, payload=None, logged_at_utc=None, **_):  # type: ignore[no-untyped-def]
        if message.startswith("挂机心跳"):
            return
        self.messages.append(message)
        self.payloads.append(dict(payload or {}))


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


def test_reaching_the_stop_time_is_written_into_the_system_log(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, recorded: RecordingLog
) -> None:
    """到点关要留下一条，而且**只留一条**。

    tick 每秒一次；每 tick 刷一条的话一晚上就是几万行，真正要看的那一条会被淹掉。
    所以这里连 tick 五次，然后数条数。
    """
    only(repository, MissionKind.PIRATE, enabled_until_utc=NOW + timedelta(minutes=30))
    scheduler.start()
    for _ in range(5):
        scheduler.tick()
    # 本次运行第一次看到它，记的是现状（「在窗口内」），不是一次变化。
    assert len(recorded.messages) == 1
    assert recorded.payloads[0] == {**recorded.payloads[0], "window_open": True, "first_look": True}

    clock.now = NOW + timedelta(hours=1)
    for _ in range(5):
        scheduler.tick()

    assert len(recorded.messages) == 2
    assert "定时关闭" in recorded.messages[1]
    assert "不打断" in recorded.messages[1]
    assert recorded.payloads[1]["window_open"] is False
    assert recorded.payloads[1]["first_look"] is False


def test_reaching_the_start_time_is_written_into_the_system_log(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, recorded: RecordingLog
) -> None:
    only(repository, MissionKind.PIRATE, enabled_from_utc=NOW + timedelta(minutes=30))
    scheduler.start()
    for _ in range(5):
        scheduler.tick()
    assert len(recorded.messages) == 1
    assert recorded.payloads[0]["window_open"] is False

    clock.now = NOW + timedelta(hours=1)
    for _ in range(5):
        scheduler.tick()

    assert len(recorded.messages) == 2
    assert "定时开启" in recorded.messages[1]
    assert recorded.payloads[1]["window_open"] is True


def test_the_first_look_never_claims_a_moment_that_did_not_happen(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, recorded: RecordingLog
) -> None:
    """本次运行第一次看到时，措辞不能说成「到达定时开启时刻」。

    一个窗口从头到尾都开着的任务，控制台每重启一次就会留下那么一句——而那一刻
    什么都没发生。事后按这句话去对时间，对出来的是一个假的开启时刻。
    """
    only(repository, MissionKind.PIRATE, enabled_until_utc=NOW + timedelta(hours=8))
    scheduler.start()
    scheduler.tick()

    assert "到达定时开启时刻" not in recorded.messages[0]
    assert "定时窗口内" in recorded.messages[0]


def test_a_task_without_a_window_is_never_logged(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, recorded: RecordingLog
) -> None:
    """没配窗口的任务一条都不记。它们永远在窗口里，记了只是噪音。"""
    only(repository, MissionKind.PIRATE)
    scheduler.start()

    clock.now = NOW + timedelta(hours=1)
    for _ in range(5):
        scheduler.tick()

    assert recorded.messages == []
