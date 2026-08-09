"""确保游戏窗口存在、尺寸正确，必要时自己重新拉起。

用户会随时关掉游戏、切换登录、或把窗口最大化。无人值守的运行必须能自己恢复，
而不是发现窗口不见了就失败。

标题必须**精确**匹配。本地控制台的页面标题是「情报中心 · EVO-Helper」，
按子串匹配 ``EVO`` 会命中它——那样就会把控制台的像素喂给游戏解析器，
而且看起来一切正常。这个坑已经踩过一次。
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evo_helper.vision.optional.window_capture import WindowInfo

GAME_URL = "https://eternal-void.online/"

#: app 模式下游戏窗口的标题恰好是这个。精确匹配，不做子串匹配。
GAME_WINDOW_TITLE = "EVO"

#: 页面把标题设成 `EVO` **之前**，窗口标题是游戏域名。
#:
#: 这是「正在加载」，不是「不存在」。分不清这两者的代价很大：页面一重连（实测会发生），
#: 标题就临时退回域名，此时按「窗口没了」去拉一个新的，就会多出**第二个**游戏窗口——
#: 而 `find_game_window()` 一见到两个就拒绝工作，扫描从此再也起不来，还得人工关窗口。
GAME_LOADING_TITLE = "eternal-void.online"

#: app 模式窗口里 Chrome 自绘的标题栏高度（物理像素，实测）。
APP_TITLE_BAR_PX = 38

#: 已标定的页面视口。布局几何只对这个尺寸成立。
CALIBRATED_VIEWPORT = (1920, 879)

#: 窗口边框：client = window - (宽 18, 高 9)（实测）。
_BORDER_W, _BORDER_H = 18, 9

CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


class GameWindowError(RuntimeError):
    """窗口拉不起来或尺寸调不到标定值时抛出，调用方应安全停止。"""


@dataclass(frozen=True)
class ViewportPlan:
    """把目标页面视口换算成窗口尺寸。"""

    viewport: tuple[int, int] = CALIBRATED_VIEWPORT
    title_bar: int = APP_TITLE_BAR_PX

    @property
    def client(self) -> tuple[int, int]:
        return (self.viewport[0], self.viewport[1] + self.title_bar)

    @property
    def window(self) -> tuple[int, int]:
        return (self.client[0] + _BORDER_W, self.client[1] + _BORDER_H)

    def viewport_from_client(self, width: int, height: int) -> tuple[int, int]:
        return (width, height - self.title_bar)


def chrome_path() -> Path:
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise GameWindowError("找不到 Chrome；无法拉起游戏窗口")


def launch_game(url: str = GAME_URL) -> None:
    """用 app 模式拉起游戏窗口。

    必须是 ``--app``：普通窗口把标签栏、地址栏、书签栏画在 client area 之内，
    既让每张截图带上用户的标签页标题，也让页面视口的原点随书签栏显隐而漂移。
    """
    subprocess.Popen(  # noqa: S603 - 路径来自固定候选列表
        [str(chrome_path()), f"--app={url}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _windows_titled(title: str) -> list[WindowInfo]:
    import win32gui

    from evo_helper.vision.optional.window_capture import WindowInfo

    found: list[WindowInfo] = []

    def _visit(handle: int, _acc: object) -> None:
        if not win32gui.IsWindowVisible(handle):
            return
        if win32gui.GetWindowText(handle).strip() != title:
            return
        found.append(WindowInfo(handle=handle, title=title, rect=win32gui.GetWindowRect(handle)))

    win32gui.EnumWindows(_visit, None)
    return found


def find_game_window() -> WindowInfo | None:
    """按**精确**标题找游戏窗口，找不到返回 None。"""
    found = _windows_titled(GAME_WINDOW_TITLE)
    if len(found) > 1:
        raise GameWindowError(
            f"有 {len(found)} 个标题为 {GAME_WINDOW_TITLE!r} 的窗口，无法判断用哪个。"
            "多半是先前把「正在加载」当成「窗口没了」又拉起了一个；请手动关掉多余的那个。"
        )
    return found[0] if found else None


def find_loading_game_window() -> WindowInfo | None:
    """找一个正在加载游戏的窗口（标题还是域名）。

    这里**不做**「只能有一个」的检查：多个加载中的窗口不影响判断——我们只用它回答
    「是不是已经有窗口在加载了」，答案是「是」就该等，而不是再拉一个。
    """
    found = _windows_titled(GAME_LOADING_TITLE)
    return found[0] if found else None


def resize_to_viewport(window: WindowInfo, plan: ViewportPlan | None = None) -> tuple[int, int]:
    """把窗口调到标定视口，返回实际视口。

    最大化的窗口对 ``SetWindowPos`` 免疫，所以先还原再调——这一点踩过坑。
    """
    import win32con
    import win32gui

    from evo_helper.vision.optional.window_capture import client_box

    target = plan or ViewportPlan()
    handle = window.handle
    if win32gui.GetWindowPlacement(handle)[1] != win32con.SW_SHOWNORMAL:
        win32gui.ShowWindow(handle, win32con.SW_SHOWNORMAL)
        time.sleep(1.2)

    win32gui.MoveWindow(handle, 0, 0, *target.window, True)
    time.sleep(1.5)

    current = find_game_window()
    if current is None:  # pragma: no cover - 窗口在调整过程中被关掉
        raise GameWindowError("调整尺寸时窗口消失了")
    box = client_box(current)
    return target.viewport_from_client(box[2] - box[0], box[3] - box[1])


#: 拉起窗口后等它出现的轮询上限。冷启动要开 Chrome、下载资源、初始化 WebGL，
#: 30s 不够——实测超时后果不是「重试一次」，而是多出一个再也关不掉的重复窗口。
LAUNCH_TIMEOUT_S = 120.0

#: 已经有窗口在加载时的等待上限。这条路径不拉新窗口，等久一点没有副作用。
LOAD_TIMEOUT_S = 180.0

LAUNCH_POLL_S = 1.0


def _wait_for_game_window(
    timeout_s: float, pause: Callable[[float], None], clock: Callable[[], float] = time.monotonic
) -> WindowInfo | None:
    deadline = clock() + timeout_s
    window = find_game_window()
    while window is None and clock() < deadline:
        pause(LAUNCH_POLL_S)
        window = find_game_window()
    return window


def ensure_game_window(
    *,
    plan: ViewportPlan | None = None,
    timeout_s: float = LAUNCH_TIMEOUT_S,
    sleep: Callable[[float], None] | None = None,
) -> WindowInfo:
    """返回一个尺寸正确的游戏窗口；窗口不存在就自己拉起来。

    用户随时会关掉游戏或切换登录，所以「窗口不见了」是正常情形，不是故障。
    但**页面重连时标题会临时退回域名**，那是「正在加载」——这时只能等，
    再拉一个会留下第二个游戏窗口，把扫描彻底卡死。
    尺寸调不到标定视口则抛错——几何不对时继续截图只会喂给解析器错位的 ROI。
    """
    pause = sleep or time.sleep
    window = find_game_window()
    if window is None:
        if find_loading_game_window() is not None:
            window = _wait_for_game_window(LOAD_TIMEOUT_S, pause)
            if window is None:
                raise GameWindowError(
                    f"已有窗口在加载游戏，但 {LOAD_TIMEOUT_S:.0f}s 内标题没变成 "
                    f"{GAME_WINDOW_TITLE!r}；没有再拉一个，请人工确认那个窗口的状态"
                )
        else:
            launch_game()
            window = _wait_for_game_window(timeout_s, pause)
            if window is None:
                raise GameWindowError(f"拉起游戏后 {timeout_s:.0f}s 内没等到窗口出现")

    target = plan or ViewportPlan()
    actual = resize_to_viewport(window, target)
    if actual != target.viewport:
        raise GameWindowError(
            f"窗口视口是 {actual[0]}x{actual[1]}，标定值是 "
            f"{target.viewport[0]}x{target.viewport[1]}；几何不符时拒绝继续采集"
        )
    found = find_game_window()
    if found is None:  # pragma: no cover - 竞态
        raise GameWindowError("窗口在校验尺寸后消失了")
    return found
