"""桌面悬浮窗：显示什么、按键发什么请求、连不上时怎么降级。

tkinter 与 Win32 那两截测不了，所以判断全部被摘成纯函数或可注入的小对象，
这个文件只测那一部分。

**这里最重要的一条不是显示逻辑，是「这个模块不能起进程」**——见
`test_the_console_has_no_way_to_start_a_process`。
"""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request

import pytest

from evo_helper.tools import scan_console
from evo_helper.tools.scan_console import (
    ACTION_START,
    ACTION_STOP,
    HOTKEY_START,
    HOTKEY_STOP,
    MODIFIERS,
    NOTICE_DENIED,
    NOTICE_OFFLINE,
    WINDOW_MARGIN,
    WINDOW_SIZE,
    CommandResult,
    ConsoleController,
    ConsoleState,
    CurrentMission,
    SchedulerClient,
    SchedulerPoller,
    SchedulerProtocolError,
    SchedulerSnapshot,
    format_duration,
    parse_scheduler,
    scheduler_base_url,
    window_position,
)

T0 = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class Clock:
    """可注入的时钟。秒表和提示语的有效期都靠它，真等秒数会让这一组测试变慢。"""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeOpener:
    """假的 HTTP 出口。记下每一次请求，按剧本回话或抛错。"""

    def __init__(self, body: object = None, error: Exception | None = None) -> None:
        self.body = body
        self.error = error
        self.calls: list[Request] = []

    def __call__(self, request: Request, timeout: float) -> bytes:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return json.dumps(self.body).encode("utf-8")


def running_payload(label: str = "侦查+攻击海盗") -> dict[str, object]:
    return {
        "running": True,
        "started_at_utc": "2026-08-09T11:00:00Z",
        "current": {
            "kind": "PIRATE",
            "label": label,
            "started_at_utc": "2026-08-09T11:58:00Z",
            "log_path": "var/logs/mission-pirate.log",
        },
        "orphan_pid": None,
        "tasks": [],
    }


def snapshot_running(label: str = "侦查+攻击海盗") -> SchedulerSnapshot:
    return parse_scheduler(running_payload(label))


def snapshot_idle() -> SchedulerSnapshot:
    return SchedulerSnapshot(running=True, started_at_utc=T0 - timedelta(seconds=90))


def snapshot_stopped() -> SchedulerSnapshot:
    return SchedulerSnapshot(running=False)


def make_controller(clock: Clock, snapshot: SchedulerSnapshot | None) -> ConsoleController:
    controller = ConsoleController(clock=clock, hint="Alt+F8起 Alt+F9停")
    if snapshot is not None:
        controller.absorb(snapshot)
    return controller


# -- 安全不变量 ----------------------------------------------------------------


def test_the_console_has_no_way_to_start_a_process() -> None:
    """安全不变量：任何时刻最多一个子进程在点鼠标（一个游戏窗口，一个鼠标）。

    这个模块以前是全仓唯一的第二个启动器。调度器上线后，两个互不知情的东西会抢
    同一个鼠标，而这条不变量靠约定守不住——只能靠这里一个进程都起不出来。
    所以查的是**能力**而不是某条分支的行为：连不上时退回「自己跑一轮扫描」的旧
    实现，正是最容易被当成「贴心降级」重新写回来的那一种。
    """
    source = Path(scan_console.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "subprocess",
        "Popen",
        "spawn",
        "os.system",
        "multiprocessing",
        "CreateProcess",
        "ShellExecute",
    ):
        assert forbidden not in source, f"悬浮窗里不该出现 {forbidden}"


def test_the_old_launcher_is_gone() -> None:
    # 起进程那份职责整个搬去了 application/mission_supervisor.MissionSupervisor。
    assert not hasattr(scan_console, "ScanSupervisor")
    assert not hasattr(scan_console, "launch_scan")


# -- 接口解析 ------------------------------------------------------------------


def test_parses_the_running_scheduler() -> None:
    snapshot = snapshot_running()
    assert snapshot.running
    assert snapshot.started_at_utc == datetime(2026, 8, 9, 11, 0, tzinfo=UTC)
    assert snapshot.current is not None
    # 链路名是服务端下发的，悬浮窗不自己拼——两处各写一份就会有一天对不上。
    assert snapshot.current.label == "侦查+攻击海盗"
    assert snapshot.current.started_at_utc == datetime(2026, 8, 9, 11, 58, tzinfo=UTC)


def test_parses_the_idle_and_stopped_shapes() -> None:
    idle = parse_scheduler({"running": True, "started_at_utc": "2026-08-09T11:00:00Z", "tasks": []})
    assert idle.running and idle.current is None

    stopped = parse_scheduler({"running": False, "started_at_utc": None, "current": None})
    assert not stopped.running
    assert stopped.started_at_utc is None


