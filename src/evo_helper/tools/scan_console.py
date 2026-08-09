"""任务调度器的桌面瘦客户端：屏幕角落的状态窗 + 全局快捷键。

    Alt+F8  开始调度（可用 --start-key 换）
    Alt+F9  结束调度（可用 --stop-key 换）

状态窗右键=结束调度、双击=只关窗（调度器照跑）、左键拖动。右键只停不启，是快捷键
被别的程序占掉时的退路。双击不再顺带停调度器：旧版本里两者绑在一起，是因为那时
窗口一关子进程就没人管了；现在进程归调度器管，关掉状态窗只是不看了。

    python -m evo_helper.tools.scan_console

**这个模块一个进程都不起。** 它以前会自己拉起扫描 runner，那是全仓唯一的第二个
启动器。调度器上线后就成了两个互不知情的东西抢同一个鼠标：调度器以为只有自己在
派舰队，而 Alt+F8 还能另开一轮扫描。设计规格第八节第 1 条「任何时刻最多一个子进程
在点鼠标」（一个游戏窗口，一个鼠标）靠约定守不住，只能靠取消第二个启动器——
起进程那份职责现在整个在 `application/mission_supervisor.MissionSupervisor` 那边。
所以这里所有的动作都只是往 `POST /api/scheduler/{start,stop}` 发一个请求。

任务期间游戏窗口一直占着前台，浏览器里的控制台被压在后面看不见——没有这个状态窗，
「现在跑的是哪条链路、跑了多久」就只能靠猜。所以状态窗必须**置顶且不抢焦点**：
它一旦抢了焦点，runner 下一次点击就会打到它身上。

HTTP 走标准库 `urllib`，不用 `requests`：`requests` 在本仓是可选依赖，
而缺依赖导致的「未连接」和服务真的没起的「未连接」在这个 200×92 的小窗上长得
一模一样，用户只会去重启一台其实好好的服务。
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol, cast
from urllib.request import Request

from evo_helper.web.security import default_local_token

#: 状态窗尺寸与离工作区右下角的边距（物理像素）。
WINDOW_SIZE = (200, 92)
WINDOW_MARGIN = 40

#: 状态窗重画间隔。秒级计时器，200ms 足够跟手又不费电。
#: 这只是重画——问服务要状态是后台线程的事，见 `SchedulerPoller`。
REFRESH_MS = 200

#: 后台线程多久问一次调度器。页面上的秒表也是一秒一跳，对得上。
POLL_INTERVAL_S = 1.0

#: 单次请求的等待上限。本机回环，正常是毫秒级；这个数只用来兜住服务卡住的情况。
REQUEST_TIMEOUT_S = 3.0

#: 连着几次拿不到状态才认「未连接」。
#:
#: 一次问不到就翻脸是错的：调度器抢占扫描那一下会 `terminate()` 之后再 `wait(5)`，
#: 那几秒里状态问不出来，而它其实正在好好地派舰队。显示成断线会让用户去重启一台
#: 没坏的服务，甚至以为要自己动手补一轮。
OFFLINE_AFTER_MISSES = 3

#: 一句提示在第三行停留多久。过期后退回快捷键提示——注册失败时用户得知道该按什么。
NOTICE_TTL_S = 5.0

#: 连不上时按快捷键给的话。**只提示，不做任何事**：调度器可能其实正在跑、
#: 只是一时接不上，那时自己再起一个进程正是要防的双主人。
NOTICE_OFFLINE = "服务未启动"
#: 403：服务活着，但不认这个令牌。跟「服务没起」提示同一句话的话，
#: 用户会去重启一台没坏的服务，而真正该做的是对一下 `EVO_HELPER_WEB_TOKEN`。
NOTICE_DENIED = "令牌不符"


class ConsoleState(Enum):
    """状态窗第一行的四档。链路名由服务端下发，只在它为空时才退回这里的字。"""

    OFFLINE = "未连接"
    STOPPED = "已停止"
    IDLE = "待命"
    RUNNING = "运行中"


def format_duration(seconds: float) -> str:
    """把秒数排成 `H:MM:SS`。跨小时的任务很常见，所以小时位不省。"""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- 调度器接口 ----------------------------------------------------------------


class SchedulerProtocolError(ValueError):
    """服务回的东西不是一份调度器状态。"""


@dataclass(frozen=True)
class CurrentMission:
    """正在跑的那条链路。`label` 是服务端下发的中文名，这边不自己拼。"""

    kind: str
    label: str
    started_at_utc: datetime | None


@dataclass(frozen=True)
class SchedulerSnapshot:
    """`GET /api/scheduler` 里悬浮窗用得上的那三个字段。

    其余字段（`tasks`、`orphan_pid`）是页面的事：这个 200×92 的小窗放不下一张
    任务表，硬塞进来只会让两边对同一份数据各写一套解释。
    """

    running: bool
    started_at_utc: datetime | None = None
    current: CurrentMission | None = None


def parse_moment(value: object) -> datetime | None:
    """把接口给的时刻字符串译成带时区的 UTC 时刻。

    认不出来就返回 None 而不是抛错：秒表少一格，远好过整个状态窗因为一个时间字段
    黑掉——它唯一的用处就是在 runner 把游戏顶在前台时还能看见状态。
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    # 服务端下发的一律带时区；不带的按 UTC 认，免得减出一个差八小时的秒表。
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _parse_current(payload: object) -> CurrentMission | None:
    if not isinstance(payload, dict):
        return None
    kind = payload.get("kind")
    label = payload.get("label")
    return CurrentMission(
        kind=kind if isinstance(kind, str) else "",
        label=label if isinstance(label, str) else "",
        started_at_utc=parse_moment(payload.get("started_at_utc")),
    )


