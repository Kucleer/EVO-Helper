"""按优先级顺序逐坐标扫描，把 bot 位置写进数据库。

这是把 scratchpad 里的临时扫描脚本正式化的版本，接上了四件事：

- `domain.scan_plan` —— 已排好的优先级顺序（2:001–200 优先，9 系补末尾）+ 跳过 1–4 位
- `game.game_window.ensure_game_window` —— 窗口没了自己拉起来
- `game.session_keeper.SessionKeeper` —— 每 10 分钟巡检会话，掉线自己接回去
- `run_instances` 的持久化游标 —— 中断后从下一个坐标继续，不重扫也不跳过

**只读**：全程只有导航点击（输入框 / OK），没有任何派遣、领奖、删信路径；
`HumanInput` 会拒绝任何看起来像动作的标签。

    python -m evo_helper.tools.scan_coordinates --limit 200
    python -m evo_helper.tools.scan_coordinates --status
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import CoordinateScan
from evo_helper.domain.scan_bounds import ScanBounds
from evo_helper.domain.scan_plan import iter_scan_coordinates, planned_segments, total_coordinates
from evo_helper.game.system_navigator import NAV_LABEL_ROI, SystemNavigator, crop_reader
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.scan_reading import (
    COORD_WHITELIST,
    PlanetPanel,
    looks_like_mangled_bot,
    read_panel_confirming,
)

#: 这次全宇宙扫描用的计划名与幂等键。重跑同一个键就是续扫，不会新开一轮。
PLAN_NAME = "全宇宙优先级扫描"
RUN_KEY = "priority-scan-0001"

#: 出发星球（计划表要求非空）。扫描本身用不到它——扫描不派遣。
ORIGIN = Coordinate(2, 137, 18)
PRESET_NAME = "探路"
PRESET_SIGNATURE = "轻型战斗机:1"

#: 连续多少个坐标读不出坐标就停。单个读失败是噪声，连着失败说明画面已经不是面板了
#: （弹窗、维护公告、掉线），继续点下去就是在认不出的画面上乱点。
MAX_CONSECUTIVE_FAILURES = 5

#: 同一个坐标核对不过时重读几次。相邻重复数字会粘连（`2:2:11` 读成 `[2:2:1]`），
#: 只读一次就放弃会把这个坐标永久漏掉。
READ_ATTEMPTS = 3

#: 本次跑下来始终核对不过的坐标记在这里，供 `--rescan-missing` 或人工复核。
GAP_LOG = Path("var/logs/scan-gaps.txt")

#: **每个恒星系恰好一个 bot**（用户确认的游戏规则；实测 111/111 相符）。
#:
#: 据此在找到某系的 bot 之后跳过该系剩余行星位。bot 位号分布均匀，期望扫 8.5 位而不是 16 位，
#: 全宇宙耗时约减半（144 小时 → 约 76 小时）。
#:
#: 「已扫完」的判据因此**不是**「16 个位都入库」，而是「找到了 bot 或 16 个位都入库」。
#: 这条判据必须只有一份：扫描主循环和 `--rescan-missing` 的补缺口都用 `systems_with_bot()`，
#: 否则补缺口会把主循环故意跳过的坐标当成缺口，一遍遍重扫。
#:
#: `--scan-full-systems` 可关掉它，把这条假设变回可撤销的。
ONE_BOT_PER_SYSTEM = True

TESSERACT_PATH = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


# -- 输出 ---------------------------------------------------------------------


def say(message: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {message}", flush=True)


# -- 计划与游标 ----------------------------------------------------------------


def ensure_run(session_factory: Any) -> tuple[UUID, Coordinate | None]:
    """找到（或建好）这次扫描的运行实例，返回它和它的游标。"""
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    now = datetime.now(UTC)
    with session_factory() as session:
        run = session.scalar(
            select(orm.RunInstance).where(orm.RunInstance.idempotency_key == RUN_KEY)
        )
        if run is not None:
            cursor = _cursor_of(run)
            return run.id, cursor

        plan = session.scalar(select(orm.ScanPlan).where(orm.ScanPlan.name == PLAN_NAME))
        if plan is None:
            plan = orm.ScanPlan(
                name=PLAN_NAME,
                enabled=True,
                time_window_start="00:00",
                time_window_end="23:59",
                dry_run=True,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(plan)
            session.flush()
            # 一段一行，priority 就是扫描顺序——仓储层按 priority 取下一个坐标。
            for index, (_segment, start, end) in enumerate(planned_segments()):
                session.add(
                    orm.ScanRangeRow(
                        plan_id=plan.id,
                        start_galaxy=start.galaxy,
                        start_system=start.system,
                        start_position=start.position,
                        end_galaxy=end.galaxy,
                        end_system=end.system,
                        end_position=end.position,
                        origin_galaxy=ORIGIN.galaxy,
                        origin_system=ORIGIN.system,
                        origin_position=ORIGIN.position,
                        fleet_preset_name=PRESET_NAME,
                        fleet_preset_signature=PRESET_SIGNATURE,
                        priority=index,
                    )
                )
        run = orm.RunInstance(
            plan_id=plan.id,
            idempotency_key=RUN_KEY,
            state="SCANNING",
            started_at_utc=now,
            created_at_utc=now,
        )
        session.add(run)
        session.commit()
        return run.id, None


def _cursor_of(run: Any) -> Coordinate | None:
    if run.cursor_galaxy is None:
        return None
    return Coordinate(run.cursor_galaxy, run.cursor_system, run.cursor_position)


def save_cursor(session_factory: Any, run_id: UUID, coordinate: Coordinate) -> None:
    """游标只在一个坐标**读完并落库之后**才前进，所以中断最多重扫一个坐标。"""
    from evo_helper.storage import models as orm

    with session_factory() as session:
        run = session.get(orm.RunInstance, run_id)
        if run is None:  # pragma: no cover - 运行实例刚建好，不可能不在
            raise RuntimeError(f"运行实例不见了: {run_id}")
        run.cursor_galaxy = coordinate.galaxy
        run.cursor_system = coordinate.system
        run.cursor_position = coordinate.position
        session.commit()


def already_scanned(session_factory: Any) -> set[tuple[int, int, int]]:
    """先前已经确认过的坐标，续扫时跳过。

    早先那 71 条是另一个运行实例写的，按运行实例过滤会把它们重扫一遍。
    """
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with session_factory() as session:
        rows = session.execute(
            select(orm.BotTargetRow.galaxy, orm.BotTargetRow.system, orm.BotTargetRow.position)
        ).all()
    return {(row[0], row[1], row[2]) for row in rows}


# -- OCR ----------------------------------------------------------------------


def record_gap(coordinate: Coordinate, raw_text: str) -> None:
    """把始终核对不过的坐标追加到缺口清单。"""
    GAP_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    line = f"{stamp}\t{coordinate.galaxy}:{coordinate.system}:{coordinate.position}\t{raw_text!r}\n"
    with GAP_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)


def systems_with_bot(session_factory: Any) -> set[tuple[int, int]]:
    """已经找到 bot 的恒星系。每系只有一个，所以这些系不用再扫了。"""
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with session_factory() as session:
        rows = session.execute(
            select(orm.BotTargetRow.galaxy, orm.BotTargetRow.system).where(
                orm.BotTargetRow.is_bot.is_(True)
            )
        ).all()
    return {(row[0], row[1]) for row in rows}


def missing_from_plan(
    session_factory: Any, *, upto: Coordinate | None, one_bot_per_system: bool = ONE_BOT_PER_SYSTEM
) -> Iterator[Coordinate]:
    """计划里走到过、但库里没有的坐标。

    核对失败的坐标不会入库，游标却会被后面成功的坐标带过去。缺口清单是给人看的，
    这个函数是给机器补的——它直接对着「计划 vs 已入库」求差，不依赖任何日志。

    **已经找到 bot 的恒星系整个跳过**：那些位是主循环按规则故意不扫的，不是缺口。
    两处用同一个判据，否则补缺口会没完没了地重扫它们。
    """
    if upto is None:
        return
    done = already_scanned(session_factory)
    found = systems_with_bot(session_factory) if one_bot_per_system else set()
    for coordinate in iter_scan_coordinates():
        if (coordinate.galaxy, coordinate.system) not in found and (
            coordinate.galaxy,
            coordinate.system,
            coordinate.position,
        ) not in done:
            yield coordinate
        if coordinate == upto:
            return


def suspicious_names(session_factory: Any) -> list[Coordinate]:
    """库里那些「名字像 bot 但没判成 bot」的坐标。

    `bot_2_9_5` 读成 `botleao.-`，前缀糊了，于是那颗星球作为普通空位入了库——
    而它正是要找的东西。这类失败不报错，只能反查。
    """
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with session_factory() as session:
        rows = session.execute(
            select(
                orm.BotTargetRow.galaxy,
                orm.BotTargetRow.system,
                orm.BotTargetRow.position,
                orm.BotTargetRow.latest_owner_name,
            ).where(orm.BotTargetRow.is_bot.is_(False))
        ).all()
    return [Coordinate(row[0], row[1], row[2]) for row in rows if looks_like_mangled_bot(row[3])]


def make_ocr() -> Any:
    import pytesseract
    from PIL import Image

    pytesseract.pytesseract.tesseract_cmd = str(TESSERACT_PATH)

    filters = {"lanczos": Image.Resampling.LANCZOS, "nearest": Image.Resampling.NEAREST}

    def ocr(
        crop: Any,
        *,
        digits: bool,
        upscale: int,
        resample: str = "lanczos",
        threshold: int | None = None,
    ) -> str:
        grey = crop.convert("L")
        # 最近邻放大保住相邻 1 之间那道缝；LANCZOS 会把它插值糊掉。见 COORD_RECIPES。
        grey = grey.resize((grey.width * upscale, grey.height * upscale), filters[resample])
        if threshold is not None:
            # START 是半透明的大字压在星空上，不二值化读不出来。
            grey = grey.point(lambda value: 255 if value > threshold else 0)
        if digits:
            # 混合语言下开头的 2 会被读成 e；数字白名单能解决。
            config = f"--psm 7 -c tessedit_char_whitelist={COORD_WHITELIST}"
            return str(pytesseract.image_to_string(grey, lang="eng", config=config)).strip()
        return str(pytesseract.image_to_string(grey, lang="chi_sim+eng", config="--psm 7")).strip()

    return ocr


# -- 驱动 ----------------------------------------------------------------------


class LiveDriver:
    """真实鼠标 + 窗口截图。窗口不见了就自己拉起来并重新定位。"""

    def __init__(self, *, allow_actions: bool = False) -> None:
        """``allow_actions`` 默认关：扫描器只导航，不该有能力把舰队送出去。

        攻击链路要点「攻击」和「出发！」，必须在构造时显式打开——
        **开关只有这一处**，翻一眼构造点就知道哪个进程有动作能力。
        """
        from evo_helper.game.human_input import HumanInput, load_pyautogui

        self._gui = load_pyautogui()
        self._human = HumanInput(self._gui, allow_actions=allow_actions)

    def window(self) -> Any:
        """返回一个**可以往上点**的游戏窗口。

        最小化的窗口 `find_game_window()` 照样找得到，但它的 rect 是 (-32000, -32000)，
        照着算出来的点击坐标会落到屏幕角落——实测就是这样触发了 `pyautogui` 的急停。
        急停兜住了，可依赖急停等于把「点到桌面上」当成正常路径。这里先还原再用。
        """
        import win32gui

        from evo_helper.game.game_window import ensure_game_window, find_game_window

        found = find_game_window()
        if found is None or win32gui.IsIconic(found.handle):
            return ensure_game_window()
        return found

    def _raise_to_front(self, handle: int) -> None:
        """试一次抢前台。被系统拒绝是常事，所以吞掉异常交给外层重试。

        Windows 只允许「当前拥有前台的那个线程」换前台。先把自己的输入队列
        挂到前台线程上，`SetForegroundWindow` 才有资格成功——直接调用会被拒，
        而且 `GetLastError` 返回 0，异常里什么信息都没有。
        """
        import win32api
        import win32con
        import win32gui
        import win32process

        win32gui.ShowWindow(handle, win32con.SW_SHOW)
        attached = False
        target, _ = win32process.GetWindowThreadProcessId(handle)
        current = win32api.GetCurrentThreadId()
        try:
            if target != current:
                attached = bool(win32process.AttachThreadInput(current, target, True))
            win32gui.SetForegroundWindow(handle)
        except Exception:  # noqa: BLE001 - 抢前台被拒绝不是错误，是常态
            pass
        finally:
            if attached:
                try:
                    win32process.AttachThreadInput(current, target, False)
                except Exception:  # noqa: BLE001 - 解绑失败也不该中断扫描
                    pass

    def focus(self, *, attempts: int = 5) -> None:
        """把游戏窗口提到前台。

        **必须真的到前台**：别的窗口盖在上面时点击会落到那个窗口上，而截图走
        `PrintWindow` 照样是游戏画面——于是看起来像「游戏没响应」，实际是点错了地方。

        抢前台会被系统间歇性拒绝（用户正在别的窗口打字时尤其如此），这是常态不是故障，
        所以退避重试；每次都**回读** `GetForegroundWindow` 核实，不信返回值。
        """
        import win32gui

        handle = self.window().handle
        for attempt in range(attempts):
            if win32gui.GetForegroundWindow() == handle:
                return
            self._raise_to_front(handle)
            time.sleep(0.3 * (attempt + 1))
        if win32gui.GetForegroundWindow() != handle:
            raise RuntimeError(
                "游戏窗口抢不到前台（多半是用户正在用别的窗口）；停止而不是把点击打到别人窗口上"
            )

    def origin(self) -> tuple[int, int]:
        from evo_helper.vision.optional.window_capture import client_box

        box = client_box(self.window())
        return box[0], box[1]

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.focus()
        origin_x, origin_y = self.origin()
        self._human.click(origin_x + x, origin_y + y, label=label)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = "") -> None:
        """面板内拖动。预设栏一屏只放得下两个预设，其余的要横向拖出来。"""
        self.focus()
        origin_x, origin_y = self.origin()
        self._human.drag(
            origin_x + from_x,
            origin_y + from_y,
            origin_x + to_x,
            origin_y + to_y,
            label=label,
        )

    def type_number(self, value: int) -> None:
        import random

        self._gui.hotkey("ctrl", "a")
        time.sleep(random.uniform(0.15, 0.35))
        for char in str(value):
            self._gui.write(char)
            time.sleep(random.uniform(0.06, 0.18))
        time.sleep(random.uniform(0.2, 0.4))

    def capture(self) -> Any:
        from evo_helper.vision.optional.window_capture import capture_window

        return capture_window(self.window())

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)


# -- 扫描循环 ------------------------------------------------------------------


@dataclass
class ScanOutcome:
    coordinate: Coordinate
    panel: PlanetPanel
    confirmed: bool
    seconds: float


def scan_one(
    navigator: SystemNavigator,
    ocr: Any,
    coordinate: Coordinate,
    *,
    debug_dir: Path | None,
    attempts: int = READ_ATTEMPTS,
) -> ScanOutcome:
    """扫一个坐标；坐标核对不过就重来，重来仍不过才算失败。

    实测 `2:2:11` 被读成 `[2:2:1]`——相邻的两个 1 粘在一起少读一位。核对是对的
    （读回来的不是请求的那个就不能入库），但**只读一次就放弃会留下静默缺口**：
    这个坐标既没入库，游标也会被后面成功的坐标带过去，再也不会回来。
    """
    started = time.perf_counter()
    outcome: ScanOutcome | None = None
    requested = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
    for attempt in range(attempts):
        if attempt:
            # 重来一次要把三个字段都重设：读不出可能是因为根本没跳过去。
            navigator.invalidate()
        navigator.goto(coordinate)
        image = navigator.driver.capture()
        # 先在同一张截图上换放大档位——位 11 的失败是读不出，不是没跳过去。
        panel = read_panel_confirming(crop_reader(image, ocr), requested)
        outcome = ScanOutcome(
            coordinate, panel, panel.confirms(requested), round(time.perf_counter() - started, 2)
        )
        if outcome.confirmed:
            return outcome
        if debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            image.save(debug_dir / f"unconfirmed-{requested.replace(':', '-')}-{attempt + 1}.png")
    assert outcome is not None  # attempts >= 1
    return outcome


#: 底部导航条**只取文字那一行**。连着图标一起裁会把图标读成拉丁噪声，
#: 同一屏两次读出 '区 Y ASA 商店 o j' 和 '6S BOD ®@'——前者恰好含「商店」而后者不含，
#: 于是在线的会话时好时坏地被判成「认不出」。
NAV_TEXT_ROI = (800, 878, 1215, 908)
NAV_TEXT_UPSCALE = 3


#: START 按钮所在的横条。整屏 OCR 读不出 START——那是压在星空上的半透明大字，
#: psm 6/11/12 全军覆没；紧凑裁剪 + 3× + 二值化才读得出来。
START_ROI = (820, 745, 1100, 830)
START_UPSCALE = 3
START_THRESHOLD = 180

#: 入口页（语言选择页）的标题与「进入」按钮。两处都读得很干净，不需要二值化。
#: 掉线弹窗的正文行与那个绿色 ✓（截图 client 空间，实机量于 2026-08-09）。
#:
#: 弹窗只有一个按钮，所以按钮位置写死；但**是否要点它由那行字决定**——
#: 读到「连接已断开」才点，读不到就停。绿色像素只用来量位置，不作判据：
#: 派遣界面上的绿色 ✓ 长得一模一样，靠颜色认会在派遣页上点出一发舰队。
DISCONNECT_TEXT_ROI = (780, 440, 1140, 500)
DISCONNECT_BUTTON = (960, 583)
DISCONNECT_UPSCALE = 3

ENTRY_TITLE_ROI = (780, 305, 1140, 355)
ENTRY_BUTTON_ROI = (999, 385, 1118, 430)
ENTRY_UPSCALE = 3
ENTRY_BUTTON_TEXT = "进入"


def make_session_keeper(
    driver: LiveDriver,
    ocr: Any,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """巡检用的会话守护。

    **只在真的读到 START 时才点 START**：判据来自这一屏本身，而不是「不在游戏里就多半是它」。
    认不出的画面一律停止——可能是维护公告或弹窗，乱点会误触派遣、删信或领奖。
    """
    from evo_helper.game.session_keeper import ScreenState, SessionKeeper, classify_screen

    def start_button(image: Any) -> tuple[int, int] | None:
        """在 START 横条里定位按钮；读不到就返回 None（于是不点）。"""
        text = ocr(
            image.crop(START_ROI),
            digits=False,
            upscale=START_UPSCALE,
            threshold=START_THRESHOLD,
        )
        if "START" not in text.upper():
            return None
        left, top, right, bottom = START_ROI
        return ((left + right) // 2, (top + bottom) // 2)

    def entry_button(image: Any) -> tuple[int, int] | None:
        """在入口页上定位「进入」；读不到就返回 None（于是不点）。"""
        text = ocr(image.crop(ENTRY_BUTTON_ROI), digits=False, upscale=ENTRY_UPSCALE)
        if ENTRY_BUTTON_TEXT not in text:
            return None
        left, top, right, bottom = ENTRY_BUTTON_ROI
        return ((left + right) // 2, (top + bottom) // 2)

    def disconnected(image: Any) -> bool:
        """掉线弹窗在不在。"""
        text = ocr(image.crop(DISCONNECT_TEXT_ROI), digits=False, upscale=DISCONNECT_UPSCALE)
        return classify_screen(text) is ScreenState.DISCONNECTED

    def observe() -> ScreenState:
        image = driver.capture()
        # **掉线要排在导航条之前判。** 弹窗是浮层，底下的导航条还在画面上，
        # 「商店/联盟」照样读得出来——先判导航条就会把死会话认成在线，
        # 之后每一步点击都石沉大海，而且全程不报错。实机上确认过这一屏。
        if disconnected(image):
            return ScreenState.DISCONNECTED
        state = classify_screen(
            ocr(image.crop(NAV_TEXT_ROI), digits=False, upscale=NAV_TEXT_UPSCALE)
        )
        if state is ScreenState.IN_GAME:
            return state
        # 顺序要紧：**先判入口页再判 START**。入口页浮在 START 页之上，
        # 底下那个 START 仍在画面里；反过来判就会在入口页上去点 START。
        entry = classify_screen(
            ocr(image.crop(ENTRY_TITLE_ROI), digits=False, upscale=ENTRY_UPSCALE)
        )
        if entry is ScreenState.ENTRY:
            return entry
        return ScreenState.START if start_button(image) is not None else state

    def click_start() -> None:
        spot = start_button(driver.capture())
        if spot is None:
            raise RuntimeError("要点 START 时却读不到它；停止而不是往固定坐标乱点")
        driver.click(*spot, label="START")

    def click_entry() -> None:
        spot = entry_button(driver.capture())
        if spot is None:
            raise RuntimeError("要点「进入」时却读不到它；停止而不是往固定坐标乱点")
        driver.click(*spot, label=ENTRY_BUTTON_TEXT)

    def dismiss_disconnect() -> None:
        if not disconnected(driver.capture()):
            raise RuntimeError("要关掉线弹窗时却读不到那行字；停止而不是往固定坐标乱点")
        driver.click(*DISCONNECT_BUTTON, label="确认掉线弹窗")

    return SessionKeeper(
        observe=observe,
        click_entry=click_entry,
        click_start=click_start,
        dismiss_disconnect=dismiss_disconnect,
        clock=clock,
        sleep=sleep,
    )


def run_scan(
    *,
    limit: int | None,
    debug_dir: Path | None,
    skip_scanned: bool,
    rescan_missing: bool = False,
    recheck_suspicious: bool = False,
    one_bot_per_system: bool = ONE_BOT_PER_SYSTEM,
) -> int:
    import ctypes

    # 系统缩放 125%：不声明 DPI 感知拿到的全是逻辑像素，窗口怎么调都对不上标定视口，
    # 而且看起来没有任何报错。
    # ctypes.windll 只在 Windows 上存在，动态取属性好让 Linux 上的 mypy 也能过。
    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    session_factory = create_session_factory(create_database_engine(Settings().database_url))
    repository = SqlAlchemyRepository(session_factory)
    run_id, cursor = ensure_run(session_factory)
    done = already_scanned(session_factory) if skip_scanned else set()

    say(f"运行实例 {run_id}")
    say(f"游标 {cursor if cursor is not None else '（尚未开始）'}；已确认坐标 {len(done)} 个")

    driver = LiveDriver()
    driver.window()  # 窗口不见了就在这一步拉起来，而不是等到第一次点击才失败
    ocr = make_ocr()
    navigator = SystemNavigator(driver)
    keeper = make_session_keeper(driver, ocr)

    def read_nav_labels() -> str:
        return str(ocr(driver.capture().crop(NAV_LABEL_ROI), digits=False, upscale=3))

    # **先确认会话还在，再谈切视图。** 顺序反了会这样：中断后重启时会话已经掉了，
    # 画面停在入口页或 START 页，导航栏标签自然读不到，`ensure_system_view` 就朝
    # 视图菜单坐标盲点三次然后放弃——**永远走不到能重连的 SessionKeeper**。
    # 实测这条路径把扫描卡了几千轮，日志里全是「切不到恒星系视图」，
    # 一次都没提过巡检；而且那三次点击本身就违反「认不出的画面绝不点击」。
    session = keeper.ensure_connected(force=True)
    if session is not None and not session.ready:
        say(f"会话不可用：{session.detail}；安全停止")
        return 1
    if session is not None and session.reconnected:
        say("已重新登录")

    if not navigator.ensure_system_view(read_nav_labels):
        say("切不到恒星系视图；停止而不是往固定坐标乱点")
        return 1

    if recheck_suspicious:
        # 复核不推进游标，也不跳过「已扫过」——重点就是重读这些已入库的坐标。
        pending = suspicious_names(session_factory)
        say(f"复核模式：名字像 bot 却没判成 bot 的坐标有 {len(pending)} 个")
        done = set()
        coordinates: Iterator[Coordinate] = iter(pending)
    elif rescan_missing:
        # 补缺口时游标已经在前面了，不能再往前推——推了会把还没扫的坐标当成扫过。
        pending = list(
            missing_from_plan(session_factory, upto=cursor, one_bot_per_system=one_bot_per_system)
        )
        say(f"补缺口模式：游标之前有 {len(pending)} 个坐标没入库")
        coordinates = iter(pending)
    else:
        coordinates = _planned(cursor)

    # 每系一个 bot：已经找到的系整个跳过。补缺口那条路径用的是同一个判据。
    found_systems = (
        systems_with_bot(session_factory) if one_bot_per_system and skip_scanned else set()
    )
    if one_bot_per_system:
        say(f"每系一个 bot：已定位 {len(found_systems)} 个系，这些系剩余的行星位不再扫")

    scanned = bots = rejected = skipped = 0
    consecutive_failures = 0
    for coordinate in coordinates:
        if limit is not None and scanned >= limit:
            say(f"已达本次上限 {limit}")
            break
        if (coordinate.galaxy, coordinate.system) in found_systems:
            # 这个系的 bot 已经找到了，剩下的位不用看。游标照推，续扫才不会回头。
            skipped += 1
            save_cursor(session_factory, run_id, coordinate)
            continue
        if (coordinate.galaxy, coordinate.system, coordinate.position) in done:
            save_cursor(session_factory, run_id, coordinate)
            continue

        outcome = keeper.ensure_connected()
        if outcome is not None:
            if not outcome.ready:
                say(f"会话巡检未通过：{outcome.detail}；安全停止")
                return 1
            if outcome.reconnected and not navigator.ensure_system_view(read_nav_labels):
                say("重连后切不回恒星系视图；安全停止")
                return 1

        result = scan_one(navigator, ocr, coordinate, debug_dir=debug_dir)
        label = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
        if not result.confirmed:
            consecutive_failures += 1
            rejected += 1
            say(
                f"{label} 坐标核对失败，原文 {result.panel.coordinate_text!r}"
                f"（连续 {consecutive_failures}）"
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                say("连续核对失败，画面多半已经不是面板；安全停止")
                return 1
            # 记下来，否则这个坐标既没入库、游标又被后面的成功坐标带过去，就此消失。
            record_gap(coordinate, result.panel.coordinate_text)
            navigator.invalidate()
            # 核对失败最常见的原因就是掉线。巡检十分钟才一次，等不到——
            # 这里立刻查一次，否则接下来又会在入口页上朝视图菜单盲点。
            dropped = keeper.ensure_connected(force=True)
            if dropped is not None and not dropped.ready:
                say(f"核对失败且会话不可用：{dropped.detail}；安全停止")
                return 1
            # 游戏会自己回到行星视图；先确认还在恒星系视图再接着扫。
            if not navigator.ensure_system_view(read_nav_labels):
                say("核对失败后切不回恒星系视图；安全停止")
                return 1
            continue

        consecutive_failures = 0
        name = result.panel.display_name
        repository.save_scan(
            CoordinateScan(
                run_id=run_id,
                coordinate=coordinate,
                scanned_at_utc=datetime.now(UTC),
                owner_name=name,
                is_bot=result.panel.is_bot,
                # 坐标经过「请求 vs 读回」双向一致校验，故为 1.0。
                confidence=1.0,
            )
        )
        if not rescan_missing and not recheck_suspicious:
            save_cursor(session_factory, run_id, coordinate)
        scanned += 1
        if result.panel.is_bot:
            bots += 1
            # 本系收工：每系一个 bot，剩下的位不用扫了。
            found_systems.add((coordinate.galaxy, coordinate.system))
            say(f"{label} BOT {name}  {result.seconds}s")
        else:
            say(f"{label} {name or '空位'}  {result.seconds}s")

    say(
        f"本次入库 {scanned} 条，其中 bot {bots} 个，核对失败 {rejected} 个，"
        f"按「每系一个 bot」跳过 {skipped} 个"
    )
    return 0


def _planned(cursor: Coordinate | None) -> Iterator[Coordinate]:
    from evo_helper.domain.scan_plan import CursorNotInPlanError

    try:
        yield from iter_scan_coordinates(after=cursor)
    except CursorNotInPlanError as exc:  # pragma: no cover - 计划改过才会走到
        raise SystemExit(f"{exc}；请确认分段是否变更后再续扫") from exc


def show_status() -> int:
    session_factory = create_session_factory(create_database_engine(Settings().database_url))
    run_id, cursor = ensure_run(session_factory)
    done = already_scanned(session_factory)
    total = total_coordinates()
    bounds = ScanBounds()
    say(f"运行实例 {run_id}")
    say(f"计划总量 {total:,} 个坐标（每系 {bounds.positions_per_system} 位，跳过 1–4）")
    say(f"游标 {cursor if cursor is not None else '（尚未开始）'}")
    say(f"已确认坐标 {len(done):,}（{len(done) / total:.4%}）")
    for index, (segment, start, end) in enumerate(planned_segments()):
        span = f"{segment.galaxy}:{segment.first_system:03d}–{segment.last_system:03d}"
        say(f"  {index + 1:>2}. {span}  {start} → {end}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="本次最多入库多少个坐标")
    parser.add_argument("--status", action="store_true", help="只看计划与游标，不动游戏")
    parser.add_argument(
        "--debug-dir", type=Path, default=Path("debug/scan"), help="核对失败时存图的目录"
    )
    parser.add_argument(
        "--no-skip-scanned", action="store_true", help="不跳过已确认过的坐标（用于强制复查）"
    )
    parser.add_argument(
        "--rescan-missing", action="store_true", help="只补游标之前没入库的坐标，不推进游标"
    )
    parser.add_argument(
        "--scan-full-systems",
        action="store_true",
        help="不套用「每系一个 bot」，每个系的 16 个位都扫（约耗时翻倍）",
    )
    parser.add_argument(
        "--recheck-suspicious",
        action="store_true",
        help="重读「名字像 bot 却没判成 bot」的坐标，不推进游标",
    )
    args = parser.parse_args(argv)

    if args.status:
        return show_status()
    return run_scan(
        limit=args.limit,
        debug_dir=args.debug_dir,
        skip_scanned=not args.no_skip_scanned,
        rescan_missing=args.rescan_missing,
        recheck_suspicious=args.recheck_suspicious,
        one_bot_per_system=not args.scan_full_systems,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