def test_an_unreadable_moment_does_not_take_the_window_down() -> None:
    # 秒表少一格远好过整个状态窗黑掉——它唯一的用处就是游戏占着前台时还能看见状态。
    snapshot = parse_scheduler({"running": True, "started_at_utc": "前天下午"})
    assert snapshot.running
    assert snapshot.started_at_utc is None


def test_a_payload_that_is_not_the_scheduler_is_rejected() -> None:
    for bad in ({}, [], "running", {"running": "yes"}, None):
        with pytest.raises(SchedulerProtocolError):
            parse_scheduler(bad)


def test_base_url_dials_the_loopback_when_the_service_binds_a_wildcard() -> None:
    """`0.0.0.0` 是「监听所有网卡」，不是一个能连的地址。"""
    assert scheduler_base_url("0.0.0.0", 8770) == "http://127.0.0.1:8770"
    assert scheduler_base_url("", 8770) == "http://127.0.0.1:8770"
    assert scheduler_base_url("::", 8770) == "http://127.0.0.1:8770"
    # 显式绑到某个地址时就照着连——回环未必通到那张网卡上。
    assert scheduler_base_url("127.0.0.1", 9001) == "http://127.0.0.1:9001"
    assert scheduler_base_url("192.168.1.9", 8770) == "http://192.168.1.9:8770"
    # IPv6 字面量在 URL 里必须加方括号，不然端口号那个冒号分不出来。
    assert scheduler_base_url("::1", 8770) == "http://[::1]:8770"


# -- HTTP 客户端 ---------------------------------------------------------------


def test_fetch_reads_the_scheduler() -> None:
    opener = FakeOpener(body=running_payload())
    client = SchedulerClient(base_url="http://127.0.0.1:8770", token="tok", opener=opener)

    snapshot = client.fetch()

    assert snapshot is not None and snapshot.running
    assert opener.calls[0].full_url == "http://127.0.0.1:8770/api/scheduler"
    assert opener.calls[0].get_method() == "GET"


def test_writes_carry_the_local_token() -> None:
    """悬浮窗是本机进程、不是浏览器，没有同源可言，所以写请求只能带令牌。"""
    opener = FakeOpener(body=running_payload())
    client = SchedulerClient(base_url="http://127.0.0.1:8770", token="tok", opener=opener)

    assert client.command(ACTION_START) is CommandResult.OK

    request = opener.calls[0]
    assert request.full_url == "http://127.0.0.1:8770/api/scheduler/start"
    assert request.get_method() == "POST"
    assert request.get_header("X-evo-helper-token") == "tok"


def test_stop_posts_to_the_stop_endpoint() -> None:
    opener = FakeOpener(body=running_payload())
    client = SchedulerClient(base_url="http://127.0.0.1:8770", token="tok", opener=opener)

    client.command(ACTION_STOP)

    assert opener.calls[0].full_url == "http://127.0.0.1:8770/api/scheduler/stop"


def test_a_refused_connection_reads_as_unreachable() -> None:
    opener = FakeOpener(error=urllib.error.URLError("connection refused"))
    client = SchedulerClient(base_url="http://127.0.0.1:8770", token="tok", opener=opener)

    assert client.fetch() is None
    assert client.command(ACTION_START) is CommandResult.UNREACHABLE


def test_a_wrong_token_is_told_apart_from_a_dead_service() -> None:
    """403 是「服务活着但不认这个令牌」。两者提示同一句话，用户会去重启一台没坏的服务。"""
    denied = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)  # type: ignore[arg-type]
    client = SchedulerClient(
        base_url="http://127.0.0.1:8770", token="tok", opener=FakeOpener(error=denied)
    )

    assert client.command(ACTION_START) is CommandResult.DENIED


def test_a_garbled_body_is_treated_as_no_answer() -> None:
    class Garbage:
        def __call__(self, request: Request, timeout: float) -> bytes:
            return b"<html>502 Bad Gateway</html>"

    client = SchedulerClient(base_url="http://127.0.0.1:8770", token="tok", opener=Garbage())
    assert client.fetch() is None


# -- 状态显示 ------------------------------------------------------------------


def test_a_running_chain_is_named_with_its_own_stopwatch() -> None:
    clock = Clock()
    view = make_controller(clock, snapshot_running()).view()

    assert view.state is ConsoleState.RUNNING
    assert view.text == "侦查+攻击海盗"
    # 秒表走的是**这条链路**跑了多久（11:58 起、现在 12:00），不是调度器的总时长。
    assert view.timer == "0:02:00"


def test_an_idle_scheduler_says_it_is_standing_by() -> None:
    view = make_controller(Clock(), snapshot_idle()).view()
    assert view.state is ConsoleState.IDLE
    assert view.text == "待命"
    assert view.timer == "0:01:30"


def test_a_stopped_scheduler_says_so_and_shows_no_stopwatch() -> None:
    view = make_controller(Clock(), snapshot_stopped()).view()
    assert view.state is ConsoleState.STOPPED
    assert view.text == "已停止"
    assert view.timer == ""