def parse_scheduler(payload: object) -> SchedulerSnapshot:
    """解 `GET /api/scheduler` 的回包。

    只有 `running` 缺失或不是布尔才算「这不是调度器状态」——那种情况多半是
    连到了别的服务，或者被中间的什么东西塞了一页 HTML 回来。其余字段一律容错：
    接口以后多一个字段、少一个字段，都不该让状态窗一片空白。
    """
    if not isinstance(payload, dict):
        raise SchedulerProtocolError(f"调度器状态不是一个对象：{type(payload).__name__}")
    running = payload.get("running")
    if not isinstance(running, bool):
        raise SchedulerProtocolError("调度器状态里没有 running，这不像是控制台")
    return SchedulerSnapshot(
        running=running,
        started_at_utc=parse_moment(payload.get("started_at_utc")),
        current=_parse_current(payload.get("current")),
    )


#: 监听所有网卡的写法。这些**不是能连的地址**，要连得回落到回环。
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "*"})


def scheduler_base_url(host: str, port: int) -> str:
    """控制台的地址。

    端口从 `config.Settings` 来（可被 `EVO_HELPER_PORT` 改掉），不写死。
    绑在通配地址上时连回环——`0.0.0.0` 是「监听所有网卡」，不是一个能连的地址。
    """
    dial = "127.0.0.1" if host in WILDCARD_HOSTS else host
    # IPv6 字面量在 URL 里必须加方括号，不然端口那个冒号分不出来。
    if ":" in dial:
        dial = f"[{dial}]"
    return f"http://{dial}:{port}"


class CommandResult(Enum):
    """一次「开始 / 结束」的下场。三档各自对应一句不同的提示。"""

    OK = "ok"
    DENIED = "denied"
    UNREACHABLE = "unreachable"


ACTION_START = "start"
ACTION_STOP = "stop"

_ACTION_PATHS = {ACTION_START: "/api/scheduler/start", ACTION_STOP: "/api/scheduler/stop"}

#: `RegisterHotKey` 的动作号。两个键控制的是**整个调度器**，等同于网页上的开始/结束。
HOTKEY_START = 1
HOTKEY_STOP = 2

