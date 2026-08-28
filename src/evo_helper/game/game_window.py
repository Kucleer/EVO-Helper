"""确保游戏窗口存在、尺寸正确，必要时自己重新拉起。

用户会随时关掉游戏、切换登录、或把窗口最大化。无人值守的运行必须能自己恢复，
而不是发现窗口不见了就失败。

标题必须**精确**匹配。本地控制台的页面标题是「情报中心 · EVO-Helper」，
按子串匹配 ``EVO`` 会命中它——那样就会把控制台的像素喂给游戏解析器，
而且看起来一切正常。这个坑已经踩过一次。
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from evo_helper.config import Settings

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

# -- 标定常量 ------------------------------------------------------------------
#
# 下面这三个是**同一次实机标定的产物，必须一起改**。任何一个单独动了，另外两个
# 就不再成立，而它们不成立的方式全是静默的：算出来的视口仍旧等于标定值、几何
# 校验仍旧通过，只是每个 ROI 都错位几像素。所有点击坐标和 OCR 的 ROI 也是这次
# 标定的产物，同样要跟着重新实测。

#: app 模式窗口里 Chrome 自绘的标题栏高度（物理像素，实测）。
#: 这个 38 是在 `CALIBRATED_SCALE_FACTOR` 下量的——Chrome 的 `--app` 标题栏跟
#: 页面用的是同一个 device scale factor。
APP_TITLE_BAR_PX = 38

#: 已标定的页面视口。布局几何只对这个尺寸成立。
CALIBRATED_VIEWPORT = (1920, 879)

#: 标定时的页面 DPR。所有坐标编码的是 `物理尺寸 ÷ 这个值` 得到的 CSS 版面
#: （1920x917 client ÷ 1.25 → 1536x703 CSS），不是物理尺寸本身。
CALIBRATED_SCALE_FACTOR = 1.25

#: 游戏窗口专用的 Chrome profile。**必须**是绝对路径，理由见 `launch_game`。
#: 落在 `var/` 下：那里已经在 .gitignore 里，而 profile 有几十 MB。
CHROME_PROFILE_DIR = Path(__file__).resolve().parents[3] / "var" / "chrome-profile"

#: 专用 profile 是全新的，所以 Chrome 的首次运行 UI 会冒出来。这些开关关掉它们。
#:
#: ⚠️ **`--disable-features=Translate` 实测关不掉右上角那个翻译气泡**
#: （Chrome 现行版本，2026-08-10 实机验过两次，换成 `Translate,TranslateUI`
#: 也一样）。保留它只是因为它是官方的 translate 特性开关。
#:
#: 现状与风险：气泡**不压任何现有 ROI**，也不会自己翻译——只有点了才会。
#: 但真被触发一次，页面文字整体改写，而认屏、关键词、OCR 全靠原文，
#: 所有判据会同时失效，现象是「哪儿都读不出来」，看不出是翻译干的。
#: 目前靠「首次登录时人工关掉，状态留在 profile 里」兜着。
#: 要彻底关，得在建 profile 时写 `Default/Preferences` 里的 `translate.enabled`
#: ——那是确定性的做法，但要单独做并单独验证，不该在别的改动里顺手塞。
FRESH_PROFILE_FLAGS = (
    "--disable-features=Translate",
    "--no-first-run",
    "--no-default-browser-check",
)

#: 系统级安装的两个落点。用户级那个要看环境变量，见 `chrome_candidates()`。
SYSTEM_CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


def chrome_candidates(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Chrome 的落点候选，按优先级排。

    **用户级安装（`%LOCALAPPDATA%`）才是安装器的默认选项**，只列 `Program Files`
    会在一台明明装着 Chrome 的机器上报「找不到 Chrome」，所以它排在最前。

    做成函数而不是模块常量，有两个理由：

    1. 常量会在 **import 那一刻**把环境变量烤进去。既测不了（CI 在 Linux 上跑，
       根本没有 `LOCALAPPDATA`），也会在环境变量后设时读到空值。
    2. `LOCALAPPDATA` 缺失时必须**整条略过**，不能拼出 `Path("")/"Google"/...`
       ——那是个**相对路径**，会去匹配当前工作目录下的同名文件。找 Chrome 找到
       工作目录里去，是那种查起来要命的错。
    """
    source = os.environ if env is None else env
    local = source.get("LOCALAPPDATA", "")
    if not local:
        return SYSTEM_CHROME_CANDIDATES
    user_level = Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"
    return (user_level, *SYSTEM_CHROME_CANDIDATES)


