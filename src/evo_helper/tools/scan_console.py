"""扫描任务的常驻控制台：全局快捷键启停 + 屏幕角落的状态窗。

    Alt+F8  开始扫描（可用 --start-key 换）
    Alt+F9  结束扫描（可用 --stop-key 换）

状态窗右键=停止、双击=退出、左键拖动。右键只停不启，是快捷键被别的程序占掉时的退路。

扫描期间游戏窗口一直占着前台，控制台窗口被压在后面看不见——没有这个状态窗，
「它还在不在跑、跑了多久」就只能靠猜。所以状态窗必须**置顶且不抢焦点**：
它一旦抢了焦点，扫描下一次点击就会打到它身上。

    python -m evo_helper.tools.scan_console
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

#: 扫描安全停止后隔多久自动续扫。安全停止多半是「用户正在用电脑」这类瞬时原因，
#: 退避重试比直接放弃合适；60 秒既不打扰人，也不会让机器长时间空转。
RETRY_BACKOFF_S = 60.0

#: 状态窗尺寸与离工作区右下角的边距（物理像素）。
WINDOW_SIZE = (200, 92)
WINDOW_MARGIN = 40

#: 状态窗刷新间隔。秒级计时器，200ms 足够跟手又不费电。
REFRESH_MS = 200

LOG_PATH = Path("var/logs/scan-priority.log")


class ScanState(Enum):
    """状态窗上直接显示这些字。"""

    IDLE = "已停止"
    RUNNING = "扫描中"
    BACKOFF = "等待重试"
    DONE = "已扫完"


class Process(Protocol):
    """`subprocess.Popen` 里这个模块用到的那一小部分。"""

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = ...) -> int: ...


def format_duration(seconds: float) -> str:
    """把秒数排成 `H:MM:SS`。跨小时的扫描很常见，所以小时位不省。"""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


@dataclass
class ScanSupervisor:
    """管扫描子进程的起停与自动续扫，并对外报状态。

    这里刻意不碰界面也不碰 Win32：起停逻辑是这个功能里唯一有分支的部分，
    把它单独摘出来才测得了。
    """

    launch: Callable[[], Process]
    clock: Callable[[], float] = time.monotonic
    backoff_s: float = RETRY_BACKOFF_S

    state: ScanState = ScanState.IDLE
    _process: Process | None = None
    _started_at: float | None = None
    _stopped_elapsed: float = 0.0
    _retry_at: float | None = None
    _restarts: int = 0

    # -- 对外操作 --------------------------------------------------------------

    def start(self) -> None:
        """开始扫描。已经在跑就什么都不做——快捷键会被手抖按两下。"""
        if self.state in {ScanState.RUNNING, ScanState.BACKOFF}:
            return
        self._started_at = self.clock()
        self._stopped_elapsed = 0.0
        self._restarts = 0
        self._spawn()

    def stop(self) -> None:
        """结束扫描。停在退避等待里也要能停——那也算「在岗」。"""
        if self.state is ScanState.IDLE:
            return
        self._stopped_elapsed = self.elapsed_s
        self._kill()
        self._retry_at = None
        self.state = ScanState.IDLE

    def poll(self) -> None:
        """由界面定时调用：收子进程的退出码，到点则续扫。"""
        if self.state is ScanState.RUNNING:
            self._poll_running()
        elif self.state is ScanState.BACKOFF:
            self._poll_backoff()

    # -- 状态 ------------------------------------------------------------------

    @property
    def elapsed_s(self) -> float:
        """持续工作时间。

        退避等待也算在内——那时扫描仍然「在岗」，只是在等机器空出来；
        把它排除掉会让一段被频繁打断的扫描看起来只干了几分钟活。
        """
        if self._started_at is None:
            return self._stopped_elapsed
        if self.state is ScanState.IDLE:
            return self._stopped_elapsed
        return self.clock() - self._started_at

    @property
    def status_line(self) -> str:
        text = self.state.value
        if self.state is ScanState.BACKOFF and self._retry_at is not None:
            text = f"{text} {max(int(self._retry_at - self.clock()), 0)}s"
        if self._restarts and self.state is not ScanState.IDLE:
            text = f"{text}· 续{self._restarts}"
        return text

    @property
    def running(self) -> bool:
        return self.state in {ScanState.RUNNING, ScanState.BACKOFF}

    # -- 内部 ------------------------------------------------------------------

    def _spawn(self) -> None:
        self._process = self.launch()
        self._retry_at = None
        self.state = ScanState.RUNNING

    def _poll_running(self) -> None:
        if self._process is None:  # pragma: no cover - 只有 start 才会进 RUNNING
            return
        code = self._process.poll()
        if code is None:
            return
        self._process = None
        if code == 0:
            # 扫描自己跑完了整段计划。这不是故障，别再续扫。
            self.state = ScanState.DONE
            self._stopped_elapsed = self.elapsed_s
            self._started_at = None
            return
        self._retry_at = self.clock() + self.backoff_s
        self.state = ScanState.BACKOFF

    def _poll_backoff(self) -> None:
        if self._retry_at is None or self.clock() < self._retry_at:
            return
        self._restarts += 1
        self._spawn()

    def _kill(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - 收不到退出码也不该让界面卡住
            pass


# -- 全局快捷键 ----------------------------------------------------------------

#: `RegisterHotKey` 的修饰键。
MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}

#: 不接受长按自动重复——按住不该反复触发。
MOD_NOREPEAT = 0x4000

#: 功能键 F1–F24 的虚拟键码是连续的。
_VK_F1 = 0x70

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

HOTKEY_START = 1
HOTKEY_STOP = 2

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
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:  # pragma: no cover - Win32 消息循环
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        for binding in self._bindings:
            modifiers, vk = parse_hotkey(binding.text)
            if user32.RegisterHotKey(None, binding.action, modifiers, vk):
                self.registered.append(binding)
            else:
                code = ctypes.GetLastError()
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


def scan_command() -> list[str]:
    """扫描子进程的命令行。用当前解释器，免得撞上系统 Python 缺依赖。"""
    return [sys.executable, "-u", "-m", "evo_helper.tools.scan_coordinates"]


def launch_scan() -> Process:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOG_PATH.open("a", encoding="utf-8")
    return subprocess.Popen(  # noqa: S603 - 命令行完全由本模块构造
        scan_command(),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


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
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return int(rect.right), int(rect.bottom)


def run_console(start_key: str, stop_key: str) -> int:  # pragma: no cover - 界面与 Win32
    import ctypes
    import tkinter as tk

    # 系统缩放 125%：不声明 DPI 感知，tkinter 拿到的是逻辑像素，窗口会摆错地方。
    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    supervisor = ScanSupervisor(launch=launch_scan)
    listener = HotkeyListener(
        [HotkeyBinding(HOTKEY_START, start_key), HotkeyBinding(HOTKEY_STOP, stop_key)]
    )
    listener.start()
    for binding, reason in listener.rejected:
        print(f"⚠ {format_hotkey(binding.text)} {reason}", flush=True)
    for binding in listener.registered:
        action = "开始" if binding.action == HOTKEY_START else "结束"
        print(f"  {format_hotkey(binding.text)} {action}", flush=True)
    if listener.rejected:
        print("  换个键：--stop-key ctrl+alt+f9；或右键点状态窗停止", flush=True)

    root = tk.Tk()
    root.title("EVO 扫描")
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

    tones = {
        ScanState.RUNNING: "#3fb950",
        ScanState.BACKOFF: "#d29922",
        ScanState.DONE: "#58a6ff",
        ScanState.IDLE: "#8b949e",
    }

    live = {binding.action: format_hotkey(binding.text) for binding in listener.registered}
    hint = " ".join(
        part
        for part in (
            f"{live[HOTKEY_START]}起" if HOTKEY_START in live else "",
            f"{live[HOTKEY_STOP]}停" if HOTKEY_STOP in live else "右键停",
        )
        if part
    )

    def tick() -> None:
        while True:
            try:
                hotkey = listener.events.get_nowait()
            except queue.Empty:
                break
            if hotkey == HOTKEY_START:
                supervisor.start()
            elif hotkey == HOTKEY_STOP:
                supervisor.stop()
        supervisor.poll()
        status.configure(text=supervisor.status_line, fg=tones[supervisor.state])
        timer.configure(text=format_duration(supervisor.elapsed_s))
        root.after(REFRESH_MS, tick)

    def quit_console(_event: Any = None) -> None:
        supervisor.stop()
        listener.stop()
        root.destroy()

    def stop_scan(_event: Any = None) -> None:
        """右键 = 停，**只停不启**。

        停是安全动作，必须在任何状态下都说得准。做成「切换」的话，在状态刚变过的
        那一瞬右键就会变成又起一轮扫描——实机上就撞见过：本想停，结果多起了一轮。
        """
        supervisor.stop()

    # 无边框窗口没有关闭按钮：双击退出，右键停止，左键拖动。
    root.bind("<Double-Button-1>", quit_console)
    root.bind("<Button-3>", stop_scan)
    for child in (status, timer):
        child.bind("<Double-Button-1>", quit_console)
        child.bind("<Button-3>", stop_scan)
    _make_draggable(root, (status, timer))
    # 快捷键提示放第三行：注册失败时，用户得知道现在到底该按什么。
    tk.Label(root, text=hint, font=("Microsoft YaHei UI", 8), bg="#0d1117", fg="#6e7681").pack(
        anchor="w", padx=10
    )

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

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-key", default=DEFAULT_START_KEY, help="开始扫描的全局快捷键")
    parser.add_argument("--stop-key", default=DEFAULT_STOP_KEY, help="结束扫描的全局快捷键")
    args = parser.parse_args(argv)
    for text in (args.start_key, args.stop_key):
        try:
            parse_hotkey(text)
        except HotkeyError as exc:
            parser.error(str(exc))
    print("扫描控制台已启动。右键状态窗=停止，双击=退出，左键可拖动。", flush=True)
    return run_console(args.start_key, args.stop_key)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
