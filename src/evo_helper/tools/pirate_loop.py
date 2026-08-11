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
from enum import Enum
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

# `vision.parsers` 只依赖标准库与 domain，没有 Pillow / pytesseract，
# 所以可以在模块顶层导入；真正带可选依赖的 `vision.optional.*` 仍旧惰性导入。
from evo_helper.vision.parsers import (
    GAME_DISPLAY_ZONE,
    REPORT_TIME_RE,
    ReportKind,
    classify_report_subject,
    parse_report_timestamp,
)

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

#: 一屏能整行看到的邮件行数（第 7 行被切掉）。
#:
#: ⚠️ 这是**读得到主题**的行数，不再是「要打开几封」。两者原先是同一个数，
#: 于是一趟信箱把 6 次「点开-等-OCR-返回」全花在盲开上：今天打出去的攻击报告
#: 不断把侦察报告挤出前 6 行，而那 6 次开封仍旧照开不误。现在先在列表页读主题
#: （便宜），只把开封预算花在**主题看着对得上**的行上，见 `_scan_mail_rows`。
MAIL_SCAN_ROWS = 6

#: 一趟信箱最多往下翻几屏（每屏 `MAIL_SCAN_ROWS` 行，所以最远能看到 24 行）。
#:
#: 翻屏靠慢拖，而列表的滚动步距**没有标定过**，所以停止条件不能是「拖了几次」。
#: 这里的停止条件是「这一屏还有没有没见过的行」，行的身份取自它自己的主题+时间
#: ——和「认报告靠 VS 块里的坐标、不靠行号」同一个道理。拖少了只是重看几行
#: （在列表页认出来就跳过，不开封），拖多了漏掉的那几行和今天的行为一样。
#:
#: 取 4 的依据是**时长要和改之前持平**：读一屏主题 ≈1–2 秒、翻一屏 ≈2 秒，
#: 四屏约 12–16 秒；加上 `MAIL_MAX_OPENS` 封开封（≈64 秒），最坏约 80 秒，
#: 与实机上原先那趟盲开 6 行的 83 秒（09:30:11→09:31:34）在同一档。
#: 也就是说窗口从 6 行放大到 24 行，是**用筛掉的开封省出来的**，不是加时间换的。
MAIL_SCAN_PAGES = 4

#: 一趟信箱最多**打开**几封。
#:
#: 开一封 ≈ 8 秒（点开等 2.4s + 认屏 + 详情 OCR + 返回等 2.0s），而读一屏 6 行
#: 主题只要一次截图加六次窄 ROI OCR。上限保证一趟的时长有界：主题筛偏了最多
#: 多花几十秒，而不是把整轮拖垮。取 8 是因为两条链路一轮各自最多在等 6–8 份
#: 报告（海盗一系 4 发侦察、bot 一轮 6 发探路）。
MAIL_MAX_OPENS = 8

#: 开工对账时最多往下翻几屏。**一封都不打开。**
#:
#: 正常的停止条件不是这个上限，而是「翻到了今天 UTC 00:00 之前的那一行」——
#: 列表按时间倒序，再往下都是昨天的，与今天的配额无关。上限只是兜底：时间
#: 读不出来时上面那条停不下来。8 屏 ≈ 48 行，是当日 32 次攻击配额的 1.5 倍，
#: 够覆盖一个守规矩的白天；读不到底只会让计数偏小，而偏小的方向是安全的
#: （见 `storage.repository.count_dispatches_since` 的取大规则）。
RECONCILE_MAX_PAGES = 8

#: 点开一封邮件之后先等这么久，然后才开始判「详情页铺开了没有」。
MAIL_OPEN_WAIT_S = 2.4

#: 从详情页退回列表之后等这么久。
MAIL_BACK_WAIT_S = 2.0

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