#: 快捷键 → 动作。右键不在这张表里，它恒为 `ACTION_STOP`，见 `request_stop()`。
_HOTKEY_ACTIONS = {HOTKEY_START: ACTION_START, HOTKEY_STOP: ACTION_STOP}

Opener = Callable[[Request, float], bytes]


def _urlopen(request: Request, timeout: float) -> bytes:  # pragma: no cover - 真的去连服务
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return cast(bytes, response.read())


@dataclass
class SchedulerClient:
    """对着 `GET/POST /api/scheduler*` 说话。**它只发请求，不做任何决定。**

    `opener` 可注入，测试一律给假的：这个类唯一的副作用就是网络。
    """

    base_url: str
    token: str
    opener: Opener = _urlopen
    timeout_s: float = REQUEST_TIMEOUT_S

    def fetch(self) -> SchedulerSnapshot | None:
        """要一份状态。任何拿不到、看不懂的情况都返回 None（= 这一轮没答案）。

        故障不细分：读路径上唯一的下一步都是「先留着上一份状态，攒够次数再说
        未连接」，分出三种错来也不会做出不同的事。
        """
        try:
            body = self._call("GET", "/api/scheduler")
        except (urllib.error.URLError, OSError):
            return None
        try:
            return parse_scheduler(json.loads(body))
        except (ValueError, SchedulerProtocolError):
            # `json.JSONDecodeError` 是 `ValueError` 的子类，一并收在这里。
            return None

    def command(self, action: str) -> CommandResult:
        """发一次开始 / 结束。"""
        try:
            self._call("POST", _ACTION_PATHS[action])
        except urllib.error.HTTPError as exc:
            return CommandResult.DENIED if exc.code == 403 else CommandResult.UNREACHABLE
        except (urllib.error.URLError, OSError):
            return CommandResult.UNREACHABLE
        return CommandResult.OK

    def _call(self, method: str, path: str) -> bytes:
        request = Request(f"{self.base_url}{path}", method=method)  # noqa: S310 - 地址本模块拼
        # 悬浮窗是本机进程、不是浏览器，**没有 Origin 可言**，所以写请求只能走
        # 令牌那条路。令牌与服务端同源同解（`web.security.default_local_token`），
        # 两边各读一次同一个环境变量，不需要任何握手。
        request.add_header("X-Evo-Helper-Token", self.token)
        return self.opener(request, self.timeout_s)


# -- 后台轮询 ------------------------------------------------------------------


class SchedulerLike(Protocol):
    """`SchedulerPoller` 用到的那一小部分。"""

    def fetch(self) -> SchedulerSnapshot | None: ...

    def command(self, action: str) -> CommandResult: ...


class SchedulerPoller:
    """在自己的线程里问状态、代发开始/结束。

    HTTP 不能在 tkinter 线程里做。调度器抢占扫描那一下会 `terminate()` 之后再
    `wait(5)`，那几秒里 `GET /api/scheduler` 不回话——跟着一起卡住的是整个状态窗，
    连拖都拖不动。这和 `HotkeyListener` 另起线程是同一个理由：主循环只泵自己的事，
    结果一律经队列递回去。

    命令走队列而不是直接调：按下 Alt+F9 那一下如果同步等在界面线程里，
    用户看到的就是窗口先僵住再变字。塞进队列还顺带让它立刻醒过来，
    不用等下一个轮询周期。
    """

    _CLOSE = "__close__"

    def __init__(self, client: SchedulerLike, *, interval_s: float = POLL_INTERVAL_S) -> None:
        self._client = client
        self._interval_s = interval_s
        self._commands: queue.Queue[str] = queue.Queue()
        self.snapshots: queue.Queue[SchedulerSnapshot | None] = queue.Queue()
        self.outcomes: queue.Queue[CommandResult] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scheduler-poll", daemon=True)
        self._thread.start()

    def submit(self, action: str) -> None:
        self._commands.put(action)

    def close(self) -> None:
        self._commands.put(self._CLOSE)
        if self._thread is not None:
            self._thread.join(timeout=REQUEST_TIMEOUT_S + 1)

    def _run(self) -> None:
        while True:
            try:
                action = self._commands.get(timeout=self._interval_s)
            except queue.Empty:
                action = ""
            if action == self._CLOSE:
                return
            if action:
                self.outcomes.put(self._client.command(action))
            # 命令之后紧跟一次取状态：那一下的结果就是用户按键想看到的反馈。
            self.snapshots.put(self._client.fetch())