def test_before_the_first_answer_the_window_says_not_connected() -> None:
    view = make_controller(Clock(), None).view()
    assert view.state is ConsoleState.OFFLINE
    assert view.text == "未连接"


def test_one_missed_poll_does_not_declare_the_service_dead() -> None:
    """抢占扫描那一下会 `terminate()` 之后再 `wait(5)`，那几秒里状态问不出来。

    一次问不到就翻脸说「未连接」，用户看到的是一台其实正在派舰队的调度器显示成断线。
    """
    clock = Clock()
    controller = make_controller(clock, snapshot_running())

    for _ in range(scan_console.OFFLINE_AFTER_MISSES - 1):
        controller.absorb(None)
        assert controller.view().state is ConsoleState.RUNNING

    controller.absorb(None)
    assert controller.view().state is ConsoleState.OFFLINE


def test_a_fresh_answer_clears_the_missed_polls() -> None:
    controller = make_controller(Clock(), snapshot_running())
    controller.absorb(None)
    controller.absorb(snapshot_running())
    for _ in range(scan_console.OFFLINE_AFTER_MISSES - 1):
        controller.absorb(None)
    assert controller.view().state is ConsoleState.RUNNING


# -- 按键 ----------------------------------------------------------------------


def test_the_hotkeys_drive_the_whole_scheduler() -> None:
    controller = make_controller(Clock(), snapshot_running())
    assert controller.press(HOTKEY_START) == ACTION_START
    assert controller.press(HOTKEY_STOP) == ACTION_STOP


def test_right_click_only_ever_stops() -> None:
    """右键**只停不启**。

    做成「切换」的话，在状态刚变过的那一瞬右键就会变成又起一轮——实机上撞见过：
    本想停，结果多起了一轮。所以停着的时候右键也只发 stop，不发 start。
    """
    clock = Clock()
    assert make_controller(clock, snapshot_running()).request_stop() == ACTION_STOP
    assert make_controller(clock, snapshot_idle()).request_stop() == ACTION_STOP
    assert make_controller(clock, snapshot_stopped()).request_stop() == ACTION_STOP


def test_offline_keys_only_warn_and_send_nothing() -> None:
    """连不上就什么都不做。

    调度器可能其实正在跑、只是一时接不上；那时自己再起一个进程正是要防的双主人。
    """
    controller = make_controller(Clock(), None)

    assert controller.press(HOTKEY_START) is None
    assert controller.view().hint == NOTICE_OFFLINE

    assert controller.press(HOTKEY_STOP) is None
    assert controller.request_stop() is None


def test_an_unknown_hotkey_is_ignored() -> None:
    controller = make_controller(Clock(), snapshot_running())
    assert controller.press(999) is None


def test_a_refused_write_is_reported_then_fades() -> None:
    clock = Clock()
    controller = make_controller(clock, snapshot_running())

    controller.report(CommandResult.DENIED)
    assert controller.view().hint == NOTICE_DENIED

    clock.advance(scan_console.NOTICE_TTL_S + 1)
    # 提示过期后退回快捷键提示：注册失败时用户得知道现在该按什么。
    assert controller.view().hint == "Alt+F8起 Alt+F9停"


def test_a_successful_write_clears_the_warning() -> None:
    controller = make_controller(Clock(), snapshot_running())
    controller.report(CommandResult.UNREACHABLE)
    controller.report(CommandResult.OK)
    assert controller.view().hint == "Alt+F8起 Alt+F9停"


# -- 后台轮询 ------------------------------------------------------------------


class RecordingClient:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def fetch(self) -> SchedulerSnapshot | None:
        return snapshot_stopped()

    def command(self, action: str) -> CommandResult:
        self.commands.append(action)
        return CommandResult.OK


def test_the_poller_answers_off_the_ui_thread() -> None:
    """HTTP 不能在 tkinter 线程里做：服务忙那几秒会把整个状态窗连拖都拖不动。"""
    client = RecordingClient()
    poller = SchedulerPoller(client, interval_s=0.01)  # type: ignore[arg-type]
    poller.start()
    try:
        assert poller.snapshots.get(timeout=5) == snapshot_stopped()
        poller.submit(ACTION_STOP)
        assert poller.outcomes.get(timeout=5) is CommandResult.OK
        assert client.commands == [ACTION_STOP]
    finally:
        poller.close()


# -- 与界面无关的老约定（原样保留） --------------------------------------------


def test_duration_keeps_the_hours_place() -> None:
    # 跨小时的任务很常见，省掉小时位会把 3 小时显示成 0 分。
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


def test_current_mission_carries_the_server_label() -> None:
    # 服务端下发标签这条是接口契约的一部分；本地兜底只在标签为空时才用得上。
    mission = CurrentMission(kind="SCAN", label="", started_at_utc=T0)
    controller = ConsoleController(clock=Clock())
    controller.absorb(SchedulerSnapshot(running=True, started_at_utc=T0, current=mission))
    assert controller.view().text == ConsoleState.RUNNING.value
