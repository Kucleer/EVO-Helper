"""海盗侦查-攻击循环：一个恒星系扫 1–4 位，侦察、判定、攻击，然后下一个。

    # 只看有没有海盗，一次点击都不派（默认）
    python -m evo_helper.tools.pirate_loop --systems 2:137

    # 加上侦察（真的派探测器出去），读回报告并打印判定
    python -m evo_helper.tools.pirate_loop --systems 2:137 --scout

    # 完整循环：判定为「打」的目标用预设 AAA 攻击
    python -m evo_helper.tools.pirate_loop --systems 2:137 --scout --attack

三档是刻意分开的：默认一个动作都不做，`--scout` 只派探测器，`--attack` 才会真的
把战斗舰队送出去。每一档都得显式打开，不存在「顺手就打出去了」这条路径。

## 攻击前的三道闸门（缺一不可，任一不通过就不点出发）

1. **面板认得出**：行星面板上读到「敌对海盗」，且坐标行与请求的坐标一致。
2. **预设按标题选中了**：`PresetPicker` 在预设条上 OCR 找到那个标题才点。
   找不到就整发放弃——**只认标题，不看预设里装了什么**（用户口径 2026-08-09：
   预设内容由用户自己在游戏里维护，助手不读也不校验）。
3. **简报写着「攻击」**：`pirate_ui.briefing_says_attack`。任务类型选错时这道闸门
   是最后一次拦住的机会。

## 判定

侦察报告里 `深空吞噬者 / 噬能截击者 / 钛能守卫者 / 收割者` 任一 > 1 就打。
判定结论是三值的（见 `vision.scout_reports`）：读不出来时**不打**也不当成空位。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
)
from evo_helper.domain.scan_bounds import PIRATE_POSITIONS
from evo_helper.game import pirate_ui
from evo_helper.game.preset_picker import PresetNotFound, PresetPicker, name_words
from evo_helper.game.system_navigator import (
    NAV_LABEL_ROI,
    PLANET_VIEW_BUTTON,
    VIEW_MENU_BUTTON,
    VIEW_SWITCH_WAIT_S,
    SystemNavigator,
    crop_reader,
)
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools.scan_coordinates import LiveDriver, make_ocr

#: 这条链路自己的计划与幂等键，与坐标扫描分开：两者的游标含义不同，
#: 共用一个运行实例会让「扫到哪了」和「打到哪了」互相踩。
PLAN_NAME = "海盗侦查攻击循环"
RUN_KEY = "pirate-loop-0001"

#: 出发星球。飞行时间与战报匹配都要它。
ORIGIN = Coordinate(2, 137, 18)

#: 点「侦察」/「攻击」之后等派遣面板铺开。
DISPATCH_WAIT_S = 2.4

#: 点绿✓之后等简报页出来。
BRIEFING_WAIT_S = 2.6

#: 点「出发！」之后等回到列表。
LAUNCH_WAIT_S = 2.8

#: 侦察报告的等待：实机上 17 秒回报，留足余量再读，读不到就再等一轮。
SCOUT_REPORT_WAIT_S = 45.0
SCOUT_REPORT_RETRIES = 3

#: 自己星球地表视图右上角的信箱入口。**底部导航里没有邮箱**，只有这一个入口。
MAIL_BUTTON = (1131, 70)

#: 信箱按钮旁边的未读数。**地表视图独有**：恒星系视图那个位置是坐标输入框，
#: 各种浮层则把它盖住。用它当「我在地表」的正面凭据。
MAIL_BADGE_ROI = (1145, 55, 1200, 92)

#: 信箱「报告」标签、邮件首行中心与行距（917 空间）。
MAIL_REPORT_TAB = (897, 178)
MAIL_FIRST_ROW_Y = 285
MAIL_ROW_PITCH = 86
MAIL_ROW_X = 900

#: 一趟信箱最多翻几行。可见 6 行整行，第 7 行被切掉；侦察报告按时间倒序排在最上面。
MAIL_SCAN_ROWS = 6

#: 读之前把列表拖回顶部。面板会夹住，多拖一次无害，少拖一次就可能从半截邮件读起。
MAIL_SCROLL_TO_TOP_DRAGS = 3

#: 面板标题（那块金属牌上的大字），用来认出「现在是哪个面板」。
#: 邮件列表是「邮箱」，报告详情页是「消息」——两者都是大字，读得很干净。
#:
#: ⚠️ **不要拿那两排分类标签当判据。** 试过，不行：标签是小字，而未读角标
#: （`21`、`99+`、`16`）正压在它们上面，`--psm 7` 会读成
#: `'oe. se. eee ee'` 这样的噪声——而画面明明就是邮件列表。
#: 角标数字随邮件多少变，所以这个失败还是时好时坏的。
PANEL_TITLE_ROI = (890, 55, 1040, 95)
MAIL_LIST_TITLE = "邮箱"
MAIL_DETAIL_TITLE = "消息"

#: 信箱与详情页左上角的返回/关闭键（同一个位置，语义随页面变）。
MAIL_BACK = (750, 71)

#: 详情页里把内容拖到底用的起止点（917 空间）。必须慢拖，见 `slow_drag`。
PANEL_DRAG_FROM_Y = 700
PANEL_DRAG_TO_Y = 300


def say(message: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {message}", flush=True)


@dataclass
class LoopOptions:
    systems: tuple[tuple[int, int], ...]
    scout: bool
    attack: bool
    preset: str = pirate_ui.ATTACK_PRESET_NAME


@dataclass
class Outcome:
    pirates: list[Coordinate] = field(default_factory=list)
    scouted: list[Coordinate] = field(default_factory=list)
    attacked: list[Coordinate] = field(default_factory=list)
    refused: list[tuple[Coordinate, str]] = field(default_factory=list)


class PirateLoop:
    """驱动一轮「扫 1–4 位 → 侦察 → 判定 → 攻击」。"""

    def __init__(self, driver: LiveDriver, ocr: Any, options: LoopOptions) -> None:
        self._driver = driver
        self._ocr = ocr
        self._options = options
        self._navigator = SystemNavigator(driver)
        self._outcome = Outcome()
        self._repository: SqlAlchemyRepository | None = None
        self._run_id: UUID | None = None

    # -- 读屏 ---------------------------------------------------------------

    def _read(
        self, roi: tuple[int, int, int, int], *, digits: bool = False, upscale: int = 3
    ) -> str:
        self._ensure_geometry()
        return crop_reader(self._driver.capture(), self._ocr)(roi, digits=digits, upscale=upscale)

    def _ensure_geometry(self) -> None:
        """每次读屏前核一次视口尺寸，漂了就调回来。

        ⚠️ **窗口会在运行中自己缩回去。** 实机反复撞到：跑到中途窗口从 1920×917
        变成 1536×733，于是所有 ROI 读的都是别处的像素、所有点击都落在别处——
        而且**一声不响**：信箱明明开着，判据却读不到那两排标签，看起来像 OCR 不行。

        校验很便宜（一次 `GetClientRect`），比事后从错误现象往回猜便宜得多。
        """
        from evo_helper.game.game_window import (
            APP_TITLE_BAR_PX,
            CALIBRATED_VIEWPORT,
            ensure_game_window,
        )
        from evo_helper.vision.optional.window_capture import client_box

        box = client_box(self._driver.window())
        size = (box[2] - box[0], box[3] - box[1] - APP_TITLE_BAR_PX)
        if size != CALIBRATED_VIEWPORT:
            say(f"  视口漂到 {size[0]}x{size[1]}，调回 {CALIBRATED_VIEWPORT}")
            ensure_game_window()

    def _nav_labels(self) -> str:
        return self._read(NAV_LABEL_ROI)

    def _dump_frame(self, name: str, roi: tuple[int, int, int, int] | None = None) -> None:
        """把当前这一帧（和一块 ROI 的读数）存到 `var/logs/`，供事后复盘。"""
        from pathlib import Path

        directory = Path("var/logs")
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        image = self._driver.capture()
        path = directory / f"dump-{name}-{stamp}.png"
        image.save(path)
        note = f"  已存现场 {path}（{image.width}x{image.height}）"
        if roi is not None:
            note += f"；ROI{roi} 读到 {self._read(roi)!r}"
        say(note)

    def _preset_names(self) -> list[tuple[int, str]]:
        import pytesseract

        return name_words(self._driver.capture(), pytesseract)

    # -- 识别 ---------------------------------------------------------------

    def is_pirate(self, coordinate: Coordinate) -> bool:
        """行星面板上是不是「敌对海盗」，而且坐标对得上。

        坐标要核：导航栏偶尔会停在别的位号上（实机踩过），这时面板是真的、
        只是不是请求的那一位——照着它打就打错了目标。
        """
        title = self._read(pirate_ui.PIRATE_TITLE_ROI)
        if pirate_ui.PIRATE_TITLE_TEXT not in title:
            return False
        wanted = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
        shown = self._read(pirate_ui.PIRATE_COORD_ROI, digits=True)
        if wanted not in shown:
            say(f"  坐标核对不过：面板显示 {shown!r}，请求的是 {wanted}")
            return False
        return True

    # -- 派遣 ---------------------------------------------------------------

    def _briefing_mission(self) -> str:
        """简报页上的任务类型。**要等它铺开**，不能只读一次。

        实测四发攻击全部卡在这里：等 2.6 秒读一次读不出来，于是闸门判成
        「简报不是攻击」而整发拒绝。页面是滑进来的，跟信箱标题一个毛病。
        """
        mission = ""

        def read_once() -> bool:
            nonlocal mission
            mission = pirate_ui.snap_mission(self._read(pirate_ui.BRIEFING_MISSION_ROI)) or ""
            return mission != ""

        self._settle(read_once)
        return mission

    def _launch(self, coordinate: Coordinate, mission: str) -> bool:
        """简报页核对任务类型，通过才点「出发！」。"""
        shown = self._briefing_mission()
        if shown != mission:
            say(f"  简报写的是 {shown or '（读不出）'}，不是{mission}；不点出发")
            self._dump_frame("briefing-unrecognised", pirate_ui.BRIEFING_MISSION_ROI)
            self._driver.click(*pirate_ui.BRIEFING_BACK_BUTTON, label="返回")
            self._driver.wait(LAUNCH_WAIT_S)
            self._outcome.refused.append((coordinate, f"简报不是{mission}"))
            return False
        self._driver.click(*pirate_ui.BRIEFING_LAUNCH_BUTTON, label="出发")
        self._driver.wait(LAUNCH_WAIT_S)
        return True

    def scout(self, coordinate: Coordinate) -> bool:
        """派一发侦察。派遣面板的终点是自动预填的，侦察也不需要选预设。"""
        self._driver.click(*pirate_ui.SCOUT_BUTTON, label="侦察")
        self._driver.wait(DISPATCH_WAIT_S)
        self._driver.click(*pirate_ui.DISPATCH_CONFIRM, label="确认终点")
        self._driver.wait(BRIEFING_WAIT_S)
        if not self._launch(coordinate, "侦察"):
            self._leave_dispatch_list()
            return False
        self._outcome.scouted.append(coordinate)
        say(f"  已派出侦察 → {coordinate}")
        # 派出之后停在「飞行中」列表上，必须自己退出来。
        self._leave_dispatch_list()
        return True

    def attack(self, coordinate: Coordinate) -> bool:
        """用预设攻击。闸门是「预设标题选中了」与「简报写着攻击」。

        **只按标题选预设，不读预设内容**（用户口径 2026-08-09）：内容是用户自己在
        游戏里维护的，助手去核对既多余、也会把「用户改了预设」误判成故障。
        """
        self._driver.click(*pirate_ui.ATTACK_BUTTON, label="攻击")
        self._driver.wait(DISPATCH_WAIT_S)

        picker = PresetPicker(driver=self._driver, read_names=self._preset_names)
        try:
            picker.pick(self._options.preset)
        except PresetNotFound as error:
            say(f"  {error}；关掉面板，不打这一发")
            self._driver.click(*pirate_ui.DISPATCH_CLOSE, label="关闭派遣面板")
            self._driver.wait(DISPATCH_WAIT_S)
            self._outcome.refused.append((coordinate, "找不到预设"))
            return False

        self._driver.click(*pirate_ui.DISPATCH_CONFIRM, label="确认终点")
        self._driver.wait(BRIEFING_WAIT_S)
        intent_id = self._record_intent(coordinate)
        if not self._launch(coordinate, "攻击"):
            self._leave_dispatch_list()
            return False
        self._record_dispatch(intent_id)
        self._outcome.attacked.append(coordinate)
        say(f"  已发动攻击 → {coordinate}（预设 {self._options.preset}）")
        self._leave_dispatch_list()
        return True

    # -- 侦察报告 -----------------------------------------------------------

    def collect_scout_reports(self, wanted: Sequence[Coordinate]) -> dict[Coordinate, Any]:
        """**一次进信箱**，把最上面几封侦察报告全读出来，按目标坐标归档。

        为什么不是「一个目标进一次信箱」：进出信箱要切视图、开面板、翻标签，
        每份报告还要慢拖两次，一趟十几秒。四个目标各跑一趟就是一分钟纯导航，
        而它们的报告本来就并排躺在同一页上。

        按**报告自己写的目标**归档，不按行号猜：行序会随新邮件变，
        而报告开头那行写着「已对 [x:y:z] 完成侦察」，那是它自己的凭据。
        """
        from evo_helper.vision.optional.report_screens import ImageReportScreens
        from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport
        from evo_helper.vision.scout_reports import ScoutReportUnreadable, read_pirate_scout

        def screens() -> Any:
            image = crop_to_viewport(self._driver.capture())
            return ImageReportScreens(
                image,
                layout_for_viewport(image.width, image.height),
                tesseract_cmd=str(_tesseract_path()),
            )

        if not self._goto_planet_surface():
            raise RuntimeError("切不到自己星球地表，读不了信箱；安全停止")
        self._open_mail()
        # 列表会记住上次滚到哪。不拖回顶部，第 0 行可能是一封只露半截的邮件——
        # 读出来是空主题，而画面看着完全正常。侦察报告按时间倒序在最上面，
        # 所以顶部才是要读的那几行。
        for _ in range(MAIL_SCROLL_TO_TOP_DRAGS):
            slow_drag(self._driver, PANEL_DRAG_TO_Y, PANEL_DRAG_FROM_Y)
        found: dict[Coordinate, Any] = {}
        remaining = set(wanted)
        for row in range(MAIL_SCAN_ROWS):
            if not remaining:
                break
            # ⚠️ **每翻一行都要先确认「还在邮件列表上」。** 实机踩过两次同一个错：
            # 上一次返回没退到列表（或把整个信箱关掉了），接着照列表的行坐标点下去，
            # 于是点在了地表 UI 上——一次点开了「取消任务」确认框，一次点开了「排名」。
            # 认不出就停，别赌下一下点在哪。
            if not self._settle(self._on_mail_list):
                say(f"  第 {row} 行之前已经不在邮件列表上了；停止翻行")
                break
            self._driver.click(
                MAIL_ROW_X, MAIL_FIRST_ROW_Y + row * MAIL_ROW_PITCH, label="打开邮件"
            )
            self._driver.wait(2.4)
            header = screens()
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            ships = screens()
            try:
                reading = read_pirate_scout(header, ships)
            except ScoutReportUnreadable as error:
                say(f"  第 {row} 行读不出侦察报告：{error}")
            else:
                if reading.target in remaining:
                    found[reading.target] = reading
                    remaining.discard(reading.target)
                    say(f"  第 {row} 行 → {reading.target} {reading.verdict}")
                else:
                    say(f"  第 {row} 行是 {reading.target} 的报告，不在本轮目标里")
            # 退回列表再看下一行。
            self._driver.click(*MAIL_BACK, label="返回")
            self._driver.wait(2.0)
        self._close_mail()
        return found

    def _panel_title(self) -> str:
        return self._read(PANEL_TITLE_ROI)

    def _settle(self, predicate: Callable[[], bool], *, tries: int = 4, pause: float = 1.0) -> bool:
        """等某个判据成立，而不是只判一次。

        ⚠️ 面板是**滑进来**的。实测：点开信箱后等 2.4 秒判一次「标题是不是邮箱」
        判不到，而失败时存下来的那一帧（约一秒后）读得清清楚楚是「邮箱」——
        判据没错，只是那一刻标题还在动画里。一次性判定会把「还没铺开」
        误报成「不是这个面板」，然后整轮白停。
        """
        for attempt in range(tries):
            if predicate():
                return True
            if attempt + 1 < tries:
                self._driver.wait(pause)
        return False

    def _on_mail_list(self) -> bool:
        """在不在信箱的邮件列表页上。判据是面板标题读到「邮箱」。"""
        return MAIL_LIST_TITLE in self._panel_title()

    def _on_mail_detail(self) -> bool:
        """在不在报告详情页上。判据是面板标题读到「消息」。"""
        return MAIL_DETAIL_TITLE in self._panel_title()

    def _open_mail(self) -> None:
        """去信箱。**每一步都要先认出这一屏，再点下一下。**

        ⚠️ 实机事故（2026-08-09）：这段原本是三下连点。派出侦察之后游戏停在
        「飞行中」列表上，而不是行星地表——于是第一下点在了列表里某条探索任务的
        「取消」上，游戏弹出「确定要取消该任务吗？」。幸好那一屏没被继续盲点，
        否则会取消掉用户正在跑的探索任务。

        所以这里改成：先确认在行星地表（读得到信箱按钮旁边那排东西），
        再点信箱；确认信箱开了（读到「邮箱」），再点「报告」标签。
        认不出就抛异常，让调用方停下来——**不许在认不出的画面上点第二下**。
        """
        if not self._on_planet_surface():
            raise RuntimeError("不在自己星球地表视图上，拒绝去点信箱（认不出的画面不点）")
        self._driver.click(*MAIL_BUTTON, label="信箱")
        self._driver.wait(2.4)
        if not self._settle(self._on_mail_list):
            # 认不出就把那一帧和读到的字存下来。判据失败时最贵的事情是「不知道当时
            # 画面长什么样」——存一帧的成本是一次写盘，省下的是一轮实机复现。
            self._dump_frame("mail-list-unrecognised", PANEL_TITLE_ROI)
            raise RuntimeError("点了信箱却没读到「邮箱」标题；停止而不是继续盲点")
        self._driver.click(*MAIL_REPORT_TAB, label="报告标签")
        self._driver.wait(2.0)

    def _on_planet_surface(self) -> bool:
        """在不在自己星球的地表视图上。正负两面各要一个凭据。

        - **负**：读不到恒星系那排坐标输入框的标签（银河系/恒星系/行星）。
        - **正**：右上角信箱旁边的未读数读得出数字（实机 `70`）。

        为什么不用星球名：那行「奥格瑞玛」是描边橙字压在金属牌上，
        实测 `chi_sim+eng` 读成 `“Rian`——拿读不准的东西当判据等于换个地方失败。

        两面都要，是为了挡住浮层：信箱面板、派遣面板、飞行中列表也读不到坐标行，
        但它们会盖住右上角那个未读数。只看「没有坐标行」会把浮层当成地表，
        然后在浮层上照地表的坐标点下去——这就是本轮点到「取消任务」的那个错。
        """
        from evo_helper.game.system_navigator import on_system_view

        if on_system_view(self._nav_labels()):
            return False
        return self._read(MAIL_BADGE_ROI, digits=True).strip() != ""

    def _goto_planet_surface(self, *, attempts: int = 3) -> bool:
        """从恒星系视图切回自己星球地表。切不过去返回 False。

        走**视图菜单**：星球按钮 → 子菜单第二项（带环行星）。子菜单只列出你现在
        不在的那些视图，所以这同一个像素在地表上是「回恒星系」、在恒星系里是
        「去地表」——`ensure_system_view` 用的就是它，方向相反而已。

        ⚠️ **不要走底部导航的「行星」**（用户 2026-08-09 明确指出）。那个开出来的是
        行星列表浮层，每颗星球一行、每行八个图标全是真实操作（运输/部署/传送/转移/
        投送/保护/扩张），而且「前往此处」的位置随行走——在那上面找坐标既没必要又危险。
        """
        for attempt in range(attempts):
            if self._on_planet_surface():
                return True
            self._driver.click(*VIEW_MENU_BUTTON, label="视图菜单")
            self._driver.wait(1.0)
            self._driver.click(*PLANET_VIEW_BUTTON, label="行星视图")
            self._driver.wait(VIEW_SWITCH_WAIT_S * (attempt + 1))
            # 视图换过之后导航栏里是什么已经不可知了。
            self._navigator.invalidate()
        return self._on_planet_surface()

    def _leave_dispatch_list(self) -> None:
        """派出之后游戏停在「飞行中」列表上，把它关掉并切回恒星系视图。

        少了这一步，下一个目标的 `goto` 会在列表页上朝导航栏坐标盲点——
        实机上就是这样点到了「取消」。
        """
        self._driver.click(*MAIL_BACK, label="关闭面板")
        self._driver.wait(2.2)
        if not self._navigator.ensure_system_view(self._nav_labels):
            raise RuntimeError("派出之后切不回恒星系视图；安全停止")
        self._navigator.invalidate()

    def _close_mail(self) -> None:
        """回到恒星系视图。信箱是浮层，关掉之后还在自己星球的地表视图上。"""
        self._driver.click(*MAIL_BACK, label="关闭信箱")
        self._driver.wait(2.0)
        if self._on_mail_list():
            # 还在列表上说明刚才那一下退的是详情页，再退一层才关掉信箱。
            self._driver.click(*MAIL_BACK, label="关闭信箱")
            self._driver.wait(2.0)
        if not self._navigator.ensure_system_view(self._nav_labels):
            raise RuntimeError("读完邮件切不回恒星系视图；安全停止")
        self._navigator.invalidate()

    # -- 持久化 -------------------------------------------------------------

    def _ensure_run(self) -> tuple[SqlAlchemyRepository, UUID]:
        if self._repository is not None and self._run_id is not None:
            return self._repository, self._run_id
        session_factory = create_session_factory(create_database_engine(Settings().database_url))
        self._repository = SqlAlchemyRepository(session_factory)
        self._run_id = _ensure_run_row(session_factory)
        return self._repository, self._run_id

    def _record_intent(self, coordinate: Coordinate) -> UUID:
        """**在点出发之前**写意图。

        顺序是有意的：被闸门拦下的那些恰恰最该出现在日志里，而它们没有派遣行。
        先写意图、后写派遣，日志上就能看出「想打但没打出去」。
        """
        repository, run_id = self._ensure_run()
        intent_id = uuid4()
        now = datetime.now(UTC)
        repository.save_attack_intent(
            AttackIntent(
                intent_id=intent_id,
                run_id=run_id,
                origin=ORIGIN,
                target=coordinate,
                preset=FleetPresetRef(
                    name=self._options.preset,
                    signature=_preset_signature(self._options.preset),
                ),
                cycle_start_utc=now,
                created_at_utc=now,
                target_kind=TARGET_KIND_PIRATE,
            )
        )
        return intent_id

    def _record_dispatch(self, intent_id: UUID) -> None:
        repository, _run_id = self._ensure_run()
        repository.save_dispatch(
            AttackDispatch(
                dispatch_id=uuid4(),
                intent_id=intent_id,
                dispatched_at_utc=datetime.now(UTC),
                dry_run=False,
                accepted=True,
            )
        )

    # -- 主循环 -------------------------------------------------------------

    def run(self) -> Outcome:
        # 几何先校一遍。窗口被改过尺寸时所有坐标一起失效，而这件事悄无声息——
        # 本轮开工时窗口就是 1536×733，照 1920×917 的坐标点下去全落在别处。
        from evo_helper.game.game_window import ensure_game_window

        ensure_game_window()
        self._reset_to_known_screen()
        if not self._navigator.ensure_system_view(self._nav_labels):
            raise RuntimeError("切不到恒星系视图；停止而不是往固定坐标乱点")

        for galaxy, system in self._options.systems:
            say(f"恒星系 {galaxy}:{system}")
            pirates = self._find_pirates(galaxy, system)
            if not pirates:
                say("  1–4 位没有敌对海盗")
                continue
            if self._options.scout:
                scouted_here = 0
                for coordinate in pirates:
                    self._navigator.goto(coordinate)
                    if not self.is_pirate(coordinate):
                        continue
                    if self.scout(coordinate):
                        scouted_here += 1
                self._wait_for_reports(scouted_here)
            if not self._options.attack:
                continue
            # 一趟信箱把这一系的报告都读回来，再逐个判定。
            # 只给 `--attack` 不给 `--scout` 时，用的就是信箱里已有的那几封。
            reports = self.collect_scout_reports(pirates)
            for coordinate in pirates:
                self._decide_and_attack(coordinate, reports.get(coordinate))
        return self._outcome

    def _reset_to_known_screen(self, *, attempts: int = 4) -> None:
        """开工先把开着的浮层关掉，让画面回到「地表」或「恒星系」这两种认得出的状态。

        上一轮跑到哪里结束，游戏就停在哪里——实测开工时遇到过信箱、飞行中列表、
        排名面板。`ensure_system_view` 在浮层下面读不到导航栏标签，只会白点三次
        视图菜单（而那个坐标此刻压在浮层底下）。

        每种浮层左上角都是同一个 ✕，所以关浮层这件事不需要认出是哪一种浮层。
        """
        from evo_helper.game.system_navigator import on_system_view

        for _attempt in range(attempts):
            if on_system_view(self._nav_labels()) or self._on_planet_surface():
                return
            self._driver.click(*MAIL_BACK, label="关闭面板")
            self._driver.wait(2.0)

    def _find_pirates(self, galaxy: int, system: int) -> list[Coordinate]:
        pirates: list[Coordinate] = []
        for position in PIRATE_POSITIONS:
            coordinate = Coordinate(galaxy, system, position)
            self._navigator.goto(coordinate)
            if self.is_pirate(coordinate):
                say(f"  {coordinate} 敌对海盗")
                pirates.append(coordinate)
                self._outcome.pirates.append(coordinate)
            else:
                say(f"  {coordinate} 不是海盗")
        return pirates

    def _wait_for_reports(self, count: int) -> None:
        if not count:
            return
        say(f"等 {count} 份侦察报告（{SCOUT_REPORT_WAIT_S:.0f}s）")
        time.sleep(SCOUT_REPORT_WAIT_S)

    def _decide_and_attack(self, coordinate: Coordinate, reading: Any) -> None:
        from evo_helper.vision.scout_reports import VERDICT_ATTACK

        if reading is None:
            say(f"  {coordinate} 读不到侦察报告；跳过")
            self._outcome.refused.append((coordinate, "读不到侦察报告"))
            return
        say(f"  {coordinate} 判定 {reading.verdict}：{reading.trigger_ships}")
        if reading.verdict != VERDICT_ATTACK:
            return
        self._navigator.goto(coordinate)
        if not self.is_pirate(coordinate):
            self._outcome.refused.append((coordinate, "攻击前面板认不出"))
            return
        self.attack(coordinate)


def slow_drag(driver: LiveDriver, from_y: int, to_y: int, *, x: int = 960, steps: int = 12) -> None:
    """面板内慢拖。

    ⚠️ **一步到位的 `dragTo` 会被游戏面板当成点击**——同样的起止点，有时滚有时不滚。
    必须「按下 → 分步移动 → 停一下 → 松开」，让面板收到连续的 mousemove。
    实机上这一条踩了好几次才看明白。
    """
    import random

    driver.focus()
    origin_x, origin_y = driver.origin()
    gui = driver._gui  # noqa: SLF001 - 慢拖需要分步控制，HumanInput 只有一步式 drag
    gui.moveTo(origin_x + x, origin_y + from_y, random.uniform(0.2, 0.4))
    gui.mouseDown()
    time.sleep(random.uniform(0.10, 0.20))
    for index in range(1, steps + 1):
        ratio = index / steps
        gui.moveTo(
            origin_x + x + random.randint(-1, 1),
            origin_y + int(from_y + (to_y - from_y) * ratio),
            random.uniform(0.02, 0.05),
        )
    time.sleep(random.uniform(0.12, 0.25))
    gui.mouseUp()
    time.sleep(1.4)


def _preset_signature(name: str) -> str:
    """预设签名就是标题本身。

    **不展开成舰种清单**：预设内容由用户在游戏里维护，随时会改；把当时的内容
    钉进签名，日后同一个预设就会显示成两个不同的东西。标题才是稳定的那个约定。
    """
    return f"预设:{name}"


def _tesseract_path() -> Any:
    from evo_helper.tools.scan_coordinates import TESSERACT_PATH

    return TESSERACT_PATH


def _ensure_run_row(session_factory: Any) -> UUID:
    """找到（或建好）这条链路自己的运行实例。"""
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    now = datetime.now(UTC)
    with session_factory() as session:
        run = session.scalar(
            select(orm.RunInstance).where(orm.RunInstance.idempotency_key == RUN_KEY)
        )
        if run is not None:
            return UUID(str(run.id))
        plan = session.scalar(select(orm.ScanPlan).where(orm.ScanPlan.name == PLAN_NAME))
        if plan is None:
            plan = orm.ScanPlan(
                name=PLAN_NAME,
                enabled=True,
                time_window_start="00:00",
                time_window_end="23:59",
                dry_run=False,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(plan)
            session.flush()
        run = orm.RunInstance(
            plan_id=plan.id,
            idempotency_key=RUN_KEY,
            state="SCANNING",
            started_at_utc=now,
            created_at_utc=now,
        )
        session.add(run)
        session.commit()
        return UUID(str(run.id))


def parse_system(text: str) -> tuple[int, int]:
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError(f"恒星系要写成 银河:恒星系，例如 2:137（收到 {text!r}）")
    return (int(parts[0]), int(parts[1]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--systems", nargs="+", type=parse_system, required=True)
    parser.add_argument("--scout", action="store_true", help="真的派侦察出去")
    parser.add_argument(
        "--attack",
        action="store_true",
        help="判定为「打」时真的攻击。不配 --scout 时用信箱里已有的侦察报告",
    )
    parser.add_argument("--preset", default=pirate_ui.ATTACK_PRESET_NAME)
    args = parser.parse_args(argv)

    import ctypes

    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    options = LoopOptions(
        systems=tuple(args.systems), scout=args.scout, attack=args.attack, preset=args.preset
    )
    mode = "扫描" if not args.scout else ("侦察+攻击" if args.attack else "只侦察")
    listed = ", ".join(f"{galaxy}:{system}" for galaxy, system in options.systems)
    say(f"模式：{mode}；恒星系 {listed}")

    # 只有 `--scout` / `--attack` 才需要动作能力。开关只有这一处。
    driver = LiveDriver(allow_actions=args.scout or args.attack)
    driver.window()
    loop = PirateLoop(driver, make_ocr(), options)
    outcome = loop.run()

    say(
        f"完成：海盗 {len(outcome.pirates)} 个，侦察 {len(outcome.scouted)} 发，"
        f"攻击 {len(outcome.attacked)} 发，拦下 {len(outcome.refused)} 次"
    )
    for coordinate, reason in outcome.refused:
        say(f"  [拦下] {coordinate} {reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