# -- 显示与按键 ----------------------------------------------------------------


@dataclass(frozen=True)
class ConsoleView:
    """一次重画要显示的全部内容。"""

    state: ConsoleState
    #: 第一行：链路名，或「未连接 / 已停止 / 待命」。
    text: str
    #: 第二行秒表。没有起始时刻时是空串，不摆一个静止的 0:00:00 当噪音。
    timer: str
    #: 第三行：临时提示，或常驻的快捷键提示。
    hint: str


@dataclass
class ConsoleController:
    """状态窗上显示什么、一次按键该发什么动作。

    tkinter 与 Win32 那两截测不了，所以判断全部收在这里；它不碰界面也不碰网络，
    输入只有「上一次轮询的结果」和「按了哪个键」。
    """

    clock: Callable[[], datetime] = _utc_now
    #: 常驻的快捷键提示。注册失败的键不会出现在里面，所以由界面那边拼好传进来。
    hint: str = ""
    offline_after: int = OFFLINE_AFTER_MISSES
    notice_ttl_s: float = NOTICE_TTL_S

    snapshot: SchedulerSnapshot | None = None
    misses: int = 0
    notice: str = ""
    notice_until: datetime | None = None

    @property
    def connected(self) -> bool:
        return self.snapshot is not None

    def absorb(self, snapshot: SchedulerSnapshot | None) -> None:
        """收一次轮询结果。None = 这一轮没答案。"""
        if snapshot is not None:
            self.snapshot = snapshot
            self.misses = 0
            return
        self.misses += 1
        if self.misses >= self.offline_after:
            self.snapshot = None

    def press(self, hotkey: int) -> str | None:
        """按下快捷键该发什么动作；连不上就只提示，什么都不发。"""
        action = _HOTKEY_ACTIONS.get(hotkey)
        if action is None:
            return None
        return self._issue(action)

    def request_stop(self) -> str | None:
        """右键 = 结束调度，**只停不启**。

        停是安全动作，必须在任何状态下都说得准。做成「切换」的话，在状态刚变过的
        那一瞬右键就会变成又起一轮——实机上撞见过：本想停，结果多起了一轮。
        所以这里不看 `snapshot.running`，恒发 stop。
        """
        return self._issue(ACTION_STOP)

    def report(self, outcome: CommandResult) -> None:
        """收一次开始/结束的下场。"""
        if outcome is CommandResult.OK:
            self.notice, self.notice_until = "", None
            return
        self._warn(NOTICE_DENIED if outcome is CommandResult.DENIED else NOTICE_OFFLINE)

    def view(self) -> ConsoleView:
        now = self.clock()
        state, text, since = self._state(now)
        timer = "" if since is None else format_duration((now - since).total_seconds())
        return ConsoleView(state=state, text=text, timer=timer, hint=self._hint(now))

    # -- 内部 ------------------------------------------------------------------

    def _issue(self, action: str) -> str | None:
        if not self.connected:
            self._warn(NOTICE_OFFLINE)
            return None
        return action

    def _warn(self, text: str) -> None:
        self.notice = text
        self.notice_until = self.clock() + timedelta(seconds=self.notice_ttl_s)

    def _state(self, now: datetime) -> tuple[ConsoleState, str, datetime | None]:
        snapshot = self.snapshot
        if snapshot is None:
            return ConsoleState.OFFLINE, ConsoleState.OFFLINE.value, None
        if not snapshot.running:
            # 停着的时候服务不给起始时刻，秒表自然是空的。
            return ConsoleState.STOPPED, ConsoleState.STOPPED.value, None
        current = snapshot.current
        if current is None:
            # 调度器在跑但手上没活：秒表退回它自己开机以来的时长。
            return ConsoleState.IDLE, ConsoleState.IDLE.value, snapshot.started_at_utc
        # 秒表走的是**这条链路**跑了多久，不是调度器的总时长——「现在这一轮跑了
        # 几分钟」才是要判断该不该去看一眼的那个数。
        label = current.label or ConsoleState.RUNNING.value
        return ConsoleState.RUNNING, label, current.started_at_utc

    def _hint(self, now: datetime) -> str:
        if self.notice and (self.notice_until is None or now < self.notice_until):
            return self.notice
        return self.hint


