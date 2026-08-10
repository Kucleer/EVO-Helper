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
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
)
from evo_helper.domain.report_wait import parse_game_duration
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
from evo_helper.tools.scan_coordinates import LiveDriver, make_ocr, origin, say

#: 这条链路自己的计划与幂等键，与坐标扫描分开：两者的游标含义不同，
#: 共用一个运行实例会让「扫到哪了」和「打到哪了」互相踩。
PLAN_NAME = "海盗侦查攻击循环"
RUN_KEY = "pirate-loop-0001"

# 出发星球（`origin()`，从 `tools.scan_coordinates` 借来）。飞行时间与战报
# 匹配都要它。主星原先在三个文件各写了一遍，改一次要改三处；现在解析只有
# 一份，而且换账号可以用 `EVO_HELPER_ORIGIN` 配。

#: 侦察发在库里的「预设」名。
#:
#: 侦察**不选预设**（派遣面板的终点自动预填），但 `attack_intents.preset_name`
#: 不可空，日志页也要显示点什么。写一个自明的词，而不是借用当次的攻击预设名：
#: 借用的话日志会把一发侦察显示成一发 AAA 攻击，而 `domain.bot_round.phase_of`
#: 只按预设名分探路发和攻击发，看到非探路的名字就当成攻击发。
#: 真正把侦察分出来的是 `mission_kind`，这个名字只管好看和可读。
SCOUT_PRESET_NAME = "侦察"

#: 点「侦察」/「攻击」之后等派遣面板铺开。
DISPATCH_WAIT_S = 2.4

#: 点绿✓之后等简报页出来。
BRIEFING_WAIT_S = 2.6

#: 点「出发！」之后等回到列表。
LAUNCH_WAIT_S = 2.8

#: 侦察报告的等待：实机上 17 秒回报，留足余量再读，读不到就再等一轮。
SCOUT_REPORT_WAIT_S = 45.0
SCOUT_REPORT_RETRIES = 3

#: 简报上的飞行时间超过这个上界，就当**读错了**，回程闹钟写 NULL。
#:
#: 这道护栏补的是 `_read_flight_time` 拿不到的那道交叉校验：`DispatchBriefing`
#: 本来用「绝对到达时间 vs 当前时间+时长」互相验（见 `duration_agrees`），
#: 而这里只读时长这一个来源，读错了没有第二处能揭穿它。
#:
#: 危险的不是读不出来——那返回 None，走「立即尝试收取」，白跑一趟而已。
#: 危险的是**读出一个能解析但偏大的值**：`parse_game_duration` 同时认
#: `X天Y时Z分W秒` 和 `01:53:19`，`8分3秒` 被读成 `8时3分` 就是 60 倍，
#: 于是调度器安安静静等 8 小时，那条链路整整停摆且不报错。
#:
#: 取 6 小时的依据：
#: - 这个方法只在 `attack()` 里调用，而这条链路打的是**同系目标**
#:   （主星 2:137:18 → 2:137:x），飞行按分钟计。
#: - 仓库里最长的一份实测简报是 `28分 21秒`，而那还是一趟**深空探索**——
#:   比这条链路任何一发都远得多。6 小时留了十倍以上余量。
#: - 反过来它拦得住最典型的量级错：任何真实时长 ≥6 分钟的一发，
#:   被「分」读成「时」之后都超过上界；带「天」的误读一律超过。
#: - 战报有有效期（见 `report_wait.MAX_SESSION_BACKOFF` 的注释），
#:   真等到 6 小时之后也多半已经读不到了，放弃这个值没有实际损失。
#:
#: 误杀的代价是可接受的那一侧：把一次合法的长途飞行判成读错，
#: 只是让助手立刻去收一次、扑空、退出。
MAX_CREDIBLE_FLIGHT = timedelta(hours=6)

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


#: 借 `scan_coordinates` 那一份，不再各写一遍。它是编码安全的——
#: 实机上 `print` 一个 OCR 读出来的 `™` 就把整个 runner 弄崩过，见那边的注释。


