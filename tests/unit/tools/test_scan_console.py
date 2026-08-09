"""扫描控制台的起停与计时。界面和 Win32 不在这里测，逻辑分支全在这。"""

from __future__ import annotations

import pytest

from evo_helper.tools.scan_console import (
    MODIFIERS,
    RETRY_BACKOFF_S,
    WINDOW_MARGIN,
    WINDOW_SIZE,
    ScanState,
    ScanSupervisor,
    format_duration,
    window_position,
)


class FakeProcess:
    """可以按剧本「跑完」的假子进程。"""

    def __init__(self) -> None:
        self.exit_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = -1

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code if self.exit_code is not None else 0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(clock: Clock) -> tuple[ScanSupervisor, list[FakeProcess]]:
    spawned: list[FakeProcess] = []

    def launch() -> FakeProcess:
        process = FakeProcess()
        spawned.append(process)
        return process

    return ScanSupervisor(launch=launch, clock=clock), spawned


def test_starts_idle_and_reports_no_time() -> None:
    supervisor, spawned = make(Clock())
    assert supervisor.state is ScanState.IDLE
    assert supervisor.elapsed_s == 0
    assert spawned == []


def test_start_launches_the_scan() -> None:
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start()
    assert supervisor.state is ScanState.RUNNING
    assert len(spawned) == 1


def test_starting_twice_does_not_launch_a_second_scan() -> None:
    # 快捷键会被手抖按两下；起两个扫描进程会让它们互相抢游戏窗口。
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start()
    supervisor.start()
    assert len(spawned) == 1


def test_elapsed_counts_up_while_running() -> None:
    clock = Clock()
    supervisor, _ = make(clock)
    supervisor.start()
    clock.advance(75)
    assert supervisor.elapsed_s == 75
    assert format_duration(supervisor.elapsed_s) == "0:01:15"


def test_stop_terminates_the_scan_and_freezes_the_timer() -> None:
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start()
    clock.advance(30)
    supervisor.stop()

    assert spawned[0].terminated
    assert supervisor.state is ScanState.IDLE
    clock.advance(500)
    # 停了就不该继续走表——不然一夜过去会显示成干了一整晚活。
    assert supervisor.elapsed_s == 30


def test_a_safe_stop_backs_off_then_resumes() -> None:
    """安全停止多半是「用户正在用电脑」这类瞬时原因，退避重试比放弃合适。"""
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start()

    spawned[0].exit_code = 1
    supervisor.poll()
    assert supervisor.state is ScanState.BACKOFF
    assert len(spawned) == 1

    clock.advance(RETRY_BACKOFF_S - 1)
    supervisor.poll()
    assert supervisor.state is ScanState.BACKOFF

    clock.advance(2)
    supervisor.poll()
    assert supervisor.state is ScanState.RUNNING
    assert len(spawned) == 2


def test_backoff_still_counts_as_on_duty() -> None:
    # 退避时扫描仍在岗，只是在等机器空出来。排除掉的话，
    # 一段被频繁打断的扫描会看起来只干了几分钟活。
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start()
    spawned[0].exit_code = 1
    supervisor.poll()
    clock.advance(40)
    assert supervisor.elapsed_s == 40


def test_stop_works_while_waiting_to_retry() -> None:
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start()
    spawned[0].exit_code = 1
    supervisor.poll()
    supervisor.stop()
    assert supervisor.state is ScanState.IDLE
    assert not supervisor.running


def test_a_clean_finish_does_not_restart() -> None:
    """扫描把整段计划跑完是退出码 0。那不是故障，别再续扫。"""
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start()
    clock.advance(10)
    spawned[0].exit_code = 0
    supervisor.poll()

    assert supervisor.state is ScanState.DONE
    clock.advance(RETRY_BACKOFF_S * 3)
    supervisor.poll()
    assert len(spawned) == 1
    assert supervisor.elapsed_s == 10


def test_status_line_counts_the_restarts() -> None:
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start()
    assert supervisor.status_line == "扫描中"

    spawned[0].exit_code = 1
    supervisor.poll()
    assert supervisor.status_line.startswith("等待重试")

    clock.advance(RETRY_BACKOFF_S)
    supervisor.poll()
    assert "续1" in supervisor.status_line


def test_duration_keeps_the_hours_place() -> None:
    # 跨小时的扫描很常见，省掉小时位会把 3 小时显示成 0 分。
    assert format_duration(0) == "0:00:00"
    assert format_duration(59) == "0:00:59"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(-5) == "0:00:00"


def test_window_sits_inside_the_work_area() -> None:
    """贴的是工作区不是屏幕——贴屏幕底会被压在任务栏下面。"""
    left, top = window_position(work_right=1920, work_bottom=1030)
    width, height = WINDOW_SIZE
    # 贴右下角、留边距，就是这个函数的全部约定。不再断言具体的 1690——
    # 那只是把同一个算式抄了一遍，改个窗口尺寸就红，却什么都没多测到。
    assert left + width == 1920 - WINDOW_MARGIN
    assert top + height == 1030 - WINDOW_MARGIN
    assert (left, top) > (0, 0)


def test_hotkey_parsing_accepts_the_documented_forms() -> None:
    from evo_helper.tools.scan_console import MOD_NOREPEAT, parse_hotkey

    alt, ctrl_shift = MODIFIERS["alt"], MODIFIERS["ctrl"] | MODIFIERS["shift"]
    assert parse_hotkey("alt+f8") == (alt | MOD_NOREPEAT, 0x77)
    assert parse_hotkey("ALT+F9") == (alt | MOD_NOREPEAT, 0x78)
    assert parse_hotkey("ctrl+shift+f12") == (ctrl_shift | MOD_NOREPEAT, 0x7B)


def test_hotkey_parsing_rejects_what_it_cannot_register() -> None:
    from evo_helper.tools.scan_console import HotkeyError, parse_hotkey

    # 裸 F 键会和一堆程序打架，不如当场说清楚。
    for bad in ("f8", "alt+g", "alt+f0", "alt+f25", "", "meta+f8"):
        with pytest.raises(HotkeyError):
            parse_hotkey(bad)


def test_hotkey_label_is_readable() -> None:
    from evo_helper.tools.scan_console import format_hotkey

    assert format_hotkey("alt+f8") == "Alt+F8"
    assert format_hotkey("ctrl+shift+f12") == "Ctrl+Shift+F12"