# -- 全局快捷键 ----------------------------------------------------------------

#: `RegisterHotKey` 的修饰键。
MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}

#: 不接受长按自动重复——按住不该反复触发。
MOD_NOREPEAT = 0x4000

#: 功能键 F1–F24 的虚拟键码是连续的。
_VK_F1 = 0x70

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

DEFAULT_START_KEY = "alt+f8"
DEFAULT_STOP_KEY = "alt+f9"


class HotkeyError(ValueError):
    """快捷键写法不认识。"""


def parse_hotkey(text: str) -> tuple[int, int]:
    """把 `alt+f8` 这样的写法译成 ``(修饰键, 虚拟键码)``。"""
    parts = [part.strip().lower() for part in text.split("+") if part.strip()]
    if not parts:
        raise HotkeyError(f"空的快捷键: {text!r}")
    key = parts[-1]
    if not (key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24):
        raise HotkeyError(f"只支持 F1–F24 作为主键，收到 {key!r}")
    modifiers = 0
    for name in parts[:-1]:
        if name not in MODIFIERS:
            raise HotkeyError(f"不认识的修饰键 {name!r}；可用：alt、ctrl、shift、win")
        modifiers |= MODIFIERS[name]
    if not modifiers:
        raise HotkeyError(f"{text!r} 没有修饰键；裸 F 键会和别的程序打架")
    return modifiers | MOD_NOREPEAT, _VK_F1 + int(key[1:]) - 1


def format_hotkey(text: str) -> str:
    """把用户写的快捷键排成好看的样子：`alt+f8` → `Alt+F8`。"""
    parts = [part.strip() for part in text.split("+") if part.strip()]
    return "+".join(
        part.upper() if part.lower().startswith("f") else part.capitalize() for part in parts
    )


@dataclass(frozen=True)
class HotkeyBinding:
    action: int
    text: str


class HotkeyListener:
    """在自己的线程里跑一个消息循环，收 `WM_HOTKEY`。

    `RegisterHotKey` 把消息投到**注册它的那个线程**的队列里，而 tkinter 的主循环
    只泵自己窗口的消息。所以快捷键必须有独立线程和独立消息循环，
    再通过队列把事件递给界面线程。

    **一个键被占用不该让整个控制台起不来**。实机上 `Alt+F9` 被 NVIDIA Overlay 占了
    （录像开关），当时的实现直接抛错退出，用户既用不了另一个能用的键，
    也不知道到底是哪个键出的问题。现在逐个注册、逐个报告。
    """

    def __init__(self, bindings: Sequence[HotkeyBinding]) -> None:
        self._bindings = tuple(bindings)
        self.events: queue.Queue[int] = queue.Queue()
        #: 注册成功的键，以及注册失败的键与原因。给界面和启动日志用。
        self.registered: list[HotkeyBinding] = []
        self.rejected: list[tuple[HotkeyBinding, str]] = []
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        import ctypes

        if self._thread_id is not None:
            # `ctypes.windll` 只在 Windows 上存在。动态取属性是为了让 CI 上
            # （Linux）的 mypy 也能过——仓库其余地方是同一个写法。
            getattr(ctypes, "windll").user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:  # pragma: no cover - Win32 消息循环
        import ctypes
        import ctypes.wintypes

        windll = getattr(ctypes, "windll")  # Windows 专有，见 `stop()` 的注释
        user32 = windll.user32
        self._thread_id = windll.kernel32.GetCurrentThreadId()
        for binding in self._bindings:
            modifiers, vk = parse_hotkey(binding.text)
            if user32.RegisterHotKey(None, binding.action, modifiers, vk):
                self.registered.append(binding)
            else:
                code = getattr(ctypes, "GetLastError")()
                reason = "已被其它程序占用" if code == 1409 else f"注册失败（错误码 {code}）"
                self.rejected.append((binding, reason))
        self._ready.set()
        try:
            message = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    self.events.put(int(message.wParam))
        finally:
            for binding in self.registered:
                user32.UnregisterHotKey(None, binding.action)