class RoundExhausted(RuntimeError):
    """这一轮没料了：舰队全在外面，或者航线占满。

    **这不是失败。** 抛到 `run()` 就正常收尾、退出码 0——调度器据此不计入连续
    失败计数。反过来当成失败的话：航线占满是必然会发生的事，连撞三次就把整条
    链路自动停用了，而它其实只是需要等舰队飞回来。
    """


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

    #: 这条链路打的是什么目标。子类覆盖它——`BotLoop` 走的是同一套写库路径，
    #: 标签却必须不同：海盗每天 32 次是游戏硬限制，两者混在一起会数错配额。
    TARGET_KIND: str = TARGET_KIND_PIRATE

    #: 行星面板上「攻击」按钮的位置。**必须由子类按目标类型覆盖**：无主星球
    #: （敌对海盗）和有主星球（bot）的面板是两套完全不同的布局，见
    #: `pirate_ui.BOT_ATTACK_BUTTON` 的注释。
    ATTACK_BUTTON: tuple[int, int] = pirate_ui.ATTACK_BUTTON

    def __init__(self, driver: LiveDriver, ocr: Any, options: LoopOptions) -> None:
        self._driver = driver
        self._ocr = ocr
        self._options = options
        self._navigator = SystemNavigator(driver)
        self._outcome = Outcome()
        self._repository: SqlAlchemyRepository | None = None
        self._run_id: UUID | None = None
        self._session_keeper: Any = None

    # -- 读屏 ---------------------------------------------------------------

    def _read(
        self,
        roi: tuple[int, int, int, int],
        *,
        digits: bool = False,
        upscale: int = 3,
        threshold: int | None = None,
    ) -> str:
        """读一块 ROI。

        `threshold` 是二值化阈值。多数行不需要，但有些行不二值化就是读不出来
        ——飞行时间那一行是绿字压在蓝底上，见 `pirate_ui.FLIGHT_RECIPES`。
        参数加在这里而不是另开一个读屏方法：多一条读屏路径就会绕过调用方的
        桩，也就是「同一件事两份实现」。
        """
        self._ensure_geometry()
        return crop_reader(self._driver.capture(), self._ocr)(
            roi, digits=digits, upscale=upscale, threshold=threshold
        )

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

    def _dialog(self) -> str | None:
        """当前屏上有没有那种单按钮弹窗；有就返回贴回词表之后的文案。

        三个弹窗共用同一个框、同一个绿 ✓，只有文字不同，所以判据只能是文字。
        贴不上返回 None——**这不等于「没有弹窗」**，也可能是个没见过的新弹窗；
        那种情况由既有的那几道闸门（简报任务类型、面板标题）去挡。
        """
        return pirate_ui.snap_dialog(self._read(pirate_ui.DIALOG_TEXT_ROI))

    def _handle_dialog(self, coordinate: Coordinate) -> bool:
        """认出弹窗就关掉它，并决定这一轮还能不能继续。

        返回 True 表示「没有弹窗，照常往下走」；False 表示「这个目标跳过」。
        资源耗尽则抛 `RoundExhausted`——那不是失败，是这一轮没料了。

        ⚠️ 三个弹窗**分两类，处理方式相反**。把「没有可执行的任务」也当成停轮，
        一个被别人打过、正在保护期里的目标就能让整轮空转，而它后面可能还排着
        一堆能打的。
        """
        message = self._dialog()
        if message is None:
            return True
        self._driver.click(*pirate_ui.DIALOG_CONFIRM, label="关闭弹窗")
        self._driver.wait(DISPATCH_WAIT_S)
        if message == pirate_ui.DIALOG_NO_MISSION:
            say(f"  {coordinate} 在保护期内（{message}）；跳过这个目标")
            self._outcome.refused.append((coordinate, message))
            return False
        raise RoundExhausted(message)

    def _read_flight_time(self) -> timedelta | None:
        """把简报上的飞行时间读下来，**必须在点「出发！」之前**。

        点完出发这一屏就没了，而这个时长是助手松手之后唯一的回程闹钟
        （见 `domain.report_wait` 的模块头）。对应的那一列此前从来没被写入过——
        实测库里 4 条派遣全是 NULL，于是整个「派出后松手、到点回来收战报」是死的。

        和任务类型那道闸门一样**要等它铺开**：页面是滑进来的，读一次读不到
        不代表这一行不存在（`_briefing_mission` 的注释记着同一个坑）。

        读不出来返回 None，而**不是**拦下这一发：飞行时间只是闹钟，不是闸门。
        为它加一道闸门等于让一次 OCR 抖动就废掉一发完全正常的攻击——
        这条链路已经因为「ROI 与放大倍数不配」白白拦下过四发。

        读出来但大得离谱的，同样返回 None（见 `MAX_CREDIBLE_FLIGHT`）。

        ⚠️ **只返回时长，不拼一个 `DispatchBriefing` 出来。** 那个类型带着
        `mission_type` 与绝对到达时间两个字段，而这里两样都没有证据：任务类型的闸门
        在这之后才跑，绝对到达时间的 ROI（`BRIEFING_ARRIVAL_ROI`）还没标定。
        硬填的话 `duration_agrees()` 会变成 `now+flight` 和 `now+flight` 相比——
        一道交叉校验降级成同义反复，比没有更糟：下一个人会以为它验过了。
        正因为这里拿不到第二个来源，才需要 `MAX_CREDIBLE_FLIGHT` 那道上界。
        """
        flight: timedelta | None = None

        def read_once() -> bool:
            nonlocal flight
            # 逐个配方试。**必须二值化**：这一行是绿字压在蓝底上，灰度化之后
            # 对比度不够，调用方原先用的默认（3× 不二值化）在实机上读出来是
            # `'-'`——见 `pirate_ui.FLIGHT_RECIPES` 的注释。
            for upscale, threshold in pirate_ui.FLIGHT_RECIPES:
                text = self._read(
                    pirate_ui.BRIEFING_FLIGHT_ROI, upscale=upscale, threshold=threshold
                )
                flight = parse_game_duration(text)
                if flight is not None:
                    return True
            return False

        if not self._settle(read_once) or flight is None:
            say("  简报上读不到飞行时间；这一发照派，回程闹钟留空")
            return None
        if flight > MAX_CREDIBLE_FLIGHT:
            # 宁可白跑一趟，也不要安安静静等一个读错的钟。
            say(
                f"  简报上的飞行时间读作 {flight}，超过 {MAX_CREDIBLE_FLIGHT} 的上界；"
                "当读错处理，回程闹钟留空"
            )
            return None
        return flight

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
        # ⚠️ **点完「出发！」不等于派出去了。** 航线占满时游戏在这里弹
        # 「同时派遣的舰队数量已达上限。」，而这一发根本没飞。不检查的话调用方会
        # 记下一条**根本不存在的派遣**：调度器据此以为一条航线被占着，等一份永远
        # 不会来的战报，要到 `MAX_REPORT_AGE`（6 小时）才被判缺失清掉。
        return self._handle_dialog(coordinate)

    def scout(self, coordinate: Coordinate) -> bool:
        """派一发侦察。派遣面板的终点是自动预填的，侦察也不需要选预设。

        **侦察一样要记账。** 它占航线（而且会飞回来，2× 返航），一条记录都不写
        的话，一轮最多 4 发侦察对调度器完全隐形：它以为航线空着就去派攻击，
        撞上游戏的「同时派遣的舰队数量已达上限。」。写进去时 `mission_kind`
        必须是 `SCOUT`——日配额只按 `target_kind` 过滤，照攻击发记会让每一发
        侦察吃掉一次当日攻击额度。

        意图与派遣的先后和 `attack()` 一个语义：意图在点「出发！」之前写，
        派遣在之后写，两者之差就是「想派但被闸门拦下了」。
        """
        self._driver.click(*pirate_ui.SCOUT_BUTTON, label="侦察")
        self._driver.wait(DISPATCH_WAIT_S)
        self._driver.click(*pirate_ui.DISPATCH_CONFIRM, label="确认终点")
        self._driver.wait(BRIEFING_WAIT_S)
        # 绿✓ 之后出来的未必是简报页：目标在保护期、或者一条战舰都选不出来时，
        # 这里弹的是那种单按钮弹窗。**先认再走**，而且要在记意图之前。
        if not self._handle_dialog(coordinate):
            self._leave_dispatch_list()
            return False
        intent_id = self._record_intent(coordinate, preset=SCOUT_PRESET_NAME)
        # ⚠️ **这一行必须留在 `_launch` 之前**，理由与 `attack()` 里那一行相同：
        # 点完「出发！」简报页就没了。不读的话 `line_free_at_utc` 恒为 NULL，
        # 而 NULL 的既定语义是**不计入在飞数**——记了账等于没记，那 4 条侦察航线
        # 对调度器仍然完全隐形。
        #
        # 侦察简报是同一块面板（只是「任务类型」显示为侦察），所以 ROI 沿用
        # `BRIEFING_FLIGHT_ROI`。⚠️ 万一它在侦察简报上对不上：读不出来会先走
        # `_read_flight_time` 里 `_settle` 的重试（约 3 秒），`_launch` 里还会再走
        # 一遍，于是**每发侦察多花约 6 秒、一轮 4 发就是 24 秒**。那是 ROI 没对上的
        # 症状，不是别的毛病——第一次实机发现侦察变慢，先去核这个 ROI。
        flight = self._read_flight_time()
        if not self._launch(coordinate, "侦察"):
            self._leave_dispatch_list()
            return False
        self._record_dispatch(intent_id, flight, mission_kind=MISSION_KIND_SCOUT)
        self._outcome.scouted.append(coordinate)
        say(f"  已派出侦察 → {coordinate}")
        # 派出之后停在「飞行中」列表上，必须自己退出来。
        self._leave_dispatch_list()
        return True

    def attack(self, coordinate: Coordinate, *, preset: str | None = None) -> bool:
        """用预设攻击。闸门是「预设标题选中了」与「简报写着攻击」。

        `preset` 允许按次指定：bot 那条链路先用「探路」做攻击侦查，再按分档换预设
        （见 `tools.bot_loop`），而海盗链路始终用同一个。

        **只按标题选预设，不读预设内容**（用户口径 2026-08-09）：内容是用户自己在
        游戏里维护的，助手去核对既多余、也会把「用户改了预设」误判成故障。
        """
        wanted = preset or self._options.preset
        self._driver.click(*self.ATTACK_BUTTON, label="攻击")
        self._driver.wait(DISPATCH_WAIT_S)

        picker = PresetPicker(driver=self._driver, read_names=self._preset_names)
        try:
            picker.pick(wanted)
        except PresetNotFound as error:
            say(f"  {error}；关掉面板，不打这一发")
            self._driver.click(*pirate_ui.DISPATCH_CLOSE, label="关闭派遣面板")
            self._driver.wait(DISPATCH_WAIT_S)
            # 派遣面板开过之后导航栏里是什么已经不可知了，和 `_leave_dispatch_list`
            # / `_close_mail` 同理。**这一处原来漏了**，代价是实机上最贵的一次故障：
            # 缓存仍以为停在原坐标，于是下一个目标的 `goto` 跳过「重设银河系」，
            # 那一下「设恒星系」落到了银河系框上，游戏把 136 截断成最大值 9。
            # 此后导航栏是 9:137，而缓存说 2:137——银河系再也不会被重设，连续
            # 44 个目标坐标核对全不过，13 分钟一发没派。
            self._navigator.invalidate()
            self._outcome.refused.append((coordinate, f"找不到预设 {wanted}"))
            return False

        self._driver.click(*pirate_ui.DISPATCH_CONFIRM, label="确认终点")
        self._driver.wait(BRIEFING_WAIT_S)
        # 绿✓ 之后出来的未必是简报页：目标在保护期、或者一条战舰都选不出来时，
        # 这里弹的是那种单按钮弹窗。**先认再走**，而且要在记意图之前。
        if not self._handle_dialog(coordinate):
            self._leave_dispatch_list()
            return False
        intent_id = self._record_intent(coordinate, preset=wanted)
        # ⚠️ **这一行必须留在 `_launch` 之前。** 点完「出发！」简报页就没了，
        # 挪到后面读，四次重试全会落空，飞行时间永久恒为 NULL——而且一声不响，
        # 看起来只是「一直在等」。
        flight = self._read_flight_time()
        if not self._launch(coordinate, "攻击"):
            self._leave_dispatch_list()
            return False
        self._record_dispatch(intent_id, flight)
        self._outcome.attacked.append(coordinate)
        say(f"  已发动攻击 → {coordinate}（预设 {wanted}）")
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

        # 先把浮层关掉再切地表。`_on_planet_surface()` 的**正面凭据是右上角那个未读
        # 数**，而它自己的注释就写着「浮层会盖住它」——`_goto_planet_surface` 却不关
        # 浮层，只会反复点视图菜单（而那个坐标此刻压在浮层底下）。
        #
        # 这一步偏偏紧跟在 `_wait_for_reports` 的 45 秒等待之后，正是舰队返航之类的
        # 通知最容易冒出来的时刻。实机（2026-08-11 02:10 / 03:35 / 03:46）三次都倒在
        # 这里，而每次都已经先派出 4 发侦察——报告读不到，那 4 发就白飞。
        self._reset_to_known_screen()
        if not self._goto_planet_surface():
            # 判据失败时最贵的事是「不知道当时画面长什么样」。存一帧的成本是一次写盘。
            self._dump_frame("planet-surface-unreachable", MAIL_BADGE_ROI)
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

    def _record_intent(self, coordinate: Coordinate, *, preset: str | None = None) -> UUID:
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
                origin=origin(),
                target=coordinate,
                preset=FleetPresetRef(
                    name=preset or self._options.preset,
                    signature=_preset_signature(preset or self._options.preset),
                ),
                cycle_start_utc=now,
                created_at_utc=now,
                target_kind=self.TARGET_KIND,
            )
        )
        return intent_id

    def _record_dispatch(
        self,
        intent_id: UUID,
        flight: timedelta | None,
        *,
        mission_kind: str = MISSION_KIND_ATTACK,
    ) -> None:
        """记下这一发，并把简报上的飞行时间存成回程闹钟。

        读不到时写 NULL——`ReportWaitPlanner` 把「未知」当成「立即尝试收取」，
        而不是无限等一个不知道何时抵达的战报。

        `mission_kind` 默认攻击。侦察发必须显式传 `SCOUT`：它占航线但不消耗
        当日 32 次的攻击配额，也不会产生战报，三笔账靠这一个字段分开。
        """
        repository, _run_id = self._ensure_run()
        dispatch_id = uuid4()
        dispatched_at = datetime.now(UTC)
        repository.save_dispatch(
            AttackDispatch(
                dispatch_id=dispatch_id,
                intent_id=intent_id,
                dispatched_at_utc=dispatched_at,
                accepted=True,
                mission_kind=mission_kind,
            )
        )
        repository.record_flight_time(dispatch_id, flight, dispatched_at)

    # -- 会话 ---------------------------------------------------------------

    def _keeper(self) -> Any:
        """惰性建一个会话守护，整轮共用一个（它内部按时间节流巡检）。"""
        from evo_helper.tools.scan_coordinates import make_session_keeper

        if self._session_keeper is None:
            self._session_keeper = make_session_keeper(self._driver, self._ocr)
        return self._session_keeper

    def _ensure_session(self, *, force: bool = False) -> bool:
        """确认会话还在；掉了就接回去。返回「刚刚重连过」。

        **必须排在切视图之前。** 顺序反了会这样（`run_scan` 里有同一段注释，
        这两条链路当时漏抄了）：会话掉了的时候画面停在入口页或 START 页，导航栏
        标签自然读不到，`ensure_system_view` 于是朝视图菜单坐标盲点三次然后放弃，
        **永远走不到能重连的 SessionKeeper**。

        实机（2026-08-11 02:10）：会话在海盗那轮读信箱时掉了，报「切不到自己星球
        地表」；调度器接着起 bot，bot 对着登录页把 80 个目标一个个试，每个 ~35 秒
        ——45 分钟白点，日志里全是「坐标核对不过：面板读作 ''」。留下的现场图上
        是 START 登录页。

        重连之后一定要清导航缓存：那份记忆记的是掉线前的坐标。
        """
        from evo_helper.game.session_keeper import ScreenState

        session = self._keeper().ensure_connected(force=force)
        if session is None:
            return False
        if session.state is ScreenState.UNKNOWN:
            # 认不出**多半是浮层压着导航条**（信箱、飞行中列表、派遣面板），不是
            # 掉线：`classify_screen` 把登录页判成 ENTRY/START，落不到 UNKNOWN。
            # 所以先把浮层关掉再问一次，而不是当场判死——上一轮停在哪个面板就能
            # 让下一轮开不了工。实机 02:24 就是这么报「会话不可用：unrecognised
            # screen」的，而那时会话好好的。
            #
            # 这里对 UNKNOWN 放行去点关闭键，并没有破坏「认不出的画面绝不点击」：
            # 真掉线时画面是 ENTRY/START/DISCONNECTED，走的是守护自己的入口序列。
            say("  画面认不出（多半是浮层）；关掉浮层后重新巡检")
            self._reset_to_known_screen()
            session = self._keeper().ensure_connected(force=True)
        if session is None or not session.ready:
            detail = session.detail if session else "巡检没返回结果"
            raise RuntimeError(f"会话不可用：{detail}；安全停止")
        if session.reconnected:
            say("已重新登录")
            self._navigator.invalidate()
            return True
        return False

    # -- 主循环 -------------------------------------------------------------

    def run(self) -> Outcome:
        # 几何先校一遍。窗口被改过尺寸时所有坐标一起失效，而这件事悄无声息——
        # 本轮开工时窗口就是 1536×733，照 1920×917 的坐标点下去全落在别处。
        from evo_helper.game.game_window import ensure_game_window

        ensure_game_window()
        self._ensure_session(force=True)
        self._reset_to_known_screen()
        if not self._navigator.ensure_system_view(self._nav_labels):
            raise RuntimeError("切不到恒星系视图；停止而不是往固定坐标乱点")

        try:
            self._sweep()
        except RoundExhausted as exhausted:
            # 资源耗尽**不是失败**：正常收尾、退出码 0。当成失败的话，航线占满
            # （必然会发生）连撞三次就把整条链路自动停用了，而它只是需要等舰队
            # 飞回来。调度器看到 0 就只走冷却，到点再来。
            say(f"这一轮到此为止：{exhausted}")
        return self._outcome

    def _sweep(self) -> None:
        for galaxy, system in self._options.systems:
            say(f"恒星系 {galaxy}:{system}")
            pirates, scouted_here = self._find_pirates(galaxy, system)
            if not pirates:
                say("  1–4 位没有敌对海盗")
                continue
            if self._options.scout:
                self._wait_for_reports(scouted_here)
            if not self._options.attack:
                continue
            # 一趟信箱把这一系的报告都读回来，再逐个判定。
            # 只给 `--attack` 不给 `--scout` 时，用的就是信箱里已有的那几封。
            reports = self.collect_scout_reports(pirates)
            for coordinate in pirates:
                self._decide_and_attack(coordinate, reports.get(coordinate))

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

    def _find_pirates(self, galaxy: int, system: int) -> tuple[list[Coordinate], int]:
        """走一遍 1–4 位；开了 `--scout` 就**当场**把侦察发出去。

        返回 (认出的海盗, 已派出的侦察数)。

        以前这里只管认，认完回到 `_sweep` 再对每个海盗 `goto` 一次才侦察。两趟
        导航的代价不只是慢一倍：实测首发侦察要等到开跑后 **68 秒**，而这 68 秒
        里日志只有几行「敌对海盗」，从外面看不出它到底在不在干活。用户据此判定
        「侦查和攻击都没触发」，43 秒就把进程停了——那一轮确实一发都没派出去，
        但原因是还没轮到派，不是派不出去。

        认出海盗的那一刻，面板已经开着、侦察按钮就在眼前，没有任何理由先走开再
        回来。融合之后首发提前到 ~25 秒，链路本身一行没改。
        """
        pirates: list[Coordinate] = []
        scouted = 0
        for position in PIRATE_POSITIONS:
            coordinate = Coordinate(galaxy, system, position)
            self._navigator.goto(coordinate)
            if not self.is_pirate(coordinate):
                say(f"  {coordinate} 不是海盗")
                continue
            say(f"  {coordinate} 敌对海盗")
            pirates.append(coordinate)
            self._outcome.pirates.append(coordinate)
            # 站在这颗星球上就把侦察发掉。`scout()` 抛 RoundExhausted 时直接往上
            # 传到 `run()`：那是「资源耗尽、这一轮到此为止」，不是失败。
            if self._options.scout and self.scout(coordinate):
                scouted += 1
        return pirates, scouted

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
    from evo_helper.tools.scan_coordinates import tesseract_path

    return tesseract_path()


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
