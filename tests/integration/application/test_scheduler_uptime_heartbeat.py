"""挂机心跳：调度器在 tick 里落拍。

⚠️ **这一份钉的是「指标不许说假话」**，四条：

1. 调度器**停着**的时候不落拍——用户按了停止、页面开着一整夜，那不是挂机。
2. 每 tick 落一拍就是一天 86,400 次 UPDATE，所以**必须按间隔限流**。
3. **进程被杀之后挂机时长不许继续涨**：这一段的右端是最后一拍，不是「现在」。
4. **落库失败不许把调度器弄死**：它是个观测指标。

时钟一律注入（`Clock`），一次都不读真实时钟——用例不许依赖本机环境。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.uptime import (
    HEARTBEAT_INTERVAL_S,
    MAX_HEARTBEAT_GAP_S,
    uptime_seconds,
)
from evo_helper.storage.overview import OverviewRepository

from .conftest import Clock, make_supervisor

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
BEAT = timedelta(seconds=HEARTBEAT_INTERVAL_S)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


@pytest.fixture
def overview(session_factory: sessionmaker[Session]) -> OverviewRepository:
    return OverviewRepository(session_factory)


class RecordingLog:
    """把 `record_system_log` 的调用记下来。签名与真的那一个一致。

    ⚠️ **刻意不复用 `test_line_shortage_recovery.RecordingLog`**：那一份把心跳写的
    行筛掉了（它服务的用例钉的是别的链路写了几条），拿来测心跳自己的留痕就永远是空的。
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.payloads: list[dict[str, object]] = []

    def __call__(self, level, source, message, *, payload=None, **_):  # type: ignore[no-untyped-def]
        self.messages.append(message)
        self.payloads.append(dict(payload or {}))


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


def _segments(overview: OverviewRepository) -> list[tuple[datetime, datetime]]:
    window = (NOW - timedelta(days=1), NOW + timedelta(days=1))
    return [
        (item.start, item.last_beat)
        for item in overview.uptime_segments(start=window[0], end=window[1])
    ]


def test_a_stopped_scheduler_does_not_beat(  # type: ignore[no-untyped-def]
    scheduler, overview
) -> None:
    """⚠️ **停着的调度器不算挂机。**

    这个指标问的是「调度器开着多久」，不是「控制台的进程活着多久」。用户按了
    「停止」之后把页面挂一整夜，那段时间一发都不会派出去——把它算进挂机时长，
    「利用率为什么低」这个问题就又没人回答了。
    """
    scheduler.tick()

    assert _segments(overview) == []


def test_the_first_tick_after_start_opens_a_segment(  # type: ignore[no-untyped-def]
    scheduler, overview
) -> None:
    scheduler.start()

    scheduler.tick()

    assert _segments(overview) == [(NOW, NOW)]


def test_beats_are_throttled_so_a_tick_does_not_write_every_second(  # type: ignore[no-untyped-def]
    scheduler, overview, clock
) -> None:
    """⚠️ tick 是每秒一次的。每次都写就是一天 86,400 次 UPDATE。

    先例是 `record_unrecognised_screen` 那 120 秒（CLAUDE.md：每 tick 可能触发的
    都要限流）。
    """
    scheduler.start()
    scheduler.tick()

    for seconds in range(1, 10):
        clock.now = NOW + timedelta(seconds=seconds)
        scheduler.tick()

    # 一段，而且末端还停在第一拍上——这九个 tick 一次都没落库。
    assert _segments(overview) == [(NOW, NOW)]


def test_a_later_tick_pushes_the_same_segment_forward(  # type: ignore[no-untyped-def]
    scheduler, overview, clock
) -> None:
    """过了一个间隔就把末端往前推，**还是同一段**。"""
    scheduler.start()
    scheduler.tick()

    clock.now = NOW + BEAT
    scheduler.tick()
    clock.now = NOW + 2 * BEAT
    scheduler.tick()

    assert _segments(overview) == [(NOW, NOW + 2 * BEAT)]


def test_a_gap_longer_than_the_limit_starts_a_new_segment(  # type: ignore[no-untyped-def]
    scheduler, overview, clock
) -> None:
    """⚠️ **机器睡了 / tick 卡了那阵不是挂机。**

    超过阈值的空档接进同一段，等于把关着的那阵算成开着。断成两段之后，中间
    那截空档在挂机时长里就是缺的——那正是它该有的样子。
    """
    scheduler.start()
    scheduler.tick()
    resumed = NOW + timedelta(seconds=MAX_HEARTBEAT_GAP_S + 60)

    clock.now = resumed
    scheduler.tick()

    assert _segments(overview) == [(NOW, NOW), (resumed, resumed)]