# -- 界面 ----------------------------------------------------------------------


def window_position(work_right: int, work_bottom: int) -> tuple[int, int]:
    """状态窗左上角坐标：贴工作区右下角，留出边距。

    用工作区而不是屏幕尺寸——屏幕底部那条是任务栏，贴屏幕底会被压在任务栏下面。
    """
    width, height = WINDOW_SIZE
    return (work_right - width - WINDOW_MARGIN, work_bottom - height - WINDOW_MARGIN)


def _work_area() -> tuple[int, int]:
    import ctypes

    class _Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = _Rect()
    # SPI_GETWORKAREA = 0x0030
    getattr(ctypes, "windll").user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return int(rect.right), int(rect.bottom)


#: 四档状态各自的颜色。「未连接」用红：它不是一种正常的停着。
TONES = {
    ConsoleState.RUNNING: "#3fb950",
    ConsoleState.IDLE: "#58a6ff",
    ConsoleState.STOPPED: "#8b949e",
    ConsoleState.OFFLINE: "#f85149",
}


def run_console(  # pragma: no cover - 界面与 Win32
    start_key: str, stop_key: str, *, base_url: str, token: str
) -> int:
    import ctypes
    import tkinter as tk

    # 系统缩放 125%：不声明 DPI 感知，tkinter 拿到的是逻辑像素，窗口会摆错地方。
    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    poller = SchedulerPoller(SchedulerClient(base_url=base_url, token=token))
    listener = HotkeyListener(
        [HotkeyBinding(HOTKEY_START, start_key), HotkeyBinding(HOTKEY_STOP, stop_key)]
    )
    listener.start()
    for binding, reason in listener.rejected:
        print(f"⚠ {format_hotkey(binding.text)} {reason}", flush=True)
    for binding in listener.registered:
        action = "开始调度" if binding.action == HOTKEY_START else "结束调度"
        print(f"  {format_hotkey(binding.text)} {action}", flush=True)
    if listener.rejected:
        print("  换个键：--stop-key ctrl+alt+f9；或右键点状态窗结束调度", flush=True)

    live = {binding.action: format_hotkey(binding.text) for binding in listener.registered}
    hint = " ".join(
        part
        for part in (
            f"{live[HOTKEY_START]}起" if HOTKEY_START in live else "",
            f"{live[HOTKEY_STOP]}停" if HOTKEY_STOP in live else "右键停",
        )
        if part
    )
    controller = ConsoleController(hint=hint)

    root = tk.Tk()
    root.title("EVO 调度")
    root.overrideredirect(True)  # 无边框：这是个状态灯，不是窗口
    root.attributes("-topmost", True)
    width, height = WINDOW_SIZE
    left, top = window_position(*_work_area())
    root.geometry(f"{width}x{height}+{left}+{top}")
    root.configure(bg="#0d1117")

    status = tk.Label(
        root, text="", font=("Microsoft YaHei UI", 11, "bold"), bg="#0d1117", fg="#8b949e"
    )
    status.pack(anchor="w", padx=10, pady=(7, 0))
    timer = tk.Label(root, text="", font=("Consolas", 14), bg="#0d1117", fg="#c9d1d9")
    timer.pack(anchor="w", padx=10)
    # 第三行平时是快捷键提示，出事时被临时提示顶掉。
    footer = tk.Label(root, text=hint, font=("Microsoft YaHei UI", 8), bg="#0d1117", fg="#6e7681")
    footer.pack(anchor="w", padx=10)

    def drain() -> None:
        """把三个队列各抽干。全在界面线程里做，因此不需要另加锁。"""
        while True:
            try:
                hotkey = listener.events.get_nowait()
            except queue.Empty:
                break
            action = controller.press(hotkey)
            if action is not None:
                poller.submit(action)
        while True:
            try:
                controller.absorb(poller.snapshots.get_nowait())
            except queue.Empty:
                break
        while True:
            try:
                controller.report(poller.outcomes.get_nowait())
            except queue.Empty:
                break

    def tick() -> None:
        drain()
        view = controller.view()
        status.configure(text=view.text, fg=TONES[view.state])
        timer.configure(text=view.timer)
        footer.configure(text=view.hint)
        root.after(REFRESH_MS, tick)

    def quit_console(_event: Any = None) -> None:
        """双击 = 关窗，**不停调度器**。这一条和旧版本不一样，不是漏改。

        以前双击会连子进程一起停掉，因为那时进程是这个窗口自己起的——窗口一关它就
        没人管了，只能在关之前先停。现在进程归 web 服务里的调度器管，活得比这个窗口
        久：关掉一个状态灯不该停掉整台调度器，那等于关遥控器就把电视关了。

        用户要的是「临时**开关快捷键**」，开关是快捷键，不是窗口的生死。停的入口
        两个都还在：Alt+F9 与右键。
        """
        listener.stop()
        poller.close()
        root.destroy()

    def stop_scheduler(_event: Any = None) -> None:
        action = controller.request_stop()
        if action is not None:
            poller.submit(action)

    # 无边框窗口没有关闭按钮：双击退出，右键结束调度，左键拖动。
    root.bind("<Double-Button-1>", quit_console)
    root.bind("<Button-3>", stop_scheduler)
    for child in (status, timer, footer):
        child.bind("<Double-Button-1>", quit_console)
        child.bind("<Button-3>", stop_scheduler)
    _make_draggable(root, (status, timer, footer))

    poller.start()
    tick()
    root.mainloop()
    return 0