class GameWindowError(RuntimeError):
    """窗口拉不起来或尺寸调不到标定值时抛出，调用方应安全停止。"""


class ForegroundUnavailable(GameWindowError):
    """游戏窗口抢不到前台——多半是用户此刻正在用别的窗口。

    ⚠️ **单独成一个类型，是因为它的收场和别的窗口故障相反。**

    抢不到前台时 runner **什么都不做**：不关窗、不重开 Chrome、一次点击都不发
    （把点击打到别人窗口上比什么都不做糟得多）。它纯粹是让路——用户放开鼠标、
    切走那个窗口，下一轮就好了。这正是 `EXIT_ENVIRONMENT_BUSY` 那一档
    「会自己好」的准入条件，而且是它当初被设计出来时唯一的服务对象。

    在此之前它是个裸 `RuntimeError`，全仓一处都没 catch，于是抛穿 `main()`、
    按 Python 默认的退出码 1 收场——也就是被当成硬失败计进连续失败。
    实机 2026-08-17 凌晨那一阵环境故障里，三条链路每一轮都因此吃掉一次
    「多条一起倒」的豁免，26 分钟就把 6/6 的豁免上限攒满了。
    """


class GameWindowMissing(GameWindowError):
    """**窗口本该在，却不在了**——关窗重开有可能救得回来。

    ⚠️ **单独成一个类型，是为了把「重开能救」和「重开救不了」分开。**
    `GameWindowError` 底下的失败原先全长一个样，于是调用方只能二选一：要么一律
    重开（在标定不对、Chrome 没装、桌面上多出一个同名窗口的时候白关一次用户的
    窗口，而且下一轮照样失败），要么一律不重开——后者就是生产 2026-08-28 昨夜
    的形状：00:00 起窗口没了，六个任务每轮约 1 秒就 `exit=1`，
    `session_keeper` 整个故障时段**一行日志都没写**，一直到早上人工介入。

    ## 分档的判据：**同样的一步再走一遍，结论会不会不一样**

    重开这个动作本身就是「关掉窗口 →`ensure_game_window()` 重走一遍」。所以判据
    很实：**如果失败发生在那一步的前面（配置、Chrome 位置、DPR、桌面上有几个
    同名窗口），或者发生在窗口已经拿到之后的几何校验上，那么再走一遍必然得到
    同一个结论**，重开只是白关一次用户的窗口。

    归到这一档（重开能救）：

    - 「拉起游戏后 N 秒内没等到窗口出现」——Chrome 挂了 / 机器休眠 / 页面没画出来。
      这正是昨夜那一档。
    - 「调整尺寸时窗口消失了」「窗口在校验尺寸后消失了」——窗口在半路没的，
      跟上一条是同一件事的两个瞬间。

    **不**归到这一档（重开救不了，照旧抛）：

    - 「页面 DPR 配成了 X」——配置问题。`ensure_game_window` 第一句就再校验一次，
      重开必然倒在同一处。
    - 「找不到 Chrome」「配置里的 Chrome 路径不存在」——重开一样要 `chrome_path()`。
    - 「有 N 个标题为 EVO 的窗口」——`restart_game_window` 第一句 `find()` 就抛同一个
      错；而且它的消息本来就写着「请手动关掉多余的那个」，要的是人不是重试。
    - 「调了 N 次仍收敛不到标定 client」「窗口视口是 AxB，标定值是 CxD」——标定问题，
      新窗口在同一台机器上照样调不到。
    - 「已有窗口在加载游戏，但 N 秒内标题没变成 EVO」——`find_game_window()` 按
      **精确**标题匹配，那个窗口的标题还是域名，所以重开根本关不掉它，只会
      再等一遍同样的 180 秒。这一档的消息同样写着「请人工确认那个窗口的状态」。
    """