def test_a_killed_process_leaves_the_uptime_stopped_at_its_last_beat(  # type: ignore[no-untyped-def]
    scheduler, overview, clock
) -> None:
    """⚠️ **进程被杀的情形。** 崩溃时不会有人写「已停止」。

    这里跑满一小时的心跳，然后**再也不 tick**（＝进程被 kill）。十小时之后去读，
    挂机时长必须还是 1 小时。

    写成「起了就一直算到现在」（或者「没有结束时刻就算到 now」）的话，这里会
    算出 11 小时——而那个数会一直涨下去，永远不下来。
    """
    scheduler.start()
    scheduler.tick()
    for minute in range(1, 61):
        clock.now = NOW + timedelta(minutes=minute)
        scheduler.tick()
    killed_at = NOW + timedelta(minutes=60)

    much_later = killed_at + timedelta(hours=10)
    seconds = uptime_seconds(
        overview.uptime_segments(start=NOW - timedelta(days=1), end=much_later),
        observed_since=overview.first_uptime_beat(),
        window_start=NOW - timedelta(days=1),
        window_end=much_later,
    )

    assert seconds == 3600.0


def test_a_restart_opens_a_new_segment_instead_of_resuming_the_old_one(  # type: ignore[no-untyped-def]
    repository, launcher, overview
) -> None:
    """⚠️ **重启之后另开一段，不去库里把上一段接回来。**

    接回来会把控制台重启那几十秒算成挂机。宁可少算一拍，也不让这个数说大话。
    """
    first_clock = Clock(NOW)
    first = MissionScheduler(repository, make_supervisor(launcher, first_clock), clock=first_clock)
    first.prepare()
    first.start()
    first.tick()

    # 新进程（内存里什么都没有），三十秒后起来——比阈值近得多。
    second_clock = Clock(NOW + timedelta(seconds=30))
    second = MissionScheduler(
        repository, make_supervisor(launcher, second_clock), clock=second_clock
    )
    second.prepare()
    second.start()
    second.tick()

    assert _segments(overview) == [(NOW, NOW), (second_clock.now, second_clock.now)]


def test_a_failing_heartbeat_never_takes_the_scheduler_down(  # type: ignore[no-untyped-def]
    scheduler, launcher, monkeypatch
) -> None:
    """⚠️ **观测指标不许把调度器弄死。**

    心跳落库炸了（库锁了、盘满了）时 tick 必须照常走完。抛出去的话，一个纯观测
    的功能会把整台调度器停掉——那比丢掉一个指标糟得多。
    """

    def explode(**_: object) -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(scheduler._repository, "open_uptime_segment", explode)
    scheduler.start()

    scheduler.tick()  # 不抛就是通过。


# -- 留痕：只在「断过」那一刻写，第一段不写 --------------------------------------


def test_the_first_segment_of_a_process_is_not_worth_a_log_line(  # type: ignore[no-untyped-def]
    scheduler,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ 用户按「开始」、控制台刚起来都会走到这里，那**不是异常**。

    每次都写一行，等于给一条本来就看得见的事（`mission_runs`、页面上的运行态）
    再刷一份噪声——而噪声会把真正要看的那几条淹掉。
    """
    scheduler.start()
    scheduler.tick()

    assert recorded.messages == []


def test_a_broken_heartbeat_leaves_a_line_that_says_how_long_it_was_out(  # type: ignore[no-untyped-def]
    scheduler,
    clock,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ **断过才是状态跃迁，而且日志必须说出断了多久。**

    判据是 CLAUDE.md 那条：出事时能不能**只靠库里的日志**定位。挂机时长上会缺
    一截，这行日志要回答「缺的那截是从哪到哪、多长」——上一拍的时刻和秒数
    都得在 payload 里。
    """
    scheduler.start()
    scheduler.tick()
    gap = timedelta(seconds=MAX_HEARTBEAT_GAP_S + 600)
    clock.now = NOW + gap

    scheduler.tick()

    assert len(recorded.messages) == 1
    assert "挂机心跳" in recorded.messages[0]
    payload = recorded.payloads[0]
    assert payload["previous_beat_utc"] == NOW.isoformat()
    assert payload["gap_seconds"] == gap.total_seconds()