def _make_draggable(root: Any, children: Sequence[Any] = ()) -> None:  # pragma: no cover - 界面
    origin = {"x": 0, "y": 0}

    def press(event: Any) -> None:
        origin["x"], origin["y"] = event.x_root - root.winfo_x(), event.y_root - root.winfo_y()

    def drag(event: Any) -> None:
        root.geometry(f"+{event.x_root - origin['x']}+{event.y_root - origin['y']}")

    for widget in (root, *children):
        widget.bind("<Button-1>", press)
        widget.bind("<B1-Motion>", drag)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 入口
    import argparse

    from evo_helper.config import Settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-key", default=DEFAULT_START_KEY, help="开始调度的全局快捷键")
    parser.add_argument("--stop-key", default=DEFAULT_STOP_KEY, help="结束调度的全局快捷键")
    args = parser.parse_args(argv)
    for text in (args.start_key, args.stop_key):
        try:
            parse_hotkey(text)
        except HotkeyError as exc:
            parser.error(str(exc))
    settings = Settings()
    base_url = scheduler_base_url(settings.host, settings.port)
    print(f"调度器状态窗已启动，连 {base_url}。", flush=True)
    print("右键状态窗=结束调度，双击=只关窗（任务照跑），左键可拖动。", flush=True)
    return run_console(
        args.start_key, args.stop_key, base_url=base_url, token=default_local_token()
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
