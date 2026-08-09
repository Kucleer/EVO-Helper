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
from evo_helper.game.system_navigator import NAV_LABEL_ROI, SystemNavigator, crop_reader
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

#: 地表视图顶部的星球名横条，用来确认「在不在地表」。
#: 恒星系视图、派遣面板、飞行中列表上都读不到它。
PLANET_TITLE_ROI = (880, 55, 1050, 90)
PLANET_TITLE_TEXT = "奥格瑞玛"

#: 信箱标题，用来确认信箱真的开了。
MAIL_TITLE_ROI = (890, 55, 1040, 92)
MAIL_TITLE_TEXT = "邮箱"

#: 信箱「报告」标签、邮件首行中心与行距（917 空间）。
MAIL_REPORT_TAB = (897, 178)
MAIL_FIRST_ROW_Y = 285
MAIL_ROW_PITCH = 86
MAIL_ROW_X = 900

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
        return crop_reader(self._driver.capture(), self._ocr)(roi, digits=digits, upscale=upscale)

    def _nav_labels(self) -> str:
        return self._read(NAV_LABEL_ROI)

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
        return pirate_ui.snap_mission(self._read(pirate_ui.BRIEFING_MISSION_ROI)) or ""

    def _launch(self, coordinate: Coordinate, mission: str) -> bool:
        """简报页核对任务类型，通过才点「出发！」。"""
        shown = self._briefing_mission()
        if shown != mission:
            say(f"  简报写的是 {shown or '（读不出）'}，不是{mission}；不点出发")
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

    def read_scout_report(self, coordinate: Coordinate) -> Any:
        """去信箱把这个坐标的侦察报告读回来。读不到返回 None。

        路径：自己星球地表 → ✉ → 「报告」标签 → 最上面那封侦察报告 →
        慢拖两次到战舰清单 → 读。
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
        # 「报告」标签页按时间倒序，侦察报告在最上面几行。逐行找发件人 Aries。
        for row in range(3):
            self._driver.click(
                MAIL_ROW_X, MAIL_FIRST_ROW_Y + row * MAIL_ROW_PITCH, label="打开邮件"
            )
            self._driver.wait(2.4)
            header = screens()
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            ships = screens()
            try:
                reading = read_pirate_scout(header, ships, expected_target=coordinate)
            except ScoutReportUnreadable as error:
                say(f"  第 {row} 行不是这个目标的侦察报告：{error}")
                self._driver.click(*MAIL_BACK, label="返回")
                self._driver.wait(2.0)
                continue
            # 读到了也要先从详情页退回列表，再关信箱。少退一层就会在列表页上
            # 去点视图菜单——那个坐标此刻压在信箱面板下面。
            self._driver.click(*MAIL_BACK, label="返回")
            self._driver.wait(2.0)
            self._close_mail()
            return reading
        self._close_mail()
        return None

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
        if MAIL_TITLE_TEXT not in self._read(MAIL_TITLE_ROI):
            raise RuntimeError("点了信箱却没读到「邮箱」标题；停止而不是继续盲点")
        self._driver.click(*MAIL_REPORT_TAB, label="报告标签")
        self._driver.wait(2.0)

    def _on_planet_surface(self) -> bool:
        """在不在自己星球的地表视图上。

        判据是地表视图右上角那个信箱按钮旁边的星球名横条——恒星系视图、
        派遣面板、飞行中列表上都读不到它。
        """
        return PLANET_TITLE_TEXT in self._read(PLANET_TITLE_ROI)

    def _goto_planet_surface(self) -> bool:
        """从恒星系视图切回自己星球地表。切不过去返回 False。

        ⚠️ **这一步还没标定完，所以现在一定返回 False（于是调用方安全停止）。**
        实机确认（2026-08-09）：点底部导航的「行星」`NAV_PLANET` 打开的是
        **行星列表浮层**（我的三颗星球各一行：奥格瑞玛 [2:137:18]、风暴哨壁 [9:250:8]、
        纳克萨玛斯 [4:96:7]），每行右侧有一排图标，其中「前往此处」才是去地表的那个。
        那个图标的坐标还没量，量之前不许往那一排图标上点——同一排里还有
        「运输 / 部署 / 传送 / 转移 / 投送 / 保护 / 扩张」，点错任何一个都是真实操作。

        标定方法：`--systems 2:137`（只扫不派）跑到这里，用
        `grab_template.py --region` 量「奥格瑞玛」那一行的「前往此处」中心。
        """
        if self._on_planet_surface():
            return True
        self._driver.click(*pirate_ui.NAV_PLANET, label="行星")
        self._driver.wait(2.6)
        if self._on_planet_surface():
            return True
        # 开出来的是行星列表浮层。关掉它，别把游戏留在一屏全是真实操作的界面上。
        self._driver.click(*MAIL_BACK, label="关闭面板")
        self._driver.wait(2.0)
        return False

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
        if MAIL_TITLE_TEXT in self._read(MAIL_TITLE_ROI):
            # 还在信箱里说明刚才那一下退的是详情页，再退一层。
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
        if not self._navigator.ensure_system_view(self._nav_labels):
            raise RuntimeError("切不到恒星系视图；停止而不是往固定坐标乱点")

        for galaxy, system in self._options.systems:
            say(f"恒星系 {galaxy}:{system}")
            pirates = self._find_pirates(galaxy, system)
            if not pirates:
                say("  1–4 位没有敌对海盗")
                continue
            if not self._options.scout:
                continue
            for coordinate in pirates:
                self._navigator.goto(coordinate)
                if not self.is_pirate(coordinate):
                    continue
                self.scout(coordinate)
            if not self._options.attack:
                continue
            self._wait_for_reports(len(self._outcome.scouted))
            for coordinate in list(self._outcome.scouted):
                self._decide_and_attack(coordinate)
        return self._outcome

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

    def _decide_and_attack(self, coordinate: Coordinate) -> None:
        from evo_helper.vision.scout_reports import VERDICT_ATTACK

        reading = self.read_scout_report(coordinate)
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
    parser.add_argument("--attack", action="store_true", help="判定为「打」时真的攻击")
    parser.add_argument("--preset", default=pirate_ui.ATTACK_PRESET_NAME)
    args = parser.parse_args(argv)

    if args.attack and not args.scout:
        parser.error("--attack 需要 --scout：没有侦察报告就没有判定依据")

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