@dataclass(frozen=True)
class ViewportPlan:
    """目标页面视口与它对应的 client 尺寸。

    刻意**不**再往上算「窗口尺寸」：窗口比 client 大多少（边框）跟**系统 DPI**
    走，本机实测的那对常量换台机器就偏，一把设过去就调不到标定 client，
    于是 `ensure_game_window` 抛错、整条链路在那台机器上直接起不来。
    现在由 `resize_to_viewport` 量着调，见那里。
    """

    viewport: tuple[int, int] = CALIBRATED_VIEWPORT
    title_bar: int = APP_TITLE_BAR_PX

    @property
    def client(self) -> tuple[int, int]:
        return (self.viewport[0], self.viewport[1] + self.title_bar)

    def viewport_from_client(self, width: int, height: int) -> tuple[int, int]:
        return (width, height - self.title_bar)


def verified_scale_factor(value: float | None = None) -> float:
    """页面 DPR，**必须**等于 `CALIBRATED_SCALE_FACTOR`，否则抛错。

    `Settings.device_scale_factor` 存在的意义是「把这个值显式写出来」，不是
    「让人挑一个」。部署时会变的是机器，而钉死 DPR 的全部目的恰恰是让 CSS
    版面**不随机器变**——真要换个值，等于整套标定重来一遍。

    校验放在这一层而不是 `Settings` 里，是因为判据需要 `CALIBRATED_SCALE_FACTOR`，
    而它和 `APP_TITLE_BAR_PX`、`CALIBRATED_VIEWPORT` 是同一次标定的产物，只能住
    在一起。`config` 反过来 import 本模块会成环（本模块要读 Settings），把常量
    抄一份到 `config` 更糟——同一条判据两份实现，这个仓库栽过好几次。
    """
    scale = value if value is not None else Settings().device_scale_factor
    if scale != CALIBRATED_SCALE_FACTOR:
        raise GameWindowError(
            f"页面 DPR 配成了 {scale}，标定值是 {CALIBRATED_SCALE_FACTOR}。"
            "它不是偏好项，是标定常量：真要改，必须同时重新实测 "
            "APP_TITLE_BAR_PX、CALIBRATED_VIEWPORT，以及所有点击坐标与 ROI。"
            "照原样跑下去不会报错，只会让每次点击都偏几个像素。"
        )
    return scale


def chrome_path() -> Path:
    """Chrome 的位置：先看配置，再按候选列表找。

    配了却找不到时**抛错而不是回落**：回落的话用户以为跑的是自己指定的那个
    Chrome，实际跑的是另一个——差别要等到登录失败才看得出来。
    """
    configured = Settings().chrome_path
    if configured:
        explicit = Path(configured)
        if not explicit.is_file():
            raise GameWindowError(f"配置里的 Chrome 路径不存在：{explicit}")
        return explicit
    for candidate in chrome_candidates():
        if candidate.is_file():
            return candidate
    raise GameWindowError("找不到 Chrome；无法拉起游戏窗口")