class TargetCheck(Enum):
    """站到一个坐标上、面板铺开之后，看到的是三种情况之一。

    ⚠️ **三值不是为了好看。** `ABSENT` 与 `MISMATCH` 都让调用方「这一位不打」，
    但成因相反，善后也必须相反：

    - `ABSENT`：面板是请求的那一位，只是上面没有要找的东西。海盗链路上这是
      **最常见的正常结果**（1–4 位里没有海盗是家常便饭）。当成异常去复位重试，
      每个空位都要多付一次复位+重导航，整轮慢一倍。
    - `MISMATCH`：面板是真的，但它显示的不是请求的那一位——导航漂了。
      `SystemNavigator` 只重设它**认为变了**的字段，一旦那份记忆和导航栏实际值
      分了岔，它再也不会自己纠回来（判「一样」用的就是那份错记忆）。实机
      2026-08-11：一次「设恒星系」落到银河系框上，136 被截断成 9，此后导航栏是
      `[9:137:12]` 而缓存说 `2:137`，连续 44 个目标坐标核对全不过。
      这一类必须走 `_goto_checked` 的自愈（清缓存后重来），否则只会一路
      静默地报「不是海盗」把整轮走完，而且从日志上看不出异常。

    ⚠️ **判据本身一个字都不许放松。** 那一轮里有一次面板读到的是上一个目标的
    星系（请求 2:321:5，面板 2:320:5），核对拦对了；放松成「位次对上就行」
    就是往错误的星球扔舰队。能改的只是核对不过之后怎么办。
    """

    #: 面板显示的就是请求的那一位，而且是要找的目标。
    CONFIRMED = "认出目标"
    #: 面板显示的就是请求的那一位，只是上面没有要找的目标。
    ABSENT = "不是目标"
    #: 面板是真的，但显示的不是请求的那一位。
    MISMATCH = "坐标核对不过"


@dataclass(frozen=True)
class MailRow:
    """列表页上的一行邮件：**没打开之前**能知道的全部。

    只有主题、时间和由主题得来的分类。归属判定（这是谁的报告）一律等打开之后
    再由报告自己的内容决定——列表行上根本没有坐标（`parse_mail_rows_v2` 的
    docstring 写着同一条）。
    """

    #: 这一屏里的行号（0 起）。点击坐标要用它，**不能跨屏用**。
    index: int
    #: 去掉时间那一行之后剩下的文字，交给 `classify_report_subject` 认类型。
    subject: str
    raw_time_text: str | None
    reported_at_utc: datetime | None
    kind: ReportKind

    @property
    def identity(self) -> tuple[str, str]:
        """跨屏认出「这一行我见过」用的身份。

        取主题+时间而不是行号：翻屏之后行号会重来一遍，而同一封邮件的主题和
        时间不变。时间读不出来时只按主题去重——那会把同一分钟的两封同类报告
        看成一封，代价是少开一封；反过来（不去重）则是翻屏后把上一屏重开一遍，
        每重复一封就白花八秒。
        """
        return (self.subject, self.raw_time_text or "")

    def may_be(self, wanted: ReportKind) -> bool:
        """这一行**值不值得打开**。

        只有主题明确读成了别的类型才跳过；读不出、认不出（`UNKNOWN`）一律照开。

        判据刻意往「开」的一侧倒：漏开一封 = 这一轮少一份报告（侦察白飞、
        探路白派），多开一封 = 多花八秒。真正的归属判定在打开之后（VS 块里的
        坐标 / 报告开头那行「已对 [x:y:z] 完成侦察」），主题只用来排掉**明摆着
        不是**的那些——实机上那恰恰是最多的一类：一整屏的攻击报告。
        """
        return self.kind is wanted or self.kind is ReportKind.UNKNOWN

    def is_older_than(self, moment: datetime | None) -> bool:
        """这一行是不是已经比 `moment` 还旧。**时间读不出来时一律返回 False。**

        列表按时间倒序，所以翻到第一行「比要找的那几发还早」的报告就可以收工——
        往下不可能再有本轮的报告。这是把窗口开大之后仍然不会翻一整个信箱的那道闸门。

        读不出时间就不敢停：停错的代价是把还没翻到的报告永久判成「不在信箱里」，
        而多翻一屏只是多花一两秒。
        """
        return (
            moment is not None
            and self.reported_at_utc is not None
            and self.reported_at_utc < moment
        )


def mail_row_from_text(index: int, text: str) -> MailRow:
    """把一行邮件的 OCR 文字读成 `MailRow`。

    主题**不取第 0 行**：`--psm 6` 在这块 ROI 上不保证行序，而时间那一行的形状
    （`DD/MM/YYYY HH:MM:SS`）是唯一确定的。所以先把时间行认出来剔掉，
    剩下的全部拼起来当主题——`classify_report_subject` 是子串判定，
    多拼几个字不会改变结论，而漏掉主题那一行会。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    raw_time = next((line for line in lines if REPORT_TIME_RE.search(line)), None)
    subject = " ".join(line for line in lines if line != raw_time)
    match = REPORT_TIME_RE.search(raw_time) if raw_time is not None else None
    return MailRow(
        index=index,
        subject=subject,
        raw_time_text=match.group(0) if match is not None else None,
        reported_at_utc=(
            parse_report_timestamp(match.group(0), GAME_DISPLAY_ZONE) if match is not None else None
        ),
        kind=classify_report_subject(subject),
    )


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

    #: 哪些判定值得「复位画面 → 清缓存 → 重新导航」自愈一次（见 `_goto_checked`）。
    #:
    #: 海盗这边**只对 `MISMATCH` 自愈**：`ABSENT`（这一位没有海盗）是最常见的
    #: 正常结果，把它也算进来等于每个空位都多付一次复位+重导航，整轮慢一倍。
    #: `BotLoop` 覆盖了它——那边的目标是扫描库里已知的 bot，认不出本身就是异常。
    RETRY_CHECKS: frozenset[TargetCheck] = frozenset({TargetCheck.MISMATCH})

    #: 坐标核对失败时最多存这么多张现场图（见 `_dump_coord_mismatch`）。
    MAX_COORD_DUMPS: int = 3

    #: 详情页铺不开时最多存这么多张现场图。同样要封顶：一趟最多开 8 封，
    #: 若某一屏整体没渲染，8 张几乎一样的图对定位没有增量。
    MAX_MAIL_DUMPS: int = 3

    #: 开工对账时，信箱里哪一类报告算作「这条链路今天打出去的一发」。
    #:
    #: 海盗战的主题是「海盗攻击报告」（`ReportKind.PIRATE`），打玩家/bot 的是
    #: 「攻击报告」（`ReportKind.ATTACK`）——`classify_report_subject` 特意先判
    #: 「海盗」再判「攻击报告」，因为后者是前者的子串。子类覆盖它。
    #:
    #: ⚠️ 分类靠 OCR，读丢「海盗」两个字就会把海盗战认成普通攻击报告。
    #: 两种错法都落在安全的一侧：海盗这边数少了，配额退回按库计数（也就是
    #: 今天的行为）；bot 那边数多了，只会让它提前收手。`count_dispatches_since`
    #: 取大的规则保证了这一点——观测值只能把计数往上抬，不能往下压。
    RECONCILE_KIND: ReportKind = ReportKind.PIRATE

    def __init__(self, driver: LiveDriver, ocr: Any, options: LoopOptions) -> None:
        self._driver = driver
        self._ocr = ocr
        self._options = options
        self._navigator = SystemNavigator(driver)
        self._outcome = Outcome()
        self._repository: SqlAlchemyRepository | None = None
        self._run_id: UUID | None = None
        self._session_keeper: Any = None
        self._coord_dumps = 0
        self._mail_dumps = 0
        #: 本趟开工时刻。本轮派出去的侦察/攻击，其报告一定比它新——
        #: 翻信箱时据此早停（见 `MailRow.is_older_than`）。
        self._started_at = datetime.now(UTC)

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

    def check_target(self, coordinate: Coordinate) -> TargetCheck:
        """行星面板上是不是「敌对海盗」，而且坐标对得上。

        **先认面板、再核坐标**，顺序不能反：坐标行（`PIRATE_COORD_ROI`）属于
        海盗面板那套布局，空位上那块像素是什么并没有证据。先核坐标的话，每个
        空位都会因为读不到坐标而判成 `MISMATCH`，于是整轮都在复位重试——
        而「这一位没有海盗」本来就是最常见的正常结果。

        坐标要核：导航栏偶尔会停在别的位号上（实机踩过），这时面板是真的、
        只是不是请求的那一位——照着它打就打错了目标。
        """
        title = self._read(pirate_ui.PIRATE_TITLE_ROI)
        if pirate_ui.PIRATE_TITLE_TEXT not in title:
            return TargetCheck.ABSENT
        wanted = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
        shown = self._read(pirate_ui.PIRATE_COORD_ROI, digits=True)
        if wanted not in shown:
            say(f"  坐标核对不过：面板显示 {shown!r}，请求的是 {wanted}")
            self._dump_coord_mismatch("pirate-coord-mismatch")
            return TargetCheck.MISMATCH
        return TargetCheck.CONFIRMED

    def _dump_coord_mismatch(self, name: str) -> None:
        """坐标核对不过就留一帧现场，但要封顶。

        只有一行文字复盘不了「画面到底成了什么样」——实机那 13 分钟就是这么白丢
        的。反过来不封顶的话，连续 44 个目标全失败会写出上百张几乎一样的图，
        前几张就够定位了。
        """
        if self._coord_dumps >= self.MAX_COORD_DUMPS:
            return
        self._coord_dumps += 1
        self._dump_frame(name)

    def _goto_checked(self, coordinate: Coordinate) -> TargetCheck:
        """导航过去并核对面板；判定落在 `RETRY_CHECKS` 里就复位画面再试一次。

        实机（2026-08-11 00:55–01:08）：第一个目标走到派遣面板时预设条读成空，
        之后**连续 44 个目标**每一次坐标核对都不过，读数一律多出个 `:9` 前缀——
        画面从某一刻起整体偏了。而每个目标只试一次、失败就跳下一个，于是这 13
        分钟一发都没派出去，日志里也只有一行文字、连张图都没留。

        动作顺序（两条链路共用这一份，别再各写一遍）：
        查会话 → 复位画面 →（重连过就切回恒星系视图）→ **清缓存** → 重新导航 → 再读一次。

        - 查会话排在最前：掉线时这一屏是 START 登录页，面板**永远**读不出来，
          复位和重新导航都是白费——实机（2026-08-11 02:11）就这么对着登录页把
          目标一个个试下去，每个 ~35 秒，日志里全是「面板读作 ''」。
        - 清缓存是这条重试的**全部意义**。导航器认为某个字段已经对了就不去重设
          （`SystemNavigator.goto` 里那三个 `if`），所以只要它的记忆和导航栏实际
          值分了岔，不清缓存的重试会一字不差地重演上一次失败——实机验证过：
          重试读回来的还是那个 `[9:137:12]`。

        **只重试一次。** 无限重试会把整轮卡死在一个目标上，比跳过还糟。
        """
        self._navigator.goto(coordinate)
        check = self.check_target(coordinate)
        if check not in self.RETRY_CHECKS:
            return check
        say("  复位画面后重试一次")
        reconnected = self._ensure_session(force=True)
        self._reset_to_known_screen()
        if reconnected and not self._navigator.ensure_system_view(self._nav_labels):
            raise RuntimeError("重连后切不到恒星系视图；安全停止")
        self._navigator.invalidate()
        self._navigator.goto(coordinate)
        return self.check_target(coordinate)

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

    def _report_screens(self) -> Any:
        """当前这一屏的 `ReportScreens`。

        **每次重新建**——同一个实例读两屏会把上一屏的像素当成这一屏
        （`ingest_pirate_report` 里记着同一条）。
        """
        from evo_helper.vision.optional.report_screens import ImageReportScreens
        from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport

        image = crop_to_viewport(self._driver.capture())
        return ImageReportScreens(
            image,
            layout_for_viewport(image.width, image.height),
            tesseract_cmd=str(_tesseract_path()),
        )

    def _mail_list_rows(self) -> list[MailRow]:
        """当前这一屏列表上的每一行：主题、时间、类型。**一封都不打开。**

        代价是一次截图加六次窄 ROI OCR，比开一封（≈8 秒）便宜整整一个量级——
        这就是「先筛后开」成立的全部理由。
        """
        return [
            mail_row_from_text(index, text)
            for index, text in enumerate(self._report_screens().mail_rows())
        ]

    def _enter_mailbox(self) -> None:
        """关浮层 → 切地表 → 开信箱 → 拖回顶部。两条链路进信箱的唯一姿势。

        ⚠️ **关浮层必须排在切地表之前。** `_on_planet_surface()` 的正面凭据是右上角
        那个未读数，而浮层会盖住它；`_goto_planet_surface` 自己不关浮层，只会反复点
        视图菜单（而那个坐标此刻压在浮层底下）。这一步偏偏紧跟在派遣与等待之后，
        正是舰队返航之类的通知最容易冒出来的时刻——实机（2026-08-11 02:10 / 03:35 /
        03:46）三次都倒在这里，而每次都已经先派出 4 发侦察，报告读不到就白飞。

        拖回顶部同样不能省：列表会记住上次滚到哪，不拖回去第 0 行可能是一封只露
        半截的邮件——读出来是空主题，而画面看着完全正常。
        """
        self._reset_to_known_screen()
        if not self._goto_planet_surface():
            # 判据失败时最贵的事是「不知道当时画面长什么样」。存一帧的成本是一次写盘。
            self._dump_frame("planet-surface-unreachable", MAIL_BADGE_ROI)
            raise RuntimeError("切不到自己星球地表，读不了信箱；安全停止")
        self._open_mail()
        for _ in range(MAIL_SCROLL_TO_TOP_DRAGS):
            slow_drag(self._driver, PANEL_DRAG_TO_Y, PANEL_DRAG_FROM_Y)

    def _scan_mail_rows(
        self,
        *,
        wanted: ReportKind,
        label: str,
        visit: Callable[[MailRow, Any], bool],
        not_before: datetime | None = None,
    ) -> None:
        """进一趟信箱，把**主题看着对得上**的报告逐封打开交给 `visit`。

        `visit(row, page)` 返回 True 表示「要的都收齐了」，这一趟就此收工。
        `not_before` 是「要找的报告最早可能是什么时候」：列表按时间倒序，翻到比它
        更早的那一行，往下就全是旧报告，可以立刻收工。

        两条链路共用这一份（侦察报告 / 攻击战报只差 `wanted` 与 `visit`）。
        原先各写一份，于是每修一次只修好一半：关浮层那条修在海盗那边、
        bot 那边过了半天才补上，就是这么来的。

        与原先那份「盲开最上面 6 行」的差别有四，每一条都是实机上真丢过报告的地方：

        1. **先在列表页读主题，再决定开不开。** 这是 bot 探路战报一整天收不回来的
           正因：两条链路的报告混在同一个收件箱里按时间倒序排，海盗链路整夜都在
           产出攻击报告，6 行的窗口很容易被别人的报告占满，而那 6 次开封的预算
           仍旧照花不误。读一屏主题 ≈ 一次截图加六次窄 ROI OCR，开一封 ≈ 8 秒
           （点开等 2.4s + 认屏 + 详情 OCR + 返回等 2.0s）——差一个量级，
           所以先筛后开永远划算。
        2. **窗口不再钉死在 6 行。** 主题筛掉的行不花开封预算，于是同样的时间可以
           多翻几屏（`MAIL_SCAN_PAGES`），而 `not_before` 让翻到旧报告就停。
        3. **点开之后要等详情页真的铺开。** 原先是点一下、死等 2.4 秒、读一次就走。
           面板是**滑进来**的——`_settle` 的注释就记着「等 2.4 秒判一次判不到，
           而失败时存下的那一帧读得清清楚楚」。没铺开的那一屏读出来是一堆读不通的
           字，和「这封是别人的报告」在下游长得一模一样，于是被静默丢掉。
           判据 `_on_mail_detail` 早就写好了，只是**从来没有人调用过**。
        4. **每一行都要留下一句话。** 原先只在认出目标时才说话，收不到时统一说
           「还没出现在信箱最上面几行」——那句话把「窗口不够大」说成了「报告还没到」，
           而两者的处置完全相反。实机上连续四趟收不到战报，日志里没有任何可以据以
           定位的东西，正是被这句措辞盖住的。
        """
        self._enter_mailbox()
        seen: set[tuple[str, str]] = set()
        opened = 0
        done = False
        for page in range(MAIL_SCAN_PAGES):
            if done or opened >= MAIL_MAX_OPENS:
                break
            # ⚠️ **每次点行之前都要先确认「还在邮件列表上」。** 实机踩过两次同一个错：
            # 上一次返回没退到列表（或把整个信箱关掉了），接着照列表的行坐标点下去，
            # 于是点在了地表 UI 上——一次点开了「取消任务」确认框，一次点开了「排名」。
            if not self._settle(self._on_mail_list):
                say("  已经不在邮件列表上了；这一趟到此为止")
                break
            fresh = [row for row in self._mail_list_rows() if row.identity not in seen]
            if not fresh:
                say(f"  第 {page + 1} 屏没有没见过的邮件；不再往下翻")
                break
            seen.update(row.identity for row in fresh)
            for row in fresh:
                if row.is_older_than(not_before):
                    say(
                        f"  第 {row.index} 行是 {row.raw_time_text} 的报告，比要找的那几发还早；"
                        "列表按时间倒序，往下都是旧的，收工"
                    )
                    done = True
                    break
                if opened >= MAIL_MAX_OPENS:
                    say(f"  这一趟已经开了 {opened} 封，到上限；剩下的留给下一趟")
                    break
                if not row.may_be(wanted):
                    say(f"  第 {row.index} 行不是{label}（主题读作 {row.subject!r}）；不打开")
                    continue
                opened += 1
                if self._open_mail_row(row, visit):
                    done = True
                    break
            if not done and page + 1 < MAIL_SCAN_PAGES:
                slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
        self._close_mail()

    def _open_mail_row(self, row: MailRow, visit: Callable[[MailRow, Any], bool]) -> bool:
        """点开一行、等它铺开、交给 `visit`，然后退回列表。返回「可以收工了」。

        铺不开就存一帧（封顶）并当作这一封读不出来。**不读没铺开的那一屏**：
        读出来的字和「这封是别人的报告」分不开，而分不开就等于静默丢掉一份战报。
        """
        self._driver.click(
            MAIL_ROW_X, MAIL_FIRST_ROW_Y + row.index * MAIL_ROW_PITCH, label="打开邮件"
        )
        self._driver.wait(MAIL_OPEN_WAIT_S)
        done = False
        if not self._settle(self._on_mail_detail):
            say(f"  第 {row.index} 行点开之后没读到「消息」标题；这一封放过")
            self._dump_mail_detail()
        else:
            done = visit(row, self._report_screens())
        self._driver.click(*MAIL_BACK, label="返回")
        self._driver.wait(MAIL_BACK_WAIT_S)
        return done

    def _dump_mail_detail(self) -> None:
        """详情页铺不开时留一帧现场，但要封顶（同 `_dump_coord_mismatch` 的理由）。"""
        if self._mail_dumps >= self.MAX_MAIL_DUMPS:
            return
        self._mail_dumps += 1
        self._dump_frame("mail-detail-unrendered", PANEL_TITLE_ROI)

    def collect_scout_reports(self, wanted: Sequence[Coordinate]) -> dict[Coordinate, Any]:
        """**一次进信箱**，把认得出的侦察报告全读出来，按目标坐标归档。

        为什么不是「一个目标进一次信箱」：进出信箱要切视图、开面板、翻标签，
        每份报告还要慢拖两次，一趟十几秒。四个目标各跑一趟就是一分钟纯导航，
        而它们的报告本来就并排躺在同一页上。

        按**报告自己写的目标**归档，不按行号猜：行序会随新邮件变，
        而报告开头那行写着「已对 [x:y:z] 完成侦察」，那是它自己的凭据。
        """
        from evo_helper.vision.scout_reports import ScoutReportUnreadable, read_pirate_scout

        found: dict[Coordinate, Any] = {}
        remaining = set(wanted)

        def visit(row: MailRow, header: Any) -> bool:
            # 舰种清单在详情页下半屏，要拖到底才看得到；VS 那一段则在拖之前读。
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            ships = self._report_screens()
            try:
                reading = read_pirate_scout(header, ships)
            except ScoutReportUnreadable as error:
                say(f"  第 {row.index} 行读不出侦察报告：{error}")
                return False
            if reading.target in remaining:
                found[reading.target] = reading
                remaining.discard(reading.target)
                say(f"  第 {row.index} 行 → {reading.target} {reading.verdict}")
            else:
                say(f"  第 {row.index} 行是 {reading.target} 的报告，不在本轮目标里")
            return not remaining

        if not remaining:
            return found
        # 本轮真的派过侦察时才按时间早停：那些报告一定比本轮开工还新。
        # 只给 `--attack` 不给 `--scout` 时用的是信箱里**已有**的那几封，
        # 它们比开工时刻早，早停会把它们全部挡在外面。
        self._scan_mail_rows(
            wanted=ReportKind.SCOUT,
            label="侦察报告",
            visit=visit,
            not_before=self._started_at if self._options.scout else None,
        )
        for coordinate in sorted(remaining, key=_coordinate_order):
            say(f"  {coordinate} 的侦察报告这一趟没翻到")
        return found

    # -- 开工对账 -----------------------------------------------------------

    def reconcile_today(self) -> None:
        """开工先对账：数一遍今天（UTC+0）信箱里已经有多少份本链路的攻击战报。

        ## 为什么要对账

        「今天已经打了几发」现在只按库里的 `attack_dispatches` 数
        （`repository.count_dispatches_since`）。库外发生过的事它一概不知道：
        用户自己手动打的、上一次进程崩在写库之前的、以及**库被换过/清过**之后
        还留在游戏里的那些。数少了就会超额，而超额的后果是游戏发邮件通知并把
        攻击强制返回——白飞一趟舰队。

        ## 哪一侧是权威

        两边都只是**下界**，而且是各自独立的证据：

        - `attack_dispatches`：权威地知道「助手自己派出去过什么」。它在游戏接受
          「出发！」的那一刻就写下了，**刚派出、战报还没到**的那几发只有它知道。
        - 信箱里的战报：权威地知道「确实打成了一发」。它是游戏自己的记录，进程崩掉、
          换库、用户手动操作都不影响，但它**滞后**——三分钟前派出去的那一发还没有报告。

        所以谁都不能单独当答案，取**两者的大者**（按 UTC 日分别取）：
        它是能被证据支持的最紧的下界，只会让助手提前收手、绝不会让它多打。

        ## 绝不凭空造派遣记录

        对账**不写 `attack_dispatches`**。多一条不存在的派遣，调度器就会以为一条
        航线被占着、等一份永远不会来的战报，要到 `MAX_REPORT_AGE`（6 小时）才被
        判缺失清掉。这里只写「今天观测到 N 份战报」这一个事实，
        让计数那一侧（`count_dispatches_since`）把它折进去——也就是用户说的
        「更新计数所依赖的那个事实，而不是伪造 N 条派遣」。

        ## 一天只做一次，而且做在链路开工处

        对账要看屏，而控制台自己不驱动游戏（它只跑网页与调度）。放在链路开工处，
        游戏窗口、会话、信箱导航全都是现成的；靠库里那条按 **UTC 日**去重的记录
        保证一天只做一次——按 UTC 日而不是按进程启动去重，是因为配额的日界本来
        就是 UTC 00:00（见 `domain.scheduler.quota_day_start_utc`），而控制台一天
        可能重启好几次。
        """
        from evo_helper.domain.scheduler import quota_day_start_utc

        repository, _run_id = self._ensure_run()
        now = datetime.now(UTC)
        day_start = quota_day_start_utc(now)
        if repository.reconciled_on(self.TARGET_KIND, day_utc=day_start):
            return
        say(f"开工对账：数一遍 UTC {day_start:%Y-%m-%d} 信箱里的{self.TARGET_KIND}战报")
        try:
            observed, complete = self._count_reports_since(day_start)
        except RoundExhausted:
            raise
        except RuntimeError as error:
            # 对账翻不了信箱**不该把这一轮判死**。它只是让配额判据退回按库计数，
            # 也就是今天没修正的那个状态——和不做对账一样，不比它更糟。
            # 不写记录，下一轮再试。
            say(f"  对账翻不了信箱（{error}）；这一轮先按库内计数走")
            return
        repository.record_daily_reconciliation(
            self.TARGET_KIND,
            day_utc=day_start,
            observed_reports=observed,
            complete=complete,
            reconciled_at_utc=now,
        )
        note = "翻到底了" if complete else "没翻到底，这是「至少」"
        say(f"  今天已有 {observed} 份（{note}）")

    def _count_reports_since(self, day_start: datetime) -> tuple[int, bool]:
        """数今天的战报，**一封都不打开**。返回 (份数, 有没有翻到今天之外)。

        只读列表页的主题与时间：一屏一次截图加六次窄 ROI OCR，而开一封要八秒。
        正常的停止条件是「翻到了 `day_start` 之前的那一行」——列表按时间倒序，
        再往下都是昨天的。`RECONCILE_MAX_PAGES` 只是时间读不出来时的兜底。

        没翻到底时返回的份数是「今天至少这么多」。它**照样算数**（配额那一侧
        仍然拿它去和库内计数取大），但这件事要一起记下来：日志和库里都要说得清
        那个数是不是全天，否则日后没人分得出「今天只打了 3 发」和「只数到 3 发」。
        """
        self._enter_mailbox()
        seen: set[tuple[str, str]] = set()
        observed = 0
        complete = False
        for page in range(RECONCILE_MAX_PAGES):
            if not self._settle(self._on_mail_list):
                say("  已经不在邮件列表上了；对账到此为止")
                break
            fresh = [row for row in self._mail_list_rows() if row.identity not in seen]
            if not fresh:
                # 拖不动了：到底了，或者面板夹住了。两种都不能算「翻完了今天」——
                # 只有真的看见一行昨天的报告才算。
                say(f"  第 {page + 1} 屏没有没见过的邮件；对账到此为止")
                break
            seen.update(row.identity for row in fresh)
            older = [row for row in fresh if row.is_older_than(day_start)]
            if older:
                complete = True
            observed += sum(
                1
                for row in fresh
                if row.kind is self.RECONCILE_KIND and not row.is_older_than(day_start)
            )
            if complete:
                break
            if page + 1 < RECONCILE_MAX_PAGES:
                slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
        self._close_mail()
        return observed, complete

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
            self.reconcile_today()
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

        ⚠️ **「认不出」分两种，这里必须分开对待**（见 `TargetCheck`）：没有海盗
        就照常走下一位；坐标核对不过是导航漂了，`_goto_checked` 会自愈一次，
        自愈完还不过就**记一笔 refused**——不记的话它长得和「这一位没有海盗」
        一模一样，而后者是最常见的正常结果，整轮一发没派也看不出异常。
        """
        pirates: list[Coordinate] = []
        scouted = 0
        for position in PIRATE_POSITIONS:
            coordinate = Coordinate(galaxy, system, position)
            check = self._goto_checked(coordinate)
            if check is TargetCheck.MISMATCH:
                say(f"  {coordinate} 复位重试后坐标仍核对不过；跳过这一位")
                self._outcome.refused.append((coordinate, "坐标核对不过"))
                continue
            if check is not TargetCheck.CONFIRMED:
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
        if self._goto_checked(coordinate) is not TargetCheck.CONFIRMED:
            self._outcome.refused.append((coordinate, "攻击前面板认不出"))
            return
        self.attack(coordinate)


def _coordinate_order(coordinate: Coordinate) -> tuple[int, int, int]:
    return (coordinate.galaxy, coordinate.system, coordinate.position)


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