def launch_game(
    url: str = GAME_URL,
    *,
    profile_dir: Path | None = None,
    scale_factor: float | None = None,
) -> None:
    """用 app 模式拉起游戏窗口，并把页面 DPR 钉死。

    必须是 ``--app``：普通窗口把标签栏、地址栏、书签栏画在 client area 之内，
    既让每张截图带上用户的标签页标题，也让页面视口的原点随书签栏显隐而漂移。

    ``--force-device-scale-factor`` 钉死 DPR。所有点击坐标和 ROI 编码的是
    「窗口物理尺寸 ÷ 缩放率」得到的 CSS 版面：同一个 1920x917 的物理窗口，
    在 125% 缩放的机器上按 1536x703 CSS 排版，在 100% 的机器上按 1920x879
    排版，版面完全不同、坐标全废。而 `ensure_game_window` 只校验物理尺寸，
    物理尺寸是对的——**几何校验通过、坐标全错**，这是最危险的那种失效。

    ``--user-data-dir`` **不是可选的隔离选项，删掉它上面那条就会静默失效**。
    Chrome 的命令行开关只对某个 profile 的**第一个**进程生效；用户的主 Chrome
    只要开着，这里的 `Popen` 就只是把 URL 转发给那个已有进程，
    `--force-device-scale-factor` 连看都不会看一眼。专用 profile 强制另起一个
    进程，开关才真的生效。代价是这个 profile 是空的，每台机器首次要重新登录
    一次游戏（之后就一直留着）——这个代价是用户确认过的。

    ``scale_factor`` 只是给测试留的显式入口，一样要过 `verified_scale_factor`
    的校验：留个能绕过校验的参数口子，等于没拦。
    """
    # 先校验再动手：拉起来之后才发现 DPR 不对，屏幕上就多了一个版面不对的
    # 游戏窗口，而 `find_game_window` 见到两个就彻底罢工，还得人工关窗口。
    scale = verified_scale_factor(scale_factor)
    profile = (profile_dir or CHROME_PROFILE_DIR).resolve()
    # 相对路径会跟着子进程的工作目录漂，每个 cwd 一个新 profile，
    # 表现出来像是「登录莫名其妙掉了」。
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(  # noqa: S603 - 路径来自固定候选列表
        [
            str(chrome_path()),
            f"--app={url}",
            f"--force-device-scale-factor={scale}",
            f"--user-data-dir={profile}",
            *FRESH_PROFILE_FLAGS,
        ],
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


class WindowDriver(Protocol):
    """`resize_to_viewport` 用到的三件事，抽出来是为了测得了。

    真窗口既不能在测试里移动也不能测量，所以这一层必须可替换。
    """

    def restore(self) -> None: ...

    def set_size(self, width: int, height: int) -> None: ...

    def measure_client(self) -> tuple[int, int]: ...


class _Win32Driver:
    """真窗口。"""

    def __init__(self, window: WindowInfo) -> None:
        self._handle = window.handle
        self._size = (0, 0)

    def restore(self) -> None:
        import win32con
        import win32gui

        if win32gui.GetWindowPlacement(self._handle)[1] != win32con.SW_SHOWNORMAL:
            win32gui.ShowWindow(self._handle, win32con.SW_SHOWNORMAL)
            time.sleep(1.2)

    def set_size(self, width: int, height: int) -> None:
        import win32gui

        win32gui.MoveWindow(self._handle, 0, 0, width, height, True)

    def measure_client(self) -> tuple[int, int]:
        from evo_helper.vision.optional.window_capture import client_box

        current = find_game_window()
        if current is None:  # pragma: no cover - 窗口在调整过程中被关掉
            # 窗口没了，不是几何不对：归「重开能救」那一档，见 `GameWindowMissing`。
            raise GameWindowMissing("调整尺寸时窗口消失了")
        box = client_box(current)
        return (box[2] - box[0], box[3] - box[1])


#: 量-调-复验最多来回几次。收敛不了就停：窗口有最小尺寸、会被贴边吸附、
#: 也可能压根调不动，无限逼近一个到不了的目标只会把链路挂在这里。
MAX_RESIZE_ATTEMPTS = 3

#: 每次 `MoveWindow` 之后等窗口稳定的时长。测的是稳定之后的 client，
#: 量早了会读到中间态，于是「差值」算出来是噪声，越调越偏。
RESIZE_SETTLE_S = 1.5


def next_window_size(
    target_client: tuple[int, int],
    measured_client: tuple[int, int],
    window_size: tuple[int, int],
) -> tuple[int, int]:
    """下一次该把窗口设成多大。

    纯函数，所以测得了——`MoveWindow` 和真实窗口测不了。
    做的事就是把这次 client 差了多少，原样补到窗口尺寸上：边框宽度是什么、
    跟不跟系统 DPI 走，都不需要知道，量出来就是了。
    """
    return (
        window_size[0] + (target_client[0] - measured_client[0]),
        window_size[1] + (target_client[1] - measured_client[1]),
    )


def resize_to_viewport(
    window: WindowInfo,
    plan: ViewportPlan | None = None,
    *,
    driver: WindowDriver | None = None,
    pause: Callable[[float], None] | None = None,
) -> tuple[int, int]:
    """把窗口调到标定视口，返回实际视口。

    最大化的窗口对 ``SetWindowPos`` 免疫，所以先还原再调——这一点踩过坑。

    **量着调，不假定边框宽度。** 窗口比 client 大多少跟系统 DPI 走（不跟
    Chrome 的 forced DPR 走），所以本机实测的那对常量换台机器就偏；照它一把
    设过去，client 就调不到 1920x917，`ensure_game_window` 直接抛错、代码在
    那台机器上根本起不来。改成先按 client 尺寸试一次，量出实际 client，按差
    值再设一次，直到复验相等——边框常量就整个不需要了。
    """
    target = plan or ViewportPlan()
    hardware = driver or _Win32Driver(window)
    wait = pause or time.sleep
    hardware.restore()

    # 第一次先按 client 尺寸设：边框恒为非负，第一发必然偏小，差多少下一轮补回来。
    size = target.client
    measured = (0, 0)
    for _ in range(MAX_RESIZE_ATTEMPTS):
        hardware.set_size(*size)
        wait(RESIZE_SETTLE_S)
        measured = hardware.measure_client()
        if measured == target.client:
            return target.viewport_from_client(*measured)
        size = next_window_size(target.client, measured, size)

    raise GameWindowError(
        f"调了 {MAX_RESIZE_ATTEMPTS} 次仍收敛不到标定 client "
        f"{target.client[0]}x{target.client[1]}，最后一次量到 "
        f"{measured[0]}x{measured[1]}；几何不符时拒绝继续采集"
    )


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
    # 窗口已经开着时走不到 `launch_game`，所以 DPR 的校验也要在这条路上做一次。
    # 「配错了 + 窗口恰好已经开着」是最常见的情形：用户先手动开了游戏，
    # 再启动助手；只拦 `launch_game` 的话，这种时候拦截整个失效。
    verified_scale_factor()
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
                # ⚠️ **这一句就是 2026-08-28 昨夜那一档**：Chrome 挂掉 / 机器休眠，
                # 拉不起来。归「重开能救」，理由整段在 `GameWindowMissing`。
                raise GameWindowMissing(f"拉起游戏后 {timeout_s:.0f}s 内没等到窗口出现")

    target = plan or ViewportPlan()
    actual = resize_to_viewport(window, target)
    if actual != target.viewport:
        raise GameWindowError(
            f"窗口视口是 {actual[0]}x{actual[1]}，标定值是 "
            f"{target.viewport[0]}x{target.viewport[1]}；几何不符时拒绝继续采集"
        )
    found = find_game_window()
    if found is None:  # pragma: no cover - 竞态
        # 同 `measure_client`：窗口没了，归「重开能救」那一档。
        raise GameWindowMissing("窗口在校验尺寸后消失了")
    return found


# -- 会话已死时的重开 -----------------------------------------------------------
#
# 页面自己写着「无法重新连接」的那一屏：点掉弹窗也回不去，只有整个页面重开一次
# 才可能恢复。见 `session_keeper.ScreenState.DEAD_SESSION`。

#: `PostMessageW` 的关窗消息号。
#:
#: **只关这一个窗口，绝不 `taskkill /im chrome.exe`。** 用户多半同时开着自己的
#: Chrome，而同一个可执行文件的所有窗口共享进程名——按进程名杀会把用户的标签页
#: 一起带走。`WM_CLOSE` 投到具体句柄上，效果等同于用户点了那个窗口的右上角 ×，
#: 别的 Chrome 窗口不受影响。
WM_CLOSE = 0x0010

#: 送出 `WM_CLOSE` 之后等窗口真的消失的上限与轮询间隔。
#:
#: **这一等不能省。** `PostMessageW` 只是把消息投进队列就返回，此刻窗口还在。
#: 不等它消失就去拉新窗口，屏幕上会同时留下两个游戏窗口，而 `find_game_window`
#: 见到两个就彻底罢工——整条链路从此起不来，还得人工关窗口。
WINDOW_CLOSE_TIMEOUT_S = 20.0
WINDOW_CLOSE_POLL_S = 0.5


class GameWindowLifecycle(Protocol):
    """`restart_game_window` 会去动系统的三件事，抽出来是为了测得了。

    ⚠️ **测试里绝不许真的开关窗口或改窗口尺寸。** 这层边界就是为此存在的：
    真实现只在 `_Win32Lifecycle` 里，测试注入假的。已经出过事——有改动在单元
    测试里直接调 `ensure_game_window()`，把用户真实的游戏窗口连拽了三次尺寸。
    新增任何「动系统」的步骤，都要先在这里开一个方法，不要在函数体里直接调。
    """

    def find(self) -> WindowInfo | None: ...

    def request_close(self, window: WindowInfo) -> None: ...

    def ensure(self) -> WindowInfo: ...


class _Win32Lifecycle:
    """真窗口。唯一会真的关窗、真的拉起 Chrome 的地方。"""

    def find(self) -> WindowInfo | None:
        return find_game_window()

    def request_close(self, window: WindowInfo) -> None:
        import ctypes

        # ctypes.windll 只在 Windows 上存在；动态取属性好让 Linux 上的 mypy 也能过。
        getattr(ctypes, "windll").user32.PostMessageW(window.handle, WM_CLOSE, 0, 0)

    def ensure(self) -> WindowInfo:
        return ensure_game_window()


def restart_game_window(
    *,
    lifecycle: GameWindowLifecycle | None = None,
    pause: Callable[[float], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> WindowInfo:
    """关掉游戏窗口再开一个，返回尺寸正确的新窗口。

    只给「会话已死」用：页面写着「无法重新连接」时，点掉弹窗回不去，
    页面本身已经没救了，唯一的出路是整个重开。这是**有代价的动作**
    （用户会看到窗口消失又出现，重开后还得重走入口序列），所以调用方必须
    真的读到那行字才来这里，并且有次数上限——见 `session_keeper`。

    中间**不插一次 `launch_game`**，拉起整个交给 `ensure_game_window` 自己做。
    手工恢复时是「关 → `launch_game` → `ensure_game_window`」三步，照抄进代码
    却有个竞态：`launch_game` 之后 Chrome 还没画出窗口，紧接着的
    `ensure_game_window` 既找不到 `EVO` 也找不到「正在加载」，于是**再拉一个**
    ——两个游戏窗口，`find_game_window` 罢工。`ensure_game_window` 本来就管
    「没窗口就拉起来」，让它一个人管这件事，这个竞态就不存在。
    """
    hardware = lifecycle or _Win32Lifecycle()
    wait = pause or time.sleep
    window = hardware.find()
    if window is not None:
        hardware.request_close(window)
        deadline = clock() + WINDOW_CLOSE_TIMEOUT_S
        while hardware.find() is not None:
            if clock() >= deadline:
                raise GameWindowError(
                    f"送出 WM_CLOSE 后 {WINDOW_CLOSE_TIMEOUT_S:.0f}s 内游戏窗口没有消失；"
                    "没有再拉一个（会留下两个窗口把链路彻底卡死），请人工确认那个窗口"
                )
            wait(WINDOW_CLOSE_POLL_S)
    return hardware.ensure()
