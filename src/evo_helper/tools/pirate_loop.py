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

## 开工第一件事：读战报，再更新「今天打了几发」

用户口径（2026-08-11）：「任务启动先去读战报……读完后，需要更新海盗攻击/bot 攻击
的数量，因为我可能暂停任务重启启动。」`reconcile_today()` 是那一趟——一次进信箱
办两件事：把还没入库的攻击战报读进 `battle_reports`（战果按
`domain.battle_outcome` 那条算式算，不看画面上那行大字），同时数今天（UTC+0）
信箱里已经有多少份本链路的战报。两条链路共用这一趟，只有「一封战报怎么读」不同。

海盗战报以前**一份都没读过**：`vision.pirate_reports.read_pirate_report` 只挂在
离线入口 `tools.ingest_pirate_report`（要人手工喂两张截图）上，活链路从来不调它，
于是攻击日志的战果列永远是空的。bot 那边的同一个死结在此之前刚修过。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.pirate_round import (
    PHASE_LABELS,
    PirateAction,
    PiratePhase,
    action_for,
)
from evo_helper.domain.planet_switch import switch_needed
from evo_helper.domain.reconcile_cooldown import (
    RECONCILE_COOLDOWN,
    ReconcileDecision,
    decide_reconcile,
)
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
)
from evo_helper.domain.report_wait import (
    DEFAULT_REPORT_SCAN_FLOOR,
    REPORT_SCAN_HOURS_MAX,
    parse_game_duration,
)
from evo_helper.domain.scan_bounds import PIRATE_POSITIONS
from evo_helper.domain.scheduler import EXIT_ENVIRONMENT_BUSY, quota_day_start_utc
from evo_helper.game import pirate_ui
from evo_helper.game.planet_list import PlanetSwitcher, SwitchResult
from evo_helper.game.preset_picker import PresetNotFound, PresetPicker, name_words
from evo_helper.game.system_navigator import (
    NAV_LABEL_ROI,
    PLANET_VIEW_BUTTON,
    VIEW_MENU_BUTTON,
    VIEW_SWITCH_WAIT_S,
    SystemNavigator,
    crop_reader,
)
from evo_helper.infrastructure.system_log import record_knob_override, record_system_log
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.report_screenshots import ReportScreenshotRepository
from evo_helper.storage.repository import PirateProgress, SqlAlchemyRepository
from evo_helper.tools.runner_logging import install_runner_system_log
from evo_helper.tools.scan_coordinates import (
    LiveDriver,
    make_ocr,
    origin,
    run_with_foreground_guard,
    say,
    thumbnail_base64,
    wait_for_login_if_unrecognised,
)

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

#: 读简报上那一行飞行时间的配方，按顺序试到**第一个能解析成时长**的为止。
#:
#: 前四套是 `pirate_ui.FLIGHT_RECIPES`（那次「ROI 从落地起就没读出过东西」的
#: 事故留下的），后四套是 2026-08-13 补的。补的理由：
#: `domain.report_wait.parse_game_duration` 收紧成「部分匹配一律失败」之后
#: （`3天19时36分7秒` 曾被静默读成 `0:36:07`，生产库 209 发里 66 发中招），
#: 读不出来的那些从**错值**变成了 `None`，而 `None` 那条路按
#: `UNKNOWN_LINE_HOLD`（90 分钟）占航线，比真实往返（10–62 分钟）保守得多。
#: 把 NULL 压回去的正当手段是**多试几套把真值读出来**，不是放松解析判据——
#: 读出一个小而合理的错值会同时污染两个钟，比 NULL 贵得多。
#:
#: 补的这四套是在两张实拍上量出来的（`var/logs/dump-briefing-unrecognised-182102
#: .png` 画面上是 `8分26秒`、`…-182153.png` 是 `8分28秒`，原先四套**一套都读不出**）：
#:
#:     (6, 160)  三张实拍全中（含原来那张 14 秒的回归基准）
#:     (5, 120)  8分26秒
#:     (3, 140)  8分28秒
#:     (6, 100)  8分26秒 与 8分28秒
#:
#: ⚠️ **一套 `nearest` 都不许加，哪怕它在某张图上读得出。** 那不是「多一次机会」
#: 而是**多一次读错的机会**：同一张 182102 上 `3×/nearest/140` 把 `8分 PEPE`
#: 解析成 `0:08:00`、`5×/nearest/120` 把 `as} 6秒` 解析成 `0:00:06`——两个都是
#: 能解析、量级也合理的**错值**，而这个函数取的是第一个解析成功的。
#: 上面这四套（连同原来四套）在三张实拍的整张 lanczos 网格里**没有一格读错**，
#: 读得出的一律是真值。
#:
#: 同理 `(2, 90)` 也被排除在外：它在 14 秒那张上解析成 `0:00:01`。
FLIGHT_RECIPES = (*pirate_ui.FLIGHT_RECIPES, (6, 160), (5, 120), (3, 140), (6, 100))

#: 等简报页铺开时，飞行时间这一行比别处多等几轮。
#:
#: 页面是**滑进来**的（`_settle` 的注释记着「等 2.4 秒判一次判不到，而失败时
#: 存下的那一帧读得清清楚楚」）。任务类型那道闸门读不出只是拦下一发，
#: 而这一行读不出是**永久**的：点完「出发！」这一屏就没了，没有第二次机会。
#: 所以这里加码到 6 轮（约 5 秒），代价只落在真的读不出来的那几发上。
FLIGHT_SETTLE_TRIES = 6

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

#: 信箱按钮旁边的未读数。**地表视图独有**：恒星系视图那个位置是绿色的资源牌，
#: 各种浮层则把它整个盖住。用它当「我在地表」的正面凭据。
#:
#: ## 框的两条边各自钉在什么上（2026-08-13 用 195 张实拍量出来的）
#:
#: 这个部件横着排三段：**信封白块**（x 1115–1147）、**数字**、**面板右描边**
#: （x 1208–1210）。数字**居中于 x≈1165**、每位约 9px 宽——2 位的 `65` 占
#: 1157–1173，3 位的 `332` 占 1153–1177，两者中心一模一样。也就是说未读数每多
#: 一位，它就同时**往左和往右各长 4.5px**。
#:
#: 所以这个框不再按「当前是几位数」来定，而是按**部件本身**定：
#:
#: - 左界 1148 = 信封白块右缘（实拍上最靠右的一次是 1147）再往右 1px。
#:   **不能再往左**：白块二值化之后是一大团纯白，psm 7 会被它压垮。实测（22 张
#:   正面样本，最终这套配方）左界推到 1143 漏 1 张、1140 漏 2 张、1138 漏 3 张。
#: - 右界 1206 = 面板右描边（1208）再往左 2px。往右到 1214 在现有样本上不掉分，
#:   留 2px 只是不去框那道竖线——它是已知的噪声源（不二值化时右界一到 1210，
#:   22 张里就有 7 张读不出来）。**要往右挪的话没有阻力，往左挪有。**
#: - 上下界 55/92 同样不该放：上下各推 6px 就漏 3 张。
#:
#: 1148–1206 就是数字在**不改变版面**的前提下能占的全部空间。
#:
#: ⚠️ **位数这件事到此为止，别再为它加设计。** 用户口径（2026-08-13）：
#: 「邮箱不需要考虑 4 位数情况」——未读数不会涨到四位。按上面的算式，四位数
#: 占 1148–1182 本来也落在框里，所以这条约束只是让下面这个问题彻底出局：
#: 五位数要占到 1143、已经压到信封白块上，那时游戏自己必然改版面（缩字、
#: 加宽面板或显示 `999+`），**而任何今天挑的框在那时都得重标**。
#: 与其为一个不会发生的情形留后手，不如把「到时候重标」写下来。
#:
#: 顺带：**读到半截也算数**。`var/logs/rank-closed.png` 上真值是 `118`，这套配方
#: 读出来是 `'8'`——仍旧是非空，而调用方只看非空（见 `_on_planet_surface`）。
#: 所以真到了五位数被左切一刀，读出来是「糊掉的首位 + 后几位」也照样成立。
#: 致命的从来是整块读成空。
MAIL_BADGE_ROI = (1148, 55, 1206, 92)

#: 读未读数的二值化阈值。**不二值化就守不住负面**。
#:
#: 这一块的字是近白色（>235）压在暗面板纹理上，而**盖住画面的模态会把整屏压暗**。
#: 不二值化时那两类都读得出来：173 张负面样本里有 2 张
#: （`dump-bot-coord-mismatch-235334.png` 读作 `166`、
#: `dump-mail-list-unrecognised-233223.png` 读作 `478`）——两张都是模态压着的暗屏，
#: 判成「在地表」之后助手就会照地表的坐标往那个模态上点。
#:
#: 140 / 150 / 160 三档在 22 张正面 + 173 张负面上都是 **0 漏 0 误**，取中间那档。
MAIL_BADGE_THRESHOLD = 150

#: 读未读数的放大倍数，逐个试到第一个读出数字为止。**全部是 LANCZOS**。
#:
#: ⚠️ **不要往里加 `nearest`。** 噪声几乎全出自它：同一批负面样本上，
#: `nearest` 会从暗纹理里读出 `'2'`、`'7'`、`'8'` 之类的单字符（3 套 lanczos +
#: 3 套 nearest 时误判 4 张），而 lanczos 在那 173 张上一个字符都读不出来。
#: 正面那侧 lanczos 也不吃亏——`332` 那一屏 2×/3× 都读得出。
#:
#: 也不要加 6×：`(3,2,4)` 是 0 误，加上 6× 就多出 1 张误判。
#:
#: ⚠️ **诚实说一句：现有 22 张正面实拍上，3× 一套就全读得出。**2× 与 4× 是保险，
#: 没有任何样本能证明它们此刻必要（2× 单用反而漏 4 张，所以它排在后面）。
#: 留着它们的理由是这一格的历史：未读数会变、版面会微移，而上一次「一套配方
#: 恰好够用」的结论正是这次事故的起点。兜底那条**路径**由
#: `tests/unit/tools/test_pirate_loop_mailbox_entry.py` 钉着（第一套读不出就换
#: 下一套），至于哪一天真的轮得到第二套，只有实机知道。
MAIL_BADGE_UPSCALES = (3, 2, 4)

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

#: 补录侦察报告（`backfill_scout_reports`）时的两个上限。
#:
#: 与活链路那两个（`MAIL_SCAN_PAGES` / `MAIL_MAX_OPENS`）分开写死，不是复用后调参：
#: 活链路每一轮都要付这段时间，上限是按「一轮在等 6–8 份报告」定的；补录是人手动
#: 跑的一次性动作，要把**一整天**的报告翻出来——2026-08-11 那天光重复侦察就 25 发。
#:
#: 12 屏 ≈ 72 行，覆盖得住一天的信箱；60 封 × ≈15 秒（开封 8 秒 + 两次慢拖）
#: 最坏约 15 分钟，是「人盯着跑一次」能接受的量级，同时保证它一定会停。
BACKFILL_SCAN_PAGES = 12
BACKFILL_MAX_OPENS = 60

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

#: 读之前把列表拖回顶部，**最多**拖这么多次。
#:
#: ⚠️ **这原先是个写死的 3，而 3 是错的。** 一次慢拖走 `PANEL_DRAG_FROM_Y -
#: PANEL_DRAG_TO_Y` = 400px ≈ 4.6 行，3 次约 14 行；而一趟对账要往下翻
#: `RECONCILE_MAX_PAGES` = 8 屏 ≈ 32 行。**每一趟净往下沉约 18 行**，而列表会
#: 记住上次滚到哪。信箱本身有 600 封（用户实测），永远沉不到底。
#:
#: 2026-08-13 那一夜的账：UTC 19:51–23:01 派了 17 发 BBB，`battle_reports`
#: 一行都没有。现场图 `var/logs/dump-mail-detail-unrendered-053043.png`
#: （本地 05:30 = **21:30 UTC**）上，列表最上面那几行是 16:42–17:02 的侦察报告
#: ——**比墙钟旧四个半钟头**；同一张图上二级角标写着「战斗 10」未读，正好等于
#: 那时已经落地的 10 发攻击。也就是说战报就躺在列表顶上，而扫描窗口停在
#: 四个半小时之前，七趟信箱一次都没够到过它。
#:
#: 所以停止条件不能是「拖了几次」，只能是**拖不动了**（判据与 `_scan_mail_rows`
#: 里那条「还是那几封」、`domain.planet_switch.list_exhausted` 同一条）。
#: 这个数只是兜底上限：40 次 ≈ 186 行，够把一夜攒下的位移拖回去，
#: 而且保证一定会停。
MAIL_SCROLL_TO_TOP_MAX_DRAGS = 40

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

#: 报告详情页左上角的返回键。
MAIL_BACK = (750, 71)

#: 邮箱**列表**左上角的 X。对账结束后必须点它离开列表；否则列表浮层会盖住
#: 后续的行星列表，出发星球坐标列就只能 OCR 成空，攻击不会进入预设选择。
#:
#: 坐标目前与报告详情页的返回键相同，但语义不能共用：详情页点它是「返回列表」，
#: 列表点它才是「关闭信箱」。分开命名能让收尾测试守住用户指定的这个动作。
MAIL_LIST_CLOSE = (750, 71)

#: 一趟信箱最多重进几次。
#:
#: 重进的成因见 `_scan_mail_rows`：点开一封不是预期格式的邮件之后，那一下「返回」
#: 可能把整个信箱关掉（`MAIL_BACK` 与「关闭面板」是同一个坐标）。
#:
#: 取 2 是因为**重进本身不便宜**：它要重新进信箱、从顶部把已经翻过的几屏重扫一遍
#: （只读主题，不重复开封）。真需要重进三次以上，说明画面已经不是「偶尔掉出列表」
#: 那种情形了，接着试只是在一个认不出的画面上多点几下。
MAIL_MAX_REENTRIES = 4

#: 从详情页退回列表最多点几次「返回」。
#:
#: 一次不够：`MAIL_BACK` 身兼两职（也是「关闭面板」），落在一个不是详情页的画面上
#: 时会把整个信箱关掉。点第二次的最坏情况是**点空**——那个坐标在恒星系视图上
#: 什么都不是（`_ensure_session` 的恢复阶梯里记着同一条）。
#:
#: 便宜太多：一次确认 + 一次补点约 2 秒，而丢了列表要重进信箱、从顶部重扫，
#: 实机上那一趟 30 屏的预算有 12 屏花在重扫上。
MAIL_BACK_ATTEMPTS = 2

#: 详情页里把内容拖到底用的起止点（917 空间）。必须慢拖，见 `slow_drag`。
PANEL_DRAG_FROM_Y = 700
PANEL_DRAG_TO_Y = 300

#: 读「单位」/「损失单位」那两行之前，详情页要往下拖几次（见 `PirateLoop._bottom_screens`）。
#: 到底会夹住，多拖一次无害；少拖一次就是静默留空。
DETAIL_SCROLL_TO_BOTTOM_DRAGS = 2


#: 借 `scan_coordinates` 那一份，不再各写一遍。它是编码安全的——
#: 实机上 `print` 一个 OCR 读出来的 `™` 就把整个 runner 弄崩过，见那边的注释。


class RoundExhausted(RuntimeError):
    """这一轮没料了：舰队全在外面，或者航线占满。

    **这不是失败。** 抛到 `run()` 就正常收尾、退出码 0——调度器据此不计入连续
    失败计数。反过来当成失败的话：航线占满是必然会发生的事，连撞三次就把整条
    链路自动停用了，而它其实只是需要等舰队飞回来。
    """


class SessionUnavailable(RuntimeError):
    """恢复阶梯走到头了，画面还是回不到游戏内。

    `recoverable` 说的是「这一轮之后还有没有救」，判据是 `SessionKeeper` 的
    **关窗重开配额**（`ReconnectOutcome.restarts_left`），整段理由在
    `domain.scheduler.exit_code_for_environment_fault` 与
    `tools.scan_coordinates.exit_code_for_unusable_session`。

    - 还有配额 → `run()` 把它收进 `Outcome.busy`，退出码 `EXIT_ENVIRONMENT_BUSY`，
      **不计入连续失败**：这一轮已经关窗重开过、失败了，但阶梯还没走到头。
    - 配额耗尽 → `busy_is_permanent`，退出码 1：重开这条路已经证明救不了，
      得让连续失败计数看见它，该停用时停用。

    ⚠️ **不能无条件豁免。** 走到这里的每一轮都会再吃一次重开配额，而配额那道闸
    只挡得住「无限重开 Chrome」，挡不住「每隔一个冷却起一轮、每轮失败、什么都不
    推进」——豁免计数不再增长的话，就再没有任何东西会最终把它停下来。
    """

    def __init__(self, message: str, *, recoverable: bool) -> None:
        super().__init__(message)
        self.recoverable = recoverable


class MailboxUnreachable(RuntimeError):
    """开工翻不了信箱，**而且单子上还有到点没战报的派遣**。

    ⚠️ **这一档必须把整轮判死（退出码 1），不能像 `RoundExhausted` 那样正常收尾。**

    翻不了信箱本身原先是被吞掉的，理由写在 `reconcile_today` 的 `except` 里：
    「和不做对账一样，不比它更糟」。**那句话只在单子为空时成立。**单子非空时
    那几发的 6 小时钟正在走（战报过期就永久判缺失），而「下一轮再试」连撞两次
    同一堵墙就等于把它们全丢掉——2026-08-12 那夜就是这样：23:51 打印出 10 发、
    00:30 打印出 15 发，两次都在下一行放弃，那 21 发全部过期。

    也不能报 `EXIT_ENVIRONMENT_BUSY`：那一档的准入条件是「会自己好」
    （见 `application.mission_supervisor.MissionOutcome.failed`），而这里已经
    关窗重开过一次仍然翻不了，正是**不会自己好**的那一侧，得让连续失败计数
    看见它、三次之后停用并报警。
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


class ReportIngest(Enum):
    """开工那一趟信箱里，一封战报的三种下场。

    ⚠️ **`KNOWN` 与 `UNREADABLE` 必须分开**，虽然两者都「没往库里加行」：

    - `KNOWN`（库里已有）是**早停的凭据**。信箱从新往旧排、入库也从新往旧写，
      所以碰到第一份已有的，往下每一份都必然已经在库里了，不必再开封。
    - `UNREADABLE`（这一封读不出来）**不能早停**。它下面还可能躺着没入库的战报，
      当成早停就是把「这一封读坏了」变成「今天剩下的都不读了」——一次 OCR 抖动
      能让一整趟收取哑掉。
    """

    STORED = "已入库"
    KNOWN = "库里已有"
    UNREADABLE = "读不出来"


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

    def may_be(self, wanted: ReportKind | Sequence[ReportKind]) -> bool:
        """这一行**值不值得打开**。

        只有主题明确读成了别的类型才跳过；读不出、认不出（`UNKNOWN`）一律照开。

        判据刻意往「开」的一侧倒：漏开一封 = 这一轮少一份报告（侦察白飞、
        探路白派），多开一封 = 多花八秒。真正的归属判定在打开之后（VS 块里的
        坐标 / 报告开头那行「已对 [x:y:z] 完成侦察」），主题只用来排掉**明摆着
        不是**的那些——实机上那恰恰是最多的一类：一整屏的攻击报告。
        """
        kinds = (wanted,) if isinstance(wanted, ReportKind) else tuple(wanted)
        return self.kind in kinds or self.kind is ReportKind.UNKNOWN

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
class DailyTally:
    """数今天（UTC+0）信箱里有多少份本链路的战报。`_scan_mail_rows` 逐行喂给它。

    **不开封**：判据只有列表行上的主题与时间。一屏六行是一次截图加六次窄 ROI OCR，
    而开一封约八秒——正因为便宜，它才敢一直数到「翻见昨天的那一行」为止，
    不必受开封预算牵制。

    `complete` 只在**真的看见一行昨天的报告**时才为真。拖不动了、到上限了都不算：
    那时 `observed` 只是「今天至少这么多」。这个数照样参与配额取大（下界也是证据），
    但日志与库里要说得清它是不是全天——否则日后没人分得出「今天只打了 3 发」
    和「只数到 3 发」，而这两件事对「还能不能接着打」的含义完全相反。
    """

    kind: ReportKind
    day_start: datetime
    observed: int = 0
    complete: bool = False

    def __call__(self, row: MailRow) -> None:
        if row.is_older_than(self.day_start):
            self.complete = True
            return
        if row.kind is self.kind:
            self.observed += 1


@dataclass
class MailScan:
    """一趟信箱的账单。**只用来说话，不参与任何判据。**"""

    #: 真的翻过几屏（不是上限，是实际翻了几屏）。
    pages: int = 0
    #: 真的打开过几封。
    opened: int = 0
    #: 这一趟**没能好好走完**的理由；正常收工时是 None。
    #:
    #: ⚠️ 摘要必须把它说出来。实机 2026-08-13 20:35：一趟给了 30 屏预算的补录在
    #: 第 3 屏丢了邮件列表、当场中止，打出来的却是
    #:
    #:     完成（bot · 补录（翻到 --since 为止））：翻了 3 屏，开了 3 封…
    #:
    #: ——一行**长得完全像成功**的话，而那一趟要救的 21 份战报一份都没碰到。
    #: 「翻了 3 屏」本身没撒谎，但只有知道预算是 30 屏的人才看得出不对劲，
    #: 而看摘要的人恰恰是不看命令行的那个人。
    cut_short: str | None = None


@dataclass
class BackfillTally:
    """一次战报补录的摘要。退出前打成一行人话，见 `tools.backfill_reports`。"""

    scan: MailScan = field(default_factory=MailScan)
    #: 打开并**读通**几份（读不通的不算，那些没进库）。
    read: int = 0
    #: 其中新写进 `battle_reports` 的有几份。
    stored: int = 0
    #: 撞见「库里已有」时顺手补认上派遣的有几份（见 `rematch_note`）。
    rematched: int = 0
    #: 这一趟开工/收工时，单子上「到点还没战报」的派遣各有几发。
    due_before: int = 0
    due_after: int = 0

    @property
    def claimed(self) -> int:
        """这一趟把几发派遣从单子上销掉了。

        ⚠️ **这是个下界，不是全部认领数。** 单子（`due_attack_dispatches`）只装
        派出不超过 `MAX_REPORT_AGE`（6 小时）的那些，所以人手动补昨晚的战报时
        它多半从 0 开始、也以 0 结束——而那一趟其实认领得好好的：认领窗口是
        `dispatched_at_utc >= reported_at - MAX_REPORT_AGE`，**相对战报自己的
        时间戳**算的，不是相对现在（见 `storage.repository.
        _unmatched_dispatch_candidates`）。所以补录过了六小时**照样有意义**。
        """
        return max(self.due_before - self.due_after, 0)


@dataclass
class LoopOptions:
    systems: tuple[tuple[int, int], ...]
    scout: bool
    attack: bool
    preset: str = pirate_ui.ATTACK_PRESET_NAME
    #: 这一轮记账用的出发星球。None 表示回落到全局 `origin()`
    #: （`EVO_HELPER_ORIGIN`）——手工跑命令行时的默认。
    #:
    #: ⚠️ 调度器**一律显式传**。任务现在各带各的出发星球，而这个坐标会原样写进
    #: `attack_intents.origin_*`，战报认领正是靠「出发坐标 + 目标坐标 + 时间就近」
    #: 配对的。让 runner 自己去猜，等于两个任务的账可能记到同一颗星球上。
    origin: Coordinate | None = None
    #: **强制**在这一轮开始前翻一次信箱，忽略冷却。手工排障用（``--reconcile``）。
    #:
    #: ⚠️ 默认档不是「不翻」，是「按冷却翻」——判据在
    #: `domain.reconcile_cooldown`，那个模块头写着这两者被混为一谈时发生了什么
    #: （战报断流两天）。调度器**不拼这个参数**，它只走冷却。
    force_reconcile: bool = False


@dataclass
class Outcome:
    pirates: list[Coordinate] = field(default_factory=list)
    scouted: list[Coordinate] = field(default_factory=list)
    attacked: list[Coordinate] = field(default_factory=list)
    refused: list[tuple[Coordinate, str]] = field(default_factory=list)
    #: 这一轮没派成的理由（目前只有「切不到出发星球」）。有值 = **一发都没派**。
    #:
    #: 用退出码而不是异常，是因为「这会儿轮不到我」已经有一档了
    #: （见 `application.mission_supervisor`）。
    busy: str | None = None
    #: 上面那个理由是**不会自己好**的那一种吗。决定退出码，见 `exit_code_for`。
    #:
    #: ⚠️ 这一档不能省，因为 `EXIT_ENVIRONMENT_BUSY` 是**不计入连续失败**的：
    #: 它原本只服务于「游戏窗口抢不到前台」，那是个必然自己好的条件（用户放开
    #: 鼠标就行）。切换星球失败**跨了两种性质**，不区分就会出事：
    #:
    #: - `UNCONFIRMED`（点过了，回读没认出来）：画面状态问题，下一轮多半就好——
    #:   该豁免。
    #: - `NOT_FOUND`（列表里翻遍了都没这颗星球）：多半是把 `origin` 配错了，
    #:   而**接口那侧无从校验**（自己有哪几颗星球只写在游戏画面上，库里没有）。
    #:   它不会自己好。豁免它的后果是一个**静默死循环**：每轮 30 秒就退，
    #:   不计故障、不报警，停顿看门狗也抓不到（那东西抓的是「跑着却没进展」，
    #:   而这里根本没跑起来）。于是任务显示「在跑」，实际一发不派，能挂一整夜。
    busy_is_permanent: bool = False
    #: 这一轮**必须以失败收场**的理由。有值 = 退出码 1（见 `exit_code_for`）。
    #:
    #: 与 `busy` 分开是因为两者说的不是一件事：`busy` 说「一发都没派，但这不算
    #: 故障」，而这个字段说「这一轮出了非得有人管的事」。目前唯一的来源是
    #: `MailboxUnreachable`——单子非空却翻不了信箱，升级重启之后还是翻不了。
    failed: str | None = None


#: 「行星列表读空 → 关浮层重读」这一支隔多久才肯再往库里塞一张图。
#: 理由与 `scan_coordinates.UNRECOGNISED_EVIDENCE_INTERVAL_S` 一模一样：
#: **限流不是省空间，是防刷爆**。文字那一条每次都写，图才限流。
OVERLAY_EVIDENCE_INTERVAL_S = 120.0

#: 上一次往 `system_log` 塞图的时刻（`time.monotonic`）。进程级，重启即清零。
_last_overlay_evidence_at: float | None = None


def record_planet_list_overlay_retry(
    message: str,
    payload: Mapping[str, Any],
    *,
    capture: Callable[[], Any] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """把「行星列表读空 → 疑似浮层 → 关掉 → 重读结果」写进 `system_log`。

    ⚠️ **跨机排障靠的就是这一条。** 2026-08-17 那次实机故障里，日志只留下
    「逐屏读到的是 `[[]]`」——够说明列表读空，却说不出**画面上盖着的是什么**。
    `_dump_frame("planet-list-unreadable")` 确实存了整帧，可它落在 runner 那台
    机器的 `var/logs` 下，本机根本取不到；最后是用户手工截了一张图，才认出那是
    「太空舱」面板。所以这里照 `scan_coordinates.record_unrecognised_screen` 的
    路子，把缩略图一起塞进 `payload_json`——`artifacts` 表存的是**路径**，
    而路径只在出事那台机器上有意义。

    文字每次都记（这一支本来就少见，而它每出现一次就等于一轮没派）；
    **图限流**，免得画面卡在浮层上时每轮都写一张进库。
    """
    global _last_overlay_evidence_at
    body: dict[str, Any] = dict(payload)
    moment = now()
    fresh = (
        _last_overlay_evidence_at is None
        or moment - _last_overlay_evidence_at >= OVERLAY_EVIDENCE_INTERVAL_S
    )
    if fresh and capture is not None:
        _last_overlay_evidence_at = moment
        body["thumbnail_png_base64"] = thumbnail_base64(capture())
    record_system_log("WARNING", "tools.pirate_loop", message, payload=body)


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
    #:
    #: 分类（2026-08-17 审计）：**低优先级旋钮**。它只影响排障时手上有几张图，
    #: 不影响任何判据；而「几张几乎一样的图对定位没有增量」这一条跟用户的处境
    #: 无关。没做成可配置——同 `MAX_COORD_DUMPS`。
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

    #: 上面那一档在日志里怎么念。只影响措辞，不影响判据。
    REPORT_LABEL: str = "海盗攻击报告"

    def __init__(self, driver: LiveDriver, ocr: Any, options: LoopOptions) -> None:
        self._driver = driver
        self._ocr = ocr
        self._options = options
        self._navigator = SystemNavigator(driver)
        self._outcome = Outcome()
        self._repository: SqlAlchemyRepository | None = None
        self._session_factory: Any = None
        self._run_id: UUID | None = None
        self._session_keeper: Any = None
        self._coord_dumps = 0
        self._mail_dumps = 0
        #: 本趟开工时刻。本轮派出去的侦察/攻击，其报告一定比它新——
        #: 翻信箱时据此早停（见 `MailRow.is_older_than`）。
        self._started_at = datetime.now(UTC)
        #: 本轮**回读确认过**的当前星球。None = 还没切过（进程刚起来一定是 None：
        #: 上一轮把游戏停在哪颗星球上不可知）。这就是「一轮只切一次」的记忆，
        #: 判据在 `domain.planet_switch.switch_needed`。
        self._current_planet: Coordinate | None = None
        #: 今天（游戏内 UTC 日）每个海盗目标走到哪一步了。`None` = 还没查过。
        #: 缓存的是**一整趟里都不该变**的那部分（今天已经派过什么、报告回了没），
        #: 每写进新的侦察报告就 `refresh=True` 重取一次，见 `_daily_progress`。
        self._daily: dict[Coordinate, PirateProgress] | None = None
        #: 本轮开工那一下到底翻没翻信箱，以及为什么。`None` = `run()` 还没走到
        #: 那一步（手工调子方法时会这样）。日志措辞靠它区分「翻过没找到」和
        #: 「本轮没翻」，见 `_reconcile_if_due` 与 `BotLoop._say_still_waiting`。
        self._reconcile_decision: ReconcileDecision | None = None

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

        capture = getattr(self._driver, "capture", None)
        if not callable(capture):
            # 现场保全是诊断附加项，绝不能覆盖原本的「安全拒绝派遣」结果。
            # 轻量驱动（尤其单元测试桩）只实现点击和等待，不具备截图能力。
            say(f"  无截图能力，跳过现场保存（{name}）")
            return
        directory = Path("var/logs")
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        image = capture()
        path = directory / f"dump-{name}-{stamp}.png"
        image.save(path)
        note = f"  已存现场 {path}（{image.width}x{image.height}）"
        if roi is not None:
            note += f"；ROI{roi} 读到 {self._read(roi)!r}"
        say(note)

    def _preset_names(self) -> list[tuple[int, str]]:
        import pytesseract

        return name_words(self._driver.capture(), pytesseract)

    def _planet_rows(self) -> list[tuple[int, str]]:
        """行星列表浮层坐标列上，这一屏每个词框的 `(中心 y, 文字)`。

        逐套配方试到**读出至少一个三段坐标**为止。理由与
        `vision.scan_reading.read_panel_confirming` 同形：粘连是读不出，不是没翻到，
        在同一张截图上换配方比重新拖一屏便宜得多；而这里换配方还有第二个理由——
        实测 3× LANCZOS 会把 `9` 读成 `8`（见 `pirate_ui.PLANET_LIST_COORD_RECIPES`），
        错的那一套给出的不是空结果而是**另一颗星球**。

        一套都读不出来就交空清单出去，调用方于是什么都不点。
        """
        import pytesseract

        from evo_helper.game.planet_list import coordinate_words
        from evo_helper.vision.scan_reading import COORD_WHITELIST, COORDINATE_RE

        # 视口漂了的话坐标列 ROI 框的是别处的像素，而这里读出来的 y 是要拿去点的。
        self._ensure_geometry()
        image = self._driver.capture()
        for upscale, resample in pirate_ui.PLANET_LIST_COORD_RECIPES:
            words = coordinate_words(
                image, pytesseract, upscale=upscale, resample=resample, whitelist=COORD_WHITELIST
            )
            if any(COORDINATE_RE.search(text) for _y, text in words):
                return words
        # 空行会安全地挡住派遣，但不能只留下 ``[[]]``：实机上同一套配方在手工
        # 截图里读得到三颗星球，运行时读空就说明画面时序或 tesseract 配置变了。
        # 留下原图和每档读数，下一轮才能校准，而不是盲目重试。
        self._dump_frame("planet-list-unreadable")
        configured = getattr(pytesseract.pytesseract, "tesseract_cmd", "<unset>")
        say(f"  行星列表坐标 OCR 全空；tesseract={configured!r}")
        return []

    def _fleet_origin_text(self) -> str:
        """派遣面板「起点」那一行的读数。读不出来就交空串（= 没切成）。"""
        for upscale, resample in pirate_ui.FLEET_ORIGIN_RECIPES:
            text = self._read_coord_line(pirate_ui.FLEET_ORIGIN_ROI, upscale, resample)
            if text:
                return text
        return ""

    def _read_coord_line(self, roi: tuple[int, int, int, int], upscale: int, resample: str) -> str:
        self._ensure_geometry()
        return crop_reader(self._driver.capture(), self._ocr)(
            roi, digits=True, upscale=upscale, resample=resample
        )

    # -- 识别 ---------------------------------------------------------------

    def check_target(self, coordinate: Coordinate) -> TargetCheck:
        """行星面板上是不是「敌对海盗」，而且坐标对得上。

        **先认面板、再核坐标**，顺序不能反：坐标行（`PIRATE_COORD_ROI`）属于
        海盗面板那套布局。先核坐标的话，一次读不出就把「这一位没有海盗」判成
        `MISMATCH`，于是整轮都在复位重试——而那本来就是最常见的正常结果。

        坐标要核：导航栏偶尔会停在别的位号上（实机踩过），这时面板是真的、
        只是不是请求的那一位——照着它打就打错了目标。

        认出海盗并核过坐标，就等于回读证明了导航栏停在这一位，于是
        `navigator.confirm()`：导航器只信有证据的记忆（见 `SystemNavigator`）。
        没有海盗的那些位走 `_confirm_from_panel`，理由见那边。
        """
        title = self._read(pirate_ui.PIRATE_TITLE_ROI)
        if pirate_ui.PIRATE_TITLE_TEXT not in title:
            return self._confirm_from_panel(coordinate)
        wanted = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
        shown = self._read(pirate_ui.PIRATE_COORD_ROI, digits=True)
        if wanted not in shown:
            say(f"  坐标核对不过：面板显示 {shown!r}，请求的是 {wanted}")
            self._dump_coord_mismatch("pirate-coord-mismatch")
            return TargetCheck.MISMATCH
        self._navigator.confirm(coordinate)
        return TargetCheck.CONFIRMED

    def _confirm_from_panel(self, coordinate: Coordinate) -> TargetCheck:
        """面板上没有海盗时，仍旧把坐标行回读一遍。返回 `ABSENT` 或 `MISMATCH`。

        为什么非读不可：海盗链路 1–4 位里绝大多数是空位，如果空位不留下任何证据，
        导航器的缓存就永远建立不起来（`goto()` 之后 `current` 是 None），每一位都要
        重设三个字段——每位白花约 6 秒。这一次读是**用一次窄 ROI 的 OCR 换掉两次
        字段输入**，量级差一个数。

        读的是坐标行而不是「有没有海盗」，用的是全仓那份唯一的判据
        （`read_panel_confirming`，坐标扫描器每一位都在用它，无主行星照样读得出
        「荒芜行星 + 坐标」）。三种结果各有各的善后：

        - **读通且就是请求的那一位** → 确认缓存，照常报「这一位没有海盗」。
        - **读通但是别的坐标** → 导航漂了，判 `MISMATCH` 交给 `_goto_checked` 自愈。
          这一档补的是实机上最贵的那种静默故障：缓存和导航栏分岔之后，44 个目标
          一路报「不是海盗」把整轮走完，日志上和「今天这几位真没海盗」一模一样。
        - **读不出坐标**（面板没铺开、被浮层压着） → **既不确认也不指控**：
          不确认，下一趟自然把三个字段都重设；不指控，免得一次 OCR 抖动就换来
          一次复位重试。
        """
        from evo_helper.vision.scan_reading import COORDINATE_RE, read_panel_confirming

        requested = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
        panel = read_panel_confirming(crop_reader(self._driver.capture(), self._ocr), requested)
        if panel.confirms(requested):
            self._navigator.confirm(coordinate)
            return TargetCheck.ABSENT
        if COORDINATE_RE.search(panel.coordinate_text):
            say(f"  坐标核对不过：面板读作 {panel.coordinate_text!r}，请求的是 {requested}")
            self._dump_coord_mismatch("pirate-coord-drift")
            return TargetCheck.MISMATCH
        return TargetCheck.ABSENT

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
        timer = StepTimer(f"{coordinate} 导航")
        self._navigator.goto(coordinate)
        timer.lap("goto")
        check = self.check_target(coordinate)
        timer.lap("核对面板")
        if check not in self.RETRY_CHECKS:
            timer.say_total(check.name)
            return check
        timer.say_total(f"{check.name}，要重试")
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
            for upscale, threshold in FLIGHT_RECIPES:
                text = self._read(
                    pirate_ui.BRIEFING_FLIGHT_ROI, upscale=upscale, threshold=threshold
                )
                flight = parse_game_duration(text)
                if flight is not None:
                    return True
            return False

        if not self._settle(read_once, tries=FLIGHT_SETTLE_TRIES) or flight is None:
            # 一套配方都没读出来时**必须留下像素**。这一行现在是两个钟的唯一来源
            # （战报到点时刻 + 航线空出时刻），读不出来的代价是那一发按
            # `UNKNOWN_LINE_HOLD`（90 分钟）占着航线；而只留一句话的话，
            # 下一次查这件事只能靠猜——2026-08-13 收紧解析判据之后正是如此。
            say("  简报上读不到飞行时间；这一发照派，回程闹钟留空")
            self._dump_frame("briefing-flight-unreadable", pirate_ui.BRIEFING_FLIGHT_ROI)
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

        `preset` 允许按次指定：bot 那条链路用自己的预设（`domain.bot_round.
        BOT_ATTACK_PRESET`，见 `tools.bot_loop`），而海盗链路始终用同一个。

        **只按标题选预设，不读预设内容**（用户口径 2026-08-09）：内容是用户自己在
        游戏里维护的，助手去核对既多余、也会把「用户改了预设」误判成故障。
        """
        wanted = preset or self._options.preset
        timer = StepTimer(f"{coordinate} 攻击")
        self._driver.click(*self.ATTACK_BUTTON, label="攻击")
        self._driver.wait(DISPATCH_WAIT_S)
        timer.lap("开面板")

        picker = PresetPicker(
            driver=_PresetPickerDriver(self._driver), read_names=self._preset_names
        )
        try:
            picker.pick(wanted)
        except PresetNotFound as error:
            timer.lap("翻预设条")
            timer.say_total("没找到预设")
            say(f"  {error}；关掉面板，不打这一发")
            # 预设名的 OCR 与拖动位置必须一起看；只留合并后的词表无法判断是
            # 识别错了，还是预设条根本没有横向移动到 BBB / CCC 所在的卡片。
            self._dump_frame("preset-not-found")
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

        timer.lap("翻预设条")
        self._driver.click(*pirate_ui.DISPATCH_CONFIRM, label="确认终点")
        self._driver.wait(BRIEFING_WAIT_S)
        # 绿✓ 之后出来的未必是简报页：目标在保护期、或者一条战舰都选不出来时，
        # 这里弹的是那种单按钮弹窗。**先认再走**，而且要在记意图之前。
        if not self._handle_dialog(coordinate):
            timer.say_total("弹窗挡下")
            self._leave_dispatch_list()
            return False
        intent_id = self._record_intent(coordinate, preset=wanted)
        # ⚠️ **这一行必须留在 `_launch` 之前。** 点完「出发！」简报页就没了，
        # 挪到后面读，四次重试全会落空，飞行时间永久恒为 NULL——而且一声不响，
        # 看起来只是「一直在等」。
        flight = self._read_flight_time()
        timer.lap("简报")
        if not self._launch(coordinate, "攻击"):
            timer.say_total("点不出「出发」")
            self._leave_dispatch_list()
            return False
        self._record_dispatch(intent_id, flight)
        self._outcome.attacked.append(coordinate)
        timer.lap("出发")
        timer.say_total("派出")
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
        半截的邮件——读出来是空主题，而画面看着完全正常。**而「拖几次算到顶」
        不能猜**，理由整段在 `_scroll_mail_list_to_top`。
        """
        self._reset_to_known_screen()
        if not self._goto_planet_surface():
            # 判据失败时最贵的事是「不知道当时画面长什么样」。存一帧的成本是一次写盘。
            self._dump_frame("planet-surface-unreachable")
            self._say_mail_badge_reads()
            raise RuntimeError("切不到自己星球地表，读不了信箱；安全停止")
        self._open_mail()
        self._scroll_mail_list_to_top()

    def _scroll_mail_list_to_top(self) -> None:
        """把邮件列表拖回真正的顶部：**拖到拖不动为止**，不是拖固定次数。

        ⚠️ **这是 2026-08-13 那夜「17 发攻击 0 份战报」的正因。** 原先是无条件
        拖 3 次（≈14 行），而一趟对账要往下翻 8 屏（≈32 行），列表又记着上次
        滚到哪——每趟净沉约 18 行，信箱 600 封，永远沉不到底。现场图
        `dump-mail-detail-unrendered-053043.png` 拍到的就是结果：21:30 UTC 时
        列表最上面是 16:42–17:02 的侦察报告，而同一屏的角标写着「战斗 10」未读。
        战报一直躺在列表顶上，扫描窗口停在四个半小时之前。

        判据是「拖了一下还是那几封」，与 `_scan_mail_rows` 里判「翻到底了」
        用的是同一条（也与 `domain.planet_switch.list_exhausted` 同形）：
        **比行身份，不比位置**——慢拖带惯性，位置每次都差几个像素。

        多付的代价是每次拖之前读一屏主题（一次截图 + 六次窄 ROI OCR ≈ 1–2 秒）。
        到顶之后的稳态是 7–8 次，约 25 秒一趟；换回来的是这一趟真的能看见
        今天的战报。读不出行（全空）时**不当成到顶**：那是 OCR 没读出来，
        照拖不误，最坏走满上限。
        """
        previous: list[tuple[str, str]] | None = None
        for drag in range(MAIL_SCROLL_TO_TOP_MAX_DRAGS):
            identities = [row.identity for row in self._mail_list_rows()]
            if identities and identities == previous:
                if drag > 1:
                    say(f"  列表往上拖了 {drag} 次才到顶（上一趟停在很深的地方）")
                return
            previous = identities
            slow_drag(self._driver, PANEL_DRAG_TO_Y, PANEL_DRAG_FROM_Y)
        # 走满上限说明列表比 40 次拖动还深，或者主题一直读不出来。两种都要说出来：
        # 这一趟看到的「最上面几行」不是信箱最上面几行，收不到战报是**必然**的。
        say(f"  往上拖满 {MAIL_SCROLL_TO_TOP_MAX_DRAGS} 次仍没到顶；这一趟看到的不是信箱最新的几封")

    def _scan_mail_rows(
        self,
        *,
        wanted: ReportKind | Sequence[ReportKind],
        label: str,
        visit: Callable[[MailRow, Any], bool],
        not_before: datetime | None = None,
        max_pages: int = MAIL_SCAN_PAGES,
        max_opens: int = MAIL_MAX_OPENS,
        observe: Callable[[MailRow], None] | None = None,
    ) -> MailScan:
        """进一趟信箱，把**主题看着对得上**的报告逐封打开交给 `visit`。

        返回这一趟的账单（翻了几屏、开了几封）。活链路的三个调用方都不看它——
        它是给补录入口打摘要用的（`backfill_reports`），因为「翻了 12 屏开了 0 封」
        和「翻了 1 屏开了 0 封」对人的意思完全不同，而日志里原先分不出来。

        `visit(row, page)` 返回 True 表示「要的都收齐了」，这一趟就此收工。
        `not_before` 是「要找的报告最早可能是什么时候」：列表按时间倒序，翻到比它
        更早的那一行，往下就全是旧报告，可以立刻收工。

        `observe(row)` 是**看每一行（不开封）**的旁路，开工对账用它数今天已经有
        多少份战报（见 `reconcile_today`）。它和开封是两笔独立的预算，这一点是
        判据的一部分而不是实现细节：

        - 开封受 `max_opens` 和「收齐了」两道限制，因为开一封 ≈8 秒；
        - **数数不受它们限制**，只受 `max_pages` 与 `not_before` 限制。
          反过来写（开封停了就整趟停）会让计数在换过库、当天战报一份都没入库时
          只数到最前面那八行，把「今天打了 20 发」记成「今天打了 8 发」——
          而计数偏小正是会超额的那一侧。
        - 没有 `observe` 的那些调用（收侦察报告、补录）行为一个字不变：
          收齐了就收工，没有任何再翻下去的理由。

        `max_pages` / `max_opens` 默认就是活链路那两个上限，它们是按「一轮在等
        6–8 份报告」定的，**不要为了补录去调大默认值**：活链路每一轮都要付这个
        时间，而补录是人手动跑的一次性动作。补录入口自己传大值
        （见 `backfill_scout_reports`）。

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
        scan = MailScan()
        opened = 0
        collected = False
        budget_noted = False
        done = False
        reentries = 0
        #: 上一屏的行身份。判「拖到底了」只能靠它，见下面 `fresh` 为空那一段。
        last_identities: list[tuple[str, str]] | None = None
        for page in range(max_pages):
            if done:
                break
            scan.pages = page + 1
            # ⚠️ **每次点行之前都要先确认「还在邮件列表上」。** 实机踩过两次同一个错：
            # 上一次返回没退到列表（或把整个信箱关掉了），接着照列表的行坐标点下去，
            # 于是点在了地表 UI 上——一次点开了「取消任务」确认框，一次点开了「排名」。
            if not self._settle(self._on_mail_list):
                # **丢了列表就重进信箱接着翻，不是中止整趟。**
                #
                # 实机 2026-08-13 20:33：补录跑到第 3 屏时点开一封主题被 OCR 糊掉的
                # 侦察报告，详情页标题读到「侦察」而不是「消息」，判据正确地拒了它；
                # 但接着那一下 `MAIL_BACK` 落在一个不是详情页的画面上——那个坐标
                # 身兼两职（在 `_reset_to_known_screen` 里它是「关闭面板」），于是
                # 整个信箱被关掉了。原先的处置是 `break`：**30 屏的预算只走了 3 屏，
                # 却打印出一行长得像成功的「完成」**，而那一趟要救的 21 份战报
                # 一份都没碰到。
                #
                # 重进的代价是回到信箱顶部、把已经翻过的几屏重扫一遍（只读主题，
                # 不重复开封——`seen` 挡着），比起丢掉整趟便宜得多。
                if reentries >= MAIL_MAX_REENTRIES:
                    say(f"  已经不在邮件列表上了，重进 {reentries} 次都没用；这一趟到此为止")
                    scan.cut_short = f"丢了邮件列表，重进 {reentries} 次都没回去"
                    break
                reentries += 1
                say(
                    f"  已经不在邮件列表上了；重进信箱接着翻"
                    f"（第 {reentries}/{MAIL_MAX_REENTRIES} 次）"
                )
                self._enter_mailbox()
                # 重进之后**不在这里再判一次**：判了就得决定「不成怎么办」，而那正是
                # 下一轮循环开头那道守卫的活。交给它，重进预算才真的是预算——
                # 在这里 break 的话，第 2 次重进永远走不到。
                last_identities = None
                continue
            rows = self._mail_list_rows()
            identities = [row.identity for row in rows]
            fresh = [row for row in rows if row.identity not in seen]
            if not fresh:
                # ⚠️ **「这一屏没有新邮件」不等于「翻到底了」。** 重进信箱之后画面回到
                # 顶部，头几屏必然全是见过的——原先在这里 `break`，等于让上面那条
                # 重进永远走不到新内容，白重进。
                #
                # 真正的到底判据是**拖了一下还是同样几封**（与
                # `domain.planet_switch.list_exhausted` 同一条）。比行身份而不是比
                # 位置：身份取自主题+时间，拖动带惯性时位置会差几像素，按位置比会
                # 永远判「还能拖」。
                if identities and identities == last_identities:
                    say(f"  第 {page + 1} 屏拖不动了（还是那几封）；不再往下翻")
                    break
                say(f"  第 {page + 1} 屏没有没见过的邮件；接着往下翻")
                last_identities = identities
                if page + 1 < max_pages:
                    slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
                continue
            last_identities = identities
            seen.update(row.identity for row in fresh)
            for row in fresh:
                if observe is not None:
                    observe(row)
                if row.is_older_than(not_before):
                    say(
                        f"  第 {row.index} 行是 {row.raw_time_text} 的报告，比要找的那几发还早；"
                        "列表按时间倒序，往下都是旧的，收工"
                    )
                    done = True
                    break
                if collected:
                    continue
                if opened >= max_opens:
                    if not budget_noted:
                        budget_noted = True
                        say(f"  这一趟已经开了 {opened} 封，到上限；剩下的留给下一趟")
                    continue
                if not row.may_be(wanted):
                    say(f"  第 {row.index} 行不是{label}（主题读作 {row.subject!r}）；不打开")
                    continue
                opened += 1
                scan.opened = opened
                # ⚠️ **开封的那些行原先一个字都不打印**，只有被跳过的行有日志——
                # 正好是不需要的那一半。2026-08-13 复盘时，「那 59 封开的到底是
                # 什么」在证据上是个黑洞：日志里 239 条主题全是跳过的行，
                # 而「VS 块读不出来」那 53 次连主题都没留下。
                when = row.raw_time_text or "时间读不出"
                say(f"  第 {row.index} 行开封（{when} {row.subject!r}）")
                if self._open_mail_row(row, visit):
                    collected = True
            # 不再开封之后还翻不翻，取决于**有没有人在数数**：
            # 数数要的是「今天一共几份」，那个数不能被开封预算截断。
            if collected and observe is None:
                done = True
            if not done and page + 1 < max_pages:
                slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
        else:
            # `for ... else`：**没有 break，也就是把翻页上限用满了**。
            # 那意味着既没翻到 `not_before`、也没拖到底——信箱比预算深，
            # 下面还有没看过的邮件。这同样不是「好好走完」，摘要要说出来。
            if not done:
                scan.cut_short = f"翻满了 {max_pages} 屏的上限，信箱还没到底"
        self._close_mail()
        return scan

    def _open_mail_row(self, row: MailRow, visit: Callable[[MailRow, Any], bool]) -> bool:
        """点开一行、等它铺开、交给 `visit`，然后退回列表。返回「可以收工了」。

        铺不开就存一帧（封顶）并当作这一封读不出来。**不读没铺开的那一屏**：
        读出来的字和「这封是别人的报告」分不开，而分不开就等于静默丢掉一份战报。

        ## 退回列表要**确认**，不是点一下就走

        实机 2026-08-13：主题被 OCR 糊掉的侦察报告会被放进来（主题筛故意往「开」
        的一侧倒），它的详情页标题是「侦察」不是「消息」，判据正确地拒了它——
        但接着那一下 `MAIL_BACK` 落在一个**不是详情页**的画面上，而那个坐标身兼
        两职（在 `_reset_to_known_screen` 里它是「关闭面板」），于是整个信箱被关掉。

        代价不是丢一封，是丢**一整趟**：调用方发现不在列表上，只能重进信箱、
        从顶部重扫（`_scan_mail_rows` 的 `MAIL_MAX_REENTRIES`）。那一趟 30 屏的
        预算里，12 屏花在重扫上、两次重进用尽仍然没走到要救的战报那里。

        所以这里多花一次读屏确认：回到列表了就走，没回去就**再点一次**。
        两次都不成才交给调用方去重进——那条路仍然在，只是不该动不动就走。
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
        for attempt in range(MAIL_BACK_ATTEMPTS):
            self._driver.click(*MAIL_BACK, label="返回")
            self._driver.wait(MAIL_BACK_WAIT_S)
            if self._settle(self._on_mail_list):
                return done
            if attempt + 1 < MAIL_BACK_ATTEMPTS:
                say("  返回之后没回到邮件列表；再点一次")
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

        **读到的每一份都落库**（`_store_scout_reading`），包括「不在本轮目标里」
        的那些。入库是加出来的一步，返回值仍旧只是本轮要的那几份——
        `_decide_and_attack` 的入参一个字没变。
        """
        from evo_helper.vision.scout_reports import ScoutReportUnreadable, read_pirate_scout

        found: dict[Coordinate, Any] = {}
        remaining = set(wanted)

        def visit(row: MailRow, header: Any) -> bool:
            if row.kind is ReportKind.PLANET_SCOUTED:
                self._ingest_planet_scout_alert(row, header)
                return False
            # 舰种清单在详情页下半屏，要拖到底才看得到；VS 那一段则在拖之前读。
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            ships = self._report_screens()
            try:
                reading = read_pirate_scout(header, ships)
            except ScoutReportUnreadable as error:
                say(f"  第 {row.index} 行读不出侦察报告：{error}")
                return False
            # ⚠️ **落库要排在归档之前，而且不管它在不在本轮目标里。** 这条链路
            # 每一轮都当作没侦察过，于是同样四颗星球被来回重侦（2026-08-11 当天
            # 31 发派遣里 25 发是重复侦察）。要看得出这件事，恰恰要靠「这一份不是
            # 我这轮要的」那些行——它们正是上几轮侦察留下的证据。
            self._store_scout_reading(reading)
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
            wanted=(ReportKind.SCOUT, ReportKind.PLANET_SCOUTED),
            label="侦察报告或安全告警",
            visit=visit,
            not_before=self._started_at if self._options.scout else None,
        )
        for coordinate in sorted(remaining, key=_coordinate_order):
            say(f"  {coordinate} 的侦察报告这一趟没翻到")
        return found

    def _store_scout_reading(self, reading: Any) -> bool:
        """把一份读通了的侦察报告落进 `scout_reports`。已经有了就不写，返回 False。

        写侧与 `bot_loop` 收战报那一段同形：先按「目标 + 报告时间」问一句，
        再写。活链路每一轮都会翻信箱里同样那几行，没有这道去重，一份报告
        会每趟复制一行。

        ⚠️ **读不通的不进这里**（`read_pirate_scout` 已经抛掉了），而读通了的
        **原样存**：哪几格没读出来就存成 `NULL`，不补 0。判定留给读的人现算，
        库里只放证据（见 `domain.records.ScoutTriggerShip`）。
        """
        from evo_helper.application.report_ingest import to_scout_report

        repository, _run_id = self._ensure_run()
        if repository.has_scout_report_at(reading.target, reading.reported_at_utc):
            return False
        repository.append_scout_report(to_scout_report(reading, report_id=uuid4()))
        return True

    def _ingest_planet_scout_alert(self, row: MailRow, page: Any) -> None:
        """Persist and notify a foreign-reconnaissance mail exactly once.

        This method is only reached from an existing attack/scout mailbox scan;
        it neither opens the mailbox itself nor schedules a poll.  Persisting
        before SMTP means a restarted process cannot resend a mail already
        accepted by the provider.
        """
        from evo_helper.application.alert_email import deliver_planet_scout_alert
        from evo_helper.application.report_ingest import to_planet_scout_alert
        from evo_helper.vision.planet_scout_alert import (
            PlanetScoutAlertUnreadable,
            read_planet_scout_alert,
        )

        try:
            reading = read_planet_scout_alert(page, subject=row.subject)
        except PlanetScoutAlertUnreadable as error:
            say(f"  第 {row.index} 行安全告警读不完整：{error}")
            return
        alert = to_planet_scout_alert(reading, alert_id=uuid4())
        repository, _run_id = self._ensure_run()
        if not repository.append_planet_scout_alert(alert):
            say(f"  第 {row.index} 行安全告警 → {alert.target}（已记录，不重复推送）")
            return
        delivery = deliver_planet_scout_alert(Settings(), alert)
        repository.record_planet_scout_alert_delivery(
            alert.alert_id,
            status=delivery.status,
            error=delivery.error,
            delivered_at_utc=delivery.delivered_at_utc,
        )
        if delivery.status == "SENT":
            say(f"  第 {row.index} 行安全告警 → {alert.target}（已记录并发送邮件）")
        elif delivery.status == "NOT_CONFIGURED":
            say(f"  第 {row.index} 行安全告警 → {alert.target}（已记录；SMTP 未配置）")
        else:
            say(f"  第 {row.index} 行安全告警 → {alert.target}（已记录；邮件发送失败）")

    def prepare_for_mailbox(self) -> None:
        """开工前的两步：校几何、查会话。与 `run()` 开头同序、同理由。

        - 几何先校：窗口会在运行中自己缩回去，缩了之后所有 ROI 读的都是别处的像素，
          而且一声不响。
        - 再查会话：掉线时画面停在登录页，面板永远读不出来，翻信箱纯属白费。

        ⚠️ **这两件事都会伸手动操作系统**（`ensure_game_window` 改真实窗口尺寸），
        所以单独成一个方法、只由实机入口调用，而不是塞进
        `backfill_scout_reports` 那样要写单元测试的方法里。
        """
        from evo_helper.game.game_window import ensure_game_window

        ensure_game_window()
        self._ensure_session(force=True)

    def backfill_scout_reports(
        self,
        *,
        not_before: datetime | None = None,
        max_pages: int = BACKFILL_SCAN_PAGES,
        max_opens: int = BACKFILL_MAX_OPENS,
    ) -> tuple[int, int]:
        """把**信箱里已经躺着的**侦察报告补进库。返回 (读通几份, 新写了几份)。

        补录与活链路读的是同一条路径、同一套判据、同一份去重口径，区别只有两处：

        - **一发都不派。** 这里只翻信箱、只读、只写库；调用方给的 `LiveDriver`
          虽然必须允许点击（开信箱、开邮件、返回都要点），但这条路径上不存在
          任何派遣动作。
        - **预算大得多。** 活链路那两个上限（4 屏 / 8 封）是按「一轮在等 6–8 份
          报告」定的；补录要把一整天的报告翻出来，一天可能有几十份。

        ⚠️ **信箱里只切「报告」标签**，别的筛选一个都不碰——这条走的仍是
        `_scan_mail_rows`，白名单由 `tests/unit/tools/test_mailbox_clicks.py` 钉着。

        ⚠️ **校几何与查会话不在这里做**，由调用方先调 `prepare_for_mailbox()`。
        这不是风格问题：`ensure_game_window()` 会去找并**改真实窗口的尺寸**，
        混在这个方法里，任何一条忘了打桩的单元测试都会伸手去动用户的窗口——
        写这条测试时就真的这么干了一次（连试三次改 1539×874 那个窗口）。
        会动操作系统的调用要留在只有实机才走的那一层。
        """
        from evo_helper.vision.scout_reports import ScoutReportUnreadable, read_pirate_scout

        read = 0
        written = 0

        def visit(row: MailRow, header: Any) -> bool:
            nonlocal read, written
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
            ships = self._report_screens()
            try:
                reading = read_pirate_scout(header, ships)
            except ScoutReportUnreadable as error:
                # 读不出来就**不存**，不存半份，不猜。留一句话就够——补录是人盯着跑的。
                say(f"  第 {row.index} 行读不出侦察报告：{error}")
                return False
            read += 1
            if self._store_scout_reading(reading):
                written += 1
                say(f"  第 {row.index} 行 → {reading.target} {reading.verdict}（已入库）")
                return False
            # **读到库里已有的那一份就收工。**（用户口径 2026-08-11）
            #
            # 信箱是按时间**从新往旧**排的，而入库也是从新往旧写的。所以碰到第一份
            # 「库里已有」时，它往下的每一份都必然更旧、也必然已经在库里了——再翻
            # 下去只是一封封开、一封封确认「已有」，每封约 8 秒。
            #
            # 这条对「同一天多次启动」尤其重要：每次重启都要重新翻一遍信箱，没有
            # 这个早停，第二次、第三次启动都要把当天所有报告重开一遍。
            say(f"  第 {row.index} 行 → {reading.target} {reading.verdict}（库里已有）")
            say("  往下都是更旧的报告，收工")
            return True

        self._scan_mail_rows(
            wanted=ReportKind.SCOUT,
            label="侦察报告",
            visit=visit,
            not_before=not_before,
            max_pages=max_pages,
            max_opens=max_opens,
        )
        return read, written

    # -- 开工：先读战报，再更新计数 ------------------------------------------

    def _ingest_report(self, row: MailRow, page: Any) -> ReportIngest:
        """把详情页上这一封读成一条战报并入库。**子类按自己的战报格式覆盖。**

        海盗这一份走 `vision.pirate_reports.read_pirate_report`，与离线入口
        `tools.ingest_pirate_report` 共用同一段读法与同一套仲裁：**胜负以画面横幅
        为准**（用户口径 2026-08-17：「游戏算法更新，剩余舰艇算法已经不准了，
        可以读 victory」），横幅读不出来才回落到 `domain.battle_outcome` 的算式。

        要两屏：`page` 是没拖过的那一屏（主题、时间、VS 块、横幅、「单位」），
        `_bottom_screens()` 是拖到底那一屏（「损失单位」）。战损读不出来照旧整份
        拒收——它是这条记录的另一半正文；胜负则要**两条路都定不出**才拒。

        入库走 `append_report`，它按「出发坐标 + 目标坐标 + 时间就近」自己认领那一发
        派遣（置 `dispatch_id`），攻击日志的战果列与「这一发打完了没有」都接在那上面。
        这里**不另做匹配，更不补派遣行**。
        """
        from evo_helper.application.report_ingest import to_pirate_battle_report
        from evo_helper.vision.pirate_reports import PirateReportUnreadable, read_pirate_report

        bottom = self._bottom_screens()
        try:
            reading = read_pirate_report(page, bottom)
        except PirateReportUnreadable as error:
            # 读不出来就**不存**，不存半份，不猜。下一趟这一封还在信箱里。
            say(f"  第 {row.index} 行读不出海盗战报：{error}")
            return ReportIngest.UNREADABLE
        repository, _run_id = self._ensure_run()
        if repository.has_report_at(reading.defender_target, reading.reported_at_utc):
            note = rematch_note(repository, reading.defender_target, reading.reported_at_utc)
            target = reading.defender_target
            say(f"  第 {row.index} 行 → {target} {reading.outcome}（库里已有{note}）")
            return ReportIngest.KNOWN
        report_id = uuid4()
        repository.append_report(to_pirate_battle_report(reading, report_id=report_id))
        say(
            f"  第 {row.index} 行 → {reading.defender_target} {reading.outcome}"
            f"（战损 我 {reading.attacker_losses} · 敌 {reading.defender_losses}；已入库）"
        )
        self._store_report_screenshot(report_id, page)
        return ReportIngest.STORED

    def _store_report_screenshot(self, report_id: UUID, page: Any) -> None:
        """把这一屏的战报面板存进库，挂在这份战报上。

        **只在真的读到并存下一份战报时才走到这里**（用户口径 2026-08-17：
        不要每次进邮件都截）。「库里已有」和「读不出来」两档都在上面就返回了。

        ⚠️ **一句异常都不许漏出去。** 这是一条旁路：图存不下顶多是攻击日志上少
        一个链接，而漏出去的异常会打断 `_scan_mail_rows` 那一趟——也就是把
        「战报读不回来」这个正在修的故障重新造一遍，只是换了个成因。

        用的是 `page` 手里那一屏已经拍好的像素（未滚动那一屏，`战报` 横幅与 VS
        块只在它上面），不另拍一次，理由在 `report_panel_image`。
        """
        try:
            panel = page.report_panel_image()
            saved = ReportScreenshotRepository(self._ensure_session_factory()).save(
                report_id,
                image_bytes=panel.image_bytes,
                width=panel.width,
                height=panel.height,
                captured_at_utc=datetime.now(UTC),
                image_format=panel.image_format,
            )
        except Exception as error:  # noqa: BLE001 - 见 docstring：旁路不许拖累主路径
            say(f"  战报截图没存下（{error}）；战报本身已入库，不影响判据")
            return
        if saved:
            say(f"  战报截图已入库（{panel.width}×{panel.height}，{len(panel.image_bytes)} 字节）")

    def _ingest_report_row(self, row: MailRow, page: Any) -> bool:
        """开工那一趟里开的每一封都走这里。返回「不必再开封了」。

        **读到库里已有的那一份就不再开封**（用户口径 2026-08-11）。信箱按时间
        从新往旧排，入库也是从新往旧写的，所以碰到第一份「已有」时，它往下的每一份
        都必然更旧、也必然已经在库里——再开下去只是一封封确认「已有」，每封约 8 秒。
        这条对「同一天多次启动」尤其要紧：每次重启都要重新翻一遍信箱。

        ⚠️ **但早停要先问过那张单子。** 「库里已有 ⇒ 往下都读过了」这个假定有个
        实机踩到的盲点：报告确实在库里，**却没接到该接的那一发派遣上**
        （`repository.rematch_report_at` 记着 2026-08-11 那四发的全过程）。
        那时早停会把这几发永久钉在「待战报」——每一趟都在第一封就收工，
        而要找的那几封就躺在它下面。

        所以两条并存，取舍是**单子说了算**：
        `repository.due_attack_dispatches()` 里还有条目，就接着往下开；
        单子空了（该有的战报都认领上了）才收工。单子本身是有界的——
        超过 `MAX_REPORT_AGE` 的派遣不在里面，所以一发真丢了的战报不会让这一趟
        永远开下去；开封数照旧封在 `MAIL_MAX_OPENS`。

        单子是**每封重查**而不是开工时算一次就拿着走：刚刚那一封入库/重认之后，
        它认领的那一发就该从单子上消失。一次本地 SQLite 查询与一封八秒的开封
        比起来可以忽略，而拿着一张过期的单子只会多开几封。

        ⚠️ **只停开封，不停这一趟。** 数数还要接着往下翻（见 `_scan_mail_rows` 的
        `observe`）：库里已有多少份和信箱里今天有多少份是两件事，而配额要的是后者。
        """
        if self._ingest_report(row, page) is not ReportIngest.KNOWN:
            return False
        return self._stop_after_known()

    def _stop_after_known(self) -> bool:
        """撞见一封「库里已有」之后，还要不要接着开封。

        单独成一个方法，是为了让**补录入口能原样复用这条判据**
        （`backfill_reports`）——补录现在挂在控制台「开始」按钮上，每次点开始都会
        跑一趟，没有早停就等于每按一次开始都要把 60 封的预算烧满（十几分钟）。
        判据只有这一份，不许在补录那边另写一条。
        """
        outstanding = self._due_dispatches(datetime.now(UTC))
        if outstanding:
            say(
                f"  这一封库里已有，但单子上还有 {len(outstanding)} 发到点没战报"
                f"（{_targets_note(outstanding)}）；接着往下开"
            )
            return False
        say("  往下都是更旧的报告，不再开封")
        return True

    def _routine_scan_floor(self, not_before: datetime | None, *, now: datetime) -> datetime:
        """**对账那一档**最早翻到哪一行为止：`not_before` 与「现在往回 N 小时」取更晚的。

        N 取攻击配置页上那个框；留空就是 `DEFAULT_REPORT_SCAN_FLOOR`（6 小时）。
        用户口径（2026-08-17）：「不要读那么多，毕竟数量是大几百封」「这个参数改为
        可配置，这样遇到活动我可以灵活调整」。

        ## 为什么默认是 6 小时——这不是性能优化

        对账那一趟的活是**把还在等的那几发的战报读回来**，而「还在等」本身就以
        6 小时为界：`due_attack_dispatches` 与 `bot_dispatch_facts` 都按
        `MAX_REPORT_AGE` 把更早的派遣剔掉，`storage.intel.RESULT_NO_REPORT` 也在
        那一刻把它们判成「战报永远不会来了」。再往下翻，翻到的都是**没有任何一条
        判据还在等的**战报。

        ⚠️ **别把它读成「6 小时以上的战报认领不上」。** 认领窗口是
        `dispatched_at_utc >= reported_at - MAX_REPORT_AGE`，相对**战报自己的
        时间戳**算，隔多久读回来都认领得上。所以把这个数调大**确实**能补回更早的
        战报——只是那是 `--exhaustive` 手动补录的活，而补录不走这个下限。

        ## 取更晚的那个，不是覆盖

        `not_before`（`--since`）是调用方给的硬下界，这道下限只会让它**更紧**。
        反过来（取更早的）会让一个配大了的时长把 `--since` 顶开，翻到用户没要的
        日期去。

        配置读不到时（老库、`ensure_mission_rows()` 还没跑、仓储替身没有这个方法）
        一律当留空：一个还没初始化的配置表说明不了「用户想改翻信箱时长」，
        为它把整趟对账停掉是不成比例的。同 `MissionScheduler._blind_scrolls`。
        """
        hours = self._report_scan_hours()
        span = DEFAULT_REPORT_SCAN_FLOOR if hours is None else timedelta(hours=hours)
        floor = now - span
        source = "默认" if hours is None else "攻击配置页"
        say(f"  对账只往回读 {int(span.total_seconds() // 3600)} 小时（{source}）")
        if not_before is None:
            return floor
        return max(not_before, floor)

    def _report_scan_hours(self) -> int | None:
        """攻击配置页上那个「翻信箱时长」。留空 / 读不到 / 不是正数都返回 None。

        **不在这里自己回落成一个数字**：默认值只该有一处
        （`DEFAULT_REPORT_SCAN_FLOOR`），写第二遍日后必然漏改。

        库里那个值也要复核一遍而不是照单全收：页面那把尺子
        （`MissionScheduler.validate_report_scan_hours`）管不到直接改库的人，
        而一个 0 或负数会让下界落在「此刻」或之后——那一趟一封都翻不到，还一声不响。
        """
        repository, _run_id = self._ensure_run()
        reader = getattr(repository, "military_attack_config", None)
        if reader is None:
            return None
        try:
            row = reader()
        except ValueError:
            return None
        hours = getattr(row, "report_scan_hours", None)
        if not isinstance(hours, int) or isinstance(hours, bool) or hours < 1:
            return None
        return min(hours, REPORT_SCAN_HOURS_MAX)

    def backfill_reports(
        self,
        *,
        not_before: datetime | None,
        max_pages: int = BACKFILL_SCAN_PAGES,
        max_opens: int = BACKFILL_MAX_OPENS,
        exhaustive: bool = False,
        now: datetime | None = None,
    ) -> BackfillTally:
        """把**信箱里已经躺着的**战报补进库。只读信箱、只写库，一发都不派。

        与开工那一趟（`reconcile_today`）读的是同一条路径、同一套判据、同一条
        去重口径（`has_report_at`），区别只有三处：

        - **不数今天的份数、不写 `daily_reconciliations`。** 补录会往回翻到昨天
          甚至更早，那一趟数出来的「今天有几份」是错的；而 `record_daily_
          reconciliation` 按 UTC 日取大，写进去就抹不掉了。
        - **预算大得多**（12 屏 / 60 封，见 `BACKFILL_SCAN_PAGES`）。
        - **`exhaustive` 决定早停还不早停**，见下。

        ## 两种模式，判据必须分开

        这个入口有两个完全不同的用法，混成一个判据的后果是二选一——要么每次点
        「开始」都跑满 60 封，要么手动补录永远救不回过期的那些：

        - **对账模式**（默认，控制台点「开始」时走这条）：撞见一封「库里已有」
          且单子（`due_attack_dispatches`）已经空了就收工，判据原样复用
          `_stop_after_known`。于是 `max_opens` 是**封顶而不是指标**：没有欠账时
          几十秒走完，有欠账才真开封。这是它能被放进「每次点开始」这条路的前提。
        - **补录模式**（`exhaustive=True`，人手动救过期的那些）：一直翻到
          `not_before` 为止，不管单子空不空。**这一档不能省**：那些派遣早就掉出
          单子了（`due_attack_dispatches` 有 6 小时上限），早停会让它一封都开不了。

        ## 时间下限只作用在对账那一档

        对账那一趟另有一道下限（`_routine_scan_floor`，攻击配置页可配，默认 6
        小时）：活动期间信箱最上面堆着几百封活动战报，而库里最近一封战报可能停在
        好几天前，于是「撞见库里已有的那一封」这个早停迟迟不触发，整趟把翻页预算
        烧满（用户口径 2026-08-17：「不要读那么多，毕竟数量是大几百封」）。

        ⚠️ **补录模式一律不受它约束。** 补录存在的唯一理由就是够到那些早就掉出
        追踪窗口的历史战报；让下限也作用在它身上，等于把这个入口悄悄废掉——
        而且**不报错**：用户会看到一趟「完成」的补录和一句「读通 0 份」。

        ⚠️ **过了六小时的战报照样认领得上**，所以补录模式是有意义的：认领窗口是
        `dispatched_at_utc >= reported_at - MAX_REPORT_AGE`，相对**战报自己的
        时间戳**算的；单子那个 6 小时是相对现在算的，管的是「还追不追」。

        ⚠️ **校几何与查会话不在这里做**，由调用方先调 `prepare_for_mailbox()`，
        理由与 `backfill_scout_reports` 那段一字不差：会动操作系统的调用要留在
        只有实机才走的那一层，否则任何一条忘了打桩的单元测试都会伸手去改用户的
        窗口尺寸。
        """
        moment = now or datetime.now(UTC)
        tally = BackfillTally(due_before=len(self._due_dispatches(moment)))
        # ⚠️ **`exhaustive` 那一档一个字都不改 `not_before`。** 见上面那段：
        # 补录要够到的正是这道下限之外的战报。
        floor = not_before if exhaustive else self._routine_scan_floor(not_before, now=moment)

        def visit(row: MailRow, page: Any) -> bool:
            if row.kind is ReportKind.PLANET_SCOUTED:
                self._ingest_planet_scout_alert(row, page)
                return False
            outcome = self._ingest_report(row, page)
            if outcome is not ReportIngest.UNREADABLE:
                tally.read += 1
            if outcome is ReportIngest.STORED:
                tally.stored += 1
            if exhaustive or outcome is not ReportIngest.KNOWN:
                return False
            return self._stop_after_known()

        tally.scan = self._scan_mail_rows(
            wanted=(self.RECONCILE_KIND, ReportKind.PLANET_SCOUTED),
            label=f"{self.REPORT_LABEL}或安全告警",
            visit=visit,
            not_before=floor,
            max_pages=max_pages,
            max_opens=max_opens,
        )
        tally.due_after = len(self._due_dispatches(datetime.now(UTC)))
        return tally

    def _due_dispatches(self, now: datetime) -> list[Any]:
        """那张单子：已派出、理论上战报早该到了、库里却还没有的那些攻击发。

        判据全在 `repository.due_attack_dispatches`；这里只负责把仓储接上。
        """
        from evo_helper.domain.report_wait import MAX_REPORT_AGE

        repository, _run_id = self._ensure_run()
        return list(
            repository.due_attack_dispatches(self.TARGET_KIND, now_utc=now, max_age=MAX_REPORT_AGE)
        )

    def _bottom_screens(self) -> Any:
        """把详情页拖到底再拍一屏：「单位」与「损失单位」两行都在那里。

        为什么非拖不可：「损失单位」在没拖的那一屏上正好被面板下沿切掉，
        七张实拍**没有一张**读得到。bot 战报还比海盗战报多一行「生成卫星概率」，
        「战斗详情」横幅因此下移约 30px，连「单位」整行都落到可视区之外
        （2026-08-11 的五张实拍里四张如此）。所以这不是锚点找错了——那些行
        **根本没画出来**，只能拖。

        ⚠️ 这一步是**判据的输入**，不只是补一个展示字段：胜负按
        「剩余 = 单位 − 损失单位」算（`domain.battle_outcome`），不拖就没有战损，
        没有战损就算不出胜负，攻击日志的战果列会一直空着。

        ⚠️ **拖之前那一屏必须先读完。** 拖到底之后 VS 块与胜负横幅都滚出可视区
        （实测拖到底的那一屏横幅读作 `'Z ?'`）。调用方拿到的 `page` 持有的是
        拖之前那一次截图的像素，所以顺序上先拿 `page`、再拖、再拍这一屏，
        两屏各读各的。

        拖两次而不是一次：面板到底会夹住（`pirate_reports` 模块头记着实测拖 280px
        与 520px 落点完全一致），多拖一次无害；而少拖一次读不到就是静默留空。
        """
        for _ in range(DETAIL_SCROLL_TO_BOTTOM_DRAGS):
            slow_drag(self._driver, PANEL_DRAG_FROM_Y, PANEL_DRAG_TO_Y)
        return self._report_screens()

    def _last_reconciled_at(self) -> datetime | None:
        """本链路上一次真正翻完信箱的时刻；从没对过账（或查不到）时 None。

        单独一个方法只为一件事：它是**冷却判据唯一的输入**，测试要能把它换掉，
        而换掉整个仓储会把这条链路上另外十几处查询一起牵进来。

        查询失败**当成「从没对过账」**，也就是这一轮翻信箱。冷却是个省钱的优化，
        而它省掉的那件事是这条链路的全部意义；拿不准的时候多翻一趟，比安静地
        不翻便宜得多——后者的代价这次已经付过了（两天、86 发）。
        """
        try:
            repository, _run_id = self._ensure_run()
            return repository.last_reconciled_at(self.TARGET_KIND)
        except Exception as error:  # noqa: BLE001 - 见 docstring：拿不准就翻
            say(f"开工对账：查不到上次对账时刻（{error}）；按「从没对过账」处理，这一轮翻信箱")
            return None

    def _reconcile_cooldown(self) -> timedelta:
        """两次翻信箱之间至少隔多久。**留空 / 读不到都走代码里的默认值。**

        值取自全局攻击配置（`military_attack_config.reconcile_cooldown_minutes`），
        **不走命令行**：调度器起 runner 时那条命令行已经很长，而这个数跟「打谁」
        毫无关系；runner 本来就连着库，直接问库比多一个参数少一处能漏改的地方。

        读不到就用默认值而不是抛异常——一个查不出来的配置说明不了「用户想改冷却」，
        为它把整轮任务弄死不成比例。默认值那一侧只会多翻几趟信箱，是安全的一侧。

        用了非默认值必须在库里留一条痕迹：不然事后翻日志会以为跑的是默认的
        15 分钟，而「本轮没翻信箱」这句话正是靠这个数才解释得通。
        """
        try:
            repository, _run_id = self._ensure_run()
            minutes = repository.military_attack_config().reconcile_cooldown_minutes
        except Exception as error:  # noqa: BLE001 - 见 docstring：读不到就走默认值
            say(f"开工对账：读不到冷却配置（{error}）；按代码默认的 {RECONCILE_COOLDOWN} 算")
            return RECONCILE_COOLDOWN
        if minutes is None:
            return RECONCILE_COOLDOWN
        cooldown = timedelta(minutes=int(minutes))
        record_knob_override(
            "reconcile_cooldown",
            source=__name__,
            effective=cooldown,
            default=RECONCILE_COOLDOWN,
            detail="两次开工翻信箱之间至少隔这么久",
        )
        return cooldown

    def _reconcile_if_due(self) -> ReconcileDecision:
        """这一轮该不该翻信箱，该翻就翻。返回决定，供本轮后续的日志措辞引用。

        判据在 `domain.reconcile_cooldown.decide_reconcile`；这里只负责问库要
        上次对账时刻、把决定说出来、并把它记在 `self._reconcile_decision` 上。

        ⚠️ **决定必须留下来。** `BotLoop._say_still_waiting` 要靠它区分「翻过
        信箱没找到」和「本轮压根没翻」——那两句话对用户的意思完全相反，而混着
        说正是这次故障拖了两天没被发现的直接原因。
        """
        options = getattr(self, "_options", None)
        forced = bool(options is not None and options.force_reconcile)
        decision = decide_reconcile(
            last_reconciled_at_utc=self._last_reconciled_at(),
            now=datetime.now(UTC),
            forced=forced,
            cooldown=self._reconcile_cooldown(),
        )
        self._reconcile_decision = decision
        say(decision.note)
        if decision.sweep:
            self.reconcile_today()
        return decision

    def reconcile_today(self) -> None:
        """开工第一件事：**把今天的战报读进库**，读完再把「今天已经打了几发」更新掉。

        用户口径（2026-08-11）：「任务启动先去读战报……读完后，需要更新海盗攻击 /
        bot 攻击的数量，因为我可能暂停任务重启启动。」

        ## 一趟信箱办两件事

        进出信箱要复位画面、切地表、开面板、切「报告」标签、慢拖回顶，一趟约 20 秒；
        而读战报和数战报看的是同一批行、同一个倒序列表。分两趟就要把这 20 秒付两遍，
        所以这里只有一次 `_scan_mail_rows`：`visit` 开封入库，`observe` 数数。

        **两笔预算互不牵连**，判据写在 `_scan_mail_rows` 里：开封受 `MAIL_MAX_OPENS`
        与「读到库里已有的那一份」限制，数数只受 `RECONCILE_MAX_PAGES` 与「翻到昨天」
        限制。反过来写（开封停了整趟就停）会在换过库的那天把「今天打了 20 发」
        记成 8 发，而计数偏小正是会超额的那一侧。

        **数的是列表行，不是入库结果**：一封读不出来的战报仍然证明「这一发打出去过」。
        两件事分开，才不会让一次 OCR 失手同时丢掉战果和配额。

        ## 每次开工都做，不再是一天一次

        原先靠 `daily_reconciliations` 里那条按 UTC 日去重的记录，一天只对一次账。
        用户会暂停任务再重启，而重启之后「今日 X/32」必须接得上——一天一次意味着
        早上那次对账之后，库外发生的事（手动打的、进程崩在写库之前的）当天再也不会
        被数进来。现在每次开工都数一遍；而这一趟本来就要跑（战报得有人读），
        多出来的成本只是那几行窄 ROI 的 OCR。

        重复对账不会把数越描越小：`record_daily_reconciliation` 按 UTC 日**取大**，
        「今天至少有几份」这件事在一天之内只会往上走。

        ## 哪一侧是权威

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

        ## 做在链路开工处

        对账要看屏，而控制台自己不驱动游戏（它只跑网页与调度）。放在链路开工处，
        游戏窗口、会话、信箱导航全都是现成的。日界一律 **UTC 00:00**
        （`domain.scheduler.quota_day_start_utc`），因为游戏的每日配额就是这么切的。
        """
        repository, _run_id = self._ensure_run()
        now = datetime.now(UTC)
        day_start = quota_day_start_utc(now)
        say(f"开工：读回{self.REPORT_LABEL}，并数一遍 UTC {day_start:%Y-%m-%d} 打了几发")
        # **先问库要单子，再进信箱。** 这一句就是「由库驱动」那条口径的落点：
        # 带着「哪几发理论上已经该有战报了」去找，而不是翻到什么算什么。
        outstanding = self._due_dispatches(now)
        if outstanding:
            say(f"  库里有 {len(outstanding)} 发到点还没战报：{_targets_note(outstanding)}")
        else:
            say("  库里没有到点还没战报的派遣；这一趟只补没入库的和数今天的份数")
        try:
            tally = self._scan_for_reconcile(day_start, now=now)
        except RoundExhausted:
            raise
        except RuntimeError as error:
            if not outstanding:
                # 单子为空时翻不了信箱**确实不该把这一轮判死**：它只是让配额判据
                # 退回按库计数，也就是今天没修正的那个状态——不比不做对账更糟。
                # 不写记录，下一轮再试。
                say(f"  开工翻不了信箱（{error}）；单子上没有欠账，这一轮先按库内计数走")
                return
            # 单子非空是**完全不同的一件事**：那几发的 6 小时钟正在走，
            # 而「下一轮再试」连撞两次同一堵墙就是永久丢数据（见 `MailboxUnreachable`）。
            # 升级一级：走 `SessionKeeper` 那条既有的关窗重开（配额 3 次 / 滚动
            # 1 小时，与 `_require_system_view` 共用，不另起一套），然后**再翻一次**。
            say(
                f"  开工翻不了信箱（{error}），而单子上还有 {len(outstanding)} 发到点没战报"
                f"（{_targets_note(outstanding)}）；关窗重开一次再翻（兜底策略）"
            )
            tally = self._retry_mailbox_after_restart(error, outstanding, day_start, now=now)
        status = repository.record_daily_reconciliation(
            self.TARGET_KIND,
            day_utc=day_start,
            observed_reports=tally.observed,
            complete=tally.complete,
            reconciled_at_utc=now,
        )
        note = "翻到底了" if tally.complete else "没翻到底，这是「至少」"
        say(f"  今天已有 {tally.observed} 份（{note}）")
        # 当天状态已经固化进 `daily_reconciliations`，重启之后一行就能读回
        # （用户口径 2026-08-11：「每天的海盗次数（状态）也可以存库，快速回读」）。
        if status is not None:
            say(
                f"  今日已用 {status.attacks_used} 发"
                f"（库内 {status.dispatched_count} · 信箱 {status.observed_reports}），"
                f"还有 {status.awaiting_reports} 发在等战报"
            )

    def _scan_for_reconcile(self, day_start: datetime, *, now: datetime) -> DailyTally:
        """开工那一趟信箱。返回这一趟数出来的当日份数。

        单独成一个方法只为一件事：**重试要用一份干净的账**。`DailyTally` 是边翻
        边累加的，失败那一趟已经数进去几行了；拿同一个对象再翻一遍，重叠的行会
        被数两遍。多数的方向虽然安全（只会让助手提前收手），但库里那个数会变成
        一个没人能解释的值，而它正是「今日 X/32」显示的东西。
        """
        tally = DailyTally(kind=self.RECONCILE_KIND, day_start=day_start)

        def visit(row: MailRow, page: Any) -> bool:
            if row.kind is ReportKind.PLANET_SCOUTED:
                self._ingest_planet_scout_alert(row, page)
                return False
            # Keep the existing early-stop invariant for already persisted
            # battle reports: alert handling must not turn every task start
            # into a run of eight redundant detail reads.
            return self._ingest_report_row(row, page)

        self._scan_mail_rows(
            wanted=(self.RECONCILE_KIND, ReportKind.PLANET_SCOUTED),
            label=f"{self.REPORT_LABEL}或安全告警",
            visit=visit,
            not_before=self._report_floor(day_start, now=now),
            max_pages=RECONCILE_MAX_PAGES,
            observe=tally,
        )
        return tally

    def _retry_mailbox_after_restart(
        self,
        error: Exception,
        outstanding: Sequence[Any],
        day_start: datetime,
        *,
        now: datetime,
    ) -> DailyTally:
        """关窗重开一次，再翻一趟信箱。还是翻不了就抛 `MailboxUnreachable`。

        - **只重开一次。** 与 `_require_system_view` 同一条理由：做成循环的话，
          服务端维护期间会变成「关一次 Chrome、开一次、再关」一直折腾到有人来看。
        - **配额与那条共用**（`SessionKeeper._restart_now` 的 3 次 / 滚动 1 小时）。
          配额用完时 `restart_and_reenter` 直接返回拒绝结局，这里照旧抛。
        - **重开之后不假定自己在游戏内**：`restart_and_reenter` 仍然走判据驱动的
          入口序列，`_enter_mailbox` 也照旧先复位画面再认地表。认不出就停，不乱点。

        抛出去之后由 `run()` 收进 `Outcome.failed`，退出码 1。**这是本次修复的
        另一半**：光有重试还不够，重试也失败时这一轮必须以可见的方式收场，
        而不是把那张受害名单打印完就照常跑目标循环。
        """
        outcome = self._keeper().restart_and_reenter(f"开工翻不了信箱：{error}")
        if not outcome.ready:
            raise MailboxUnreachable(
                f"开工翻不了信箱（{error}）；重开也没能回到游戏内（{outcome.detail}）；"
                f"单子上 {len(outstanding)} 发到点没战报（{_targets_note(outstanding)}）"
            )
        # 重开之后画面整个换过一遍，导航器与出发星球那两份记忆记的都是重开前的。
        self._navigator.invalidate()
        self._current_planet = None
        try:
            return self._scan_for_reconcile(day_start, now=now)
        except RoundExhausted:
            raise
        except RuntimeError as again:
            raise MailboxUnreachable(
                f"开工翻不了信箱：重开之后仍然翻不了（{again}）；"
                f"单子上 {len(outstanding)} 发到点没战报（{_targets_note(outstanding)}）"
            ) from again

    def _report_floor(self, day_start: datetime, *, now: datetime) -> datetime:
        """这一趟最早翻到哪一行为止。默认就是今天的 UTC 日界。

        日界之外还要多翻一段的情况只有一种：**跨过 UTC 午夜还在等的那一发**。
        它的战报写着昨天的时间，翻到日界就停的话永远读不到，那一发要一直挂到
        `MAX_REPORT_AGE`（6 小时）才被判缺失——bot 那边还要连带把目标退回去重打一遍。

        所以下界取「今天的日界」与「最早那发还在等战报的攻击派于何时」的更早者。
        问库而不是无条件往回翻 6 小时：没有在等的派遣时（绝大多数时候）下界就是
        日界，一行都不多翻；真有在等的才多付那几屏。
        """
        oldest = self._oldest_open_attack(now)
        if oldest is None or oldest >= day_start:
            return day_start
        say(f"  还有 {oldest:%m-%d %H:%M} UTC 派出的一发在等战报；往回多翻到那里")
        return oldest

    def _oldest_open_attack(self, now: datetime) -> datetime | None:
        from evo_helper.domain.report_wait import MAX_REPORT_AGE

        repository, _run_id = self._ensure_run()
        return repository.oldest_open_attack_at(
            self.TARGET_KIND, now_utc=now, max_age=MAX_REPORT_AGE
        )

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
        - **正**：右上角信箱旁边的未读数读得出数字。

        为什么不用星球名：那行「奥格瑞玛」是描边橙字压在金属牌上，
        实测 `chi_sim+eng` 读成 `“Rian`——拿读不准的东西当判据等于换个地方失败。

        两面都要，是为了挡住浮层：信箱面板、派遣面板、飞行中列表也读不到坐标行，
        但它们会盖住右上角那个未读数。只看「没有坐标行」会把浮层当成地表，
        然后在浮层上照地表的坐标点下去——这就是本轮点到「取消任务」的那个错。

        ⚠️ **这里只问「读不读得出数字」，一次都不用那个数。** 未读数是多少与
        「我在不在地表」无关，所以偶尔把面板描边一起读成 `4160` 是无害的；
        读成空才是致命的——2026-08-12 那夜 21 份战报就是这么丢的（信箱按钮明明
        就在画面右上角，`_enter_mailbox` 却报「切不到自己星球地表」）。
        新加的调用方要用这个数值的话，先回来重读这一段：判据是按「非空即可」
        选的，值本身没有任何一条测试守着。
        """
        from evo_helper.game.system_navigator import on_system_view

        if on_system_view(self._nav_labels()):
            return False
        return self.mail_badge_text() != ""

    def mail_badge_text(self) -> str:
        """未读数那一块的读数；一套配方都读不出来就交空串。

        逐个放大倍数试到**第一个读出纯数字的**为止，理由与
        `_fleet_origin_text` / `_planet_rows` 同形：一套读不出是粘连，不是画面
        不对，在同一张截图上换配方比换一次画面便宜得多。

        ⚠️ **必须是「整串都是数字」而不是「非空」。** 数字白名单
        （`COORD_WHITELIST`）里还有冒号——它是给坐标行 `2:137:18` 用的。
        只判非空时，别的画面上的纹理噪声会读成 `':'` 或 `'7 :'`，于是浮层被判成
        地表。实测（放宽到含 `nearest` 的配方表时）173 张负面样本里有 9 张这样。

        ⚠️ 诚实说一句：**收成现在这套 lanczos + 二值化之后，195 张实拍里一处
        非数字读数都不再出现**，也就是说这道判据此刻是纯冗余。留着是因为它便宜、
        且方向单一（只会把「不确定」推向安全的那一侧）；它守的**行为**由
        `tests/unit/tools/test_pirate_loop_mailbox_entry.py` 钉着，而不是靠像素。
        """
        for upscale in MAIL_BADGE_UPSCALES:
            text = self._read_mail_badge(upscale)
            if text.isdigit():
                return text
        return ""

    def _read_mail_badge(self, upscale: int) -> str:
        return self._read(
            MAIL_BADGE_ROI, digits=True, upscale=upscale, threshold=MAIL_BADGE_THRESHOLD
        ).strip()

    def _say_mail_badge_reads(self) -> None:
        """把每一套配方的原始读数打出来。**失败时才调。**

        只说一句「ROI 读到 ''」复盘不了任何事——2026-08-12 那两条日志就是这样，
        事后要重新跑一遍 OCR 才知道是哪一套读空了、读到的又是什么。
        """
        reads = ", ".join(
            f"{upscale}x={self._read_mail_badge(upscale)!r}" for upscale in MAIL_BADGE_UPSCALES
        )
        say(f"  信箱未读数 ROI{MAIL_BADGE_ROI}（阈 {MAIL_BADGE_THRESHOLD}）逐套读到：{reads}")

    def _goto_planet_surface(self, *, attempts: int = 3) -> bool:
        """从恒星系视图切回自己星球地表。切不过去返回 False。

        走**视图菜单**：星球按钮 → 子菜单第二项（带环行星）。子菜单只列出你现在
        不在的那些视图，所以这同一个像素在地表上是「回恒星系」、在恒星系里是
        「去地表」——`ensure_system_view` 用的就是它，方向相反而已。

        ⚠️ **不要走底部导航的「行星」**（用户 2026-08-09 明确指出）。那个开出来的是
        行星列表浮层，每颗星球一行、每行八个图标全是真实操作（运输/部署/传送/转移/
        投送/保护/扩张），而且「前往此处」的位置随行走——在那上面找坐标既没必要又危险。

        ⚠️ 这句只管「回地表」。**切换出发星球**走的正是那个浮层，见
        `ensure_origin_planet` 与 `game.planet_list`：那边一屏一屏认坐标、
        只点「前往此处」那一列，代价换来的是唯一一条换星球的路。
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

        ⚠️ **这里不再清导航缓存**（用户 2026-08-11：「海盗侦查不用每次都修改 3 个
        坐标，降低效率」）。原先每派出一发就清一次，于是同一恒星系里每颗星球都要
        重设三个字段——一次字段输入约 3 秒，每颗星球白花 6 秒。

        不清的依据是**缓存里只放回读确认过的坐标**（见 `SystemNavigator` 的类注释）：
        那份记忆来自派遣之前面板坐标行的一次核对，而关掉一层浮层并不改导航栏的值。
        真改了值的动作仍旧照清：`_require_system_view` 一旦需要切视图，
        `ensure_system_view` 自己就 `invalidate()` 了。
        万一记忆终究不作数，下一个目标的回读会当场核不过，走 `_goto_checked` 自愈。
        """
        self._driver.click(*MAIL_BACK, label="关闭面板")
        self._driver.wait(2.2)
        self._require_system_view("派出之后切不回恒星系视图")

    def _close_mail(self) -> None:
        """点邮箱列表左上角 X，再回到恒星系视图。

        ⚠️ 这里同样不再显式清导航缓存：进信箱要先切到地表视图，而
        `_goto_planet_surface` 与回来时的 `ensure_system_view` **换过视图就已经清了**。
        再补一次是空动作，理由见 `_leave_dispatch_list`。
        """
        self._driver.click(*MAIL_LIST_CLOSE, label="关闭邮箱列表（左上角X）")
        self._driver.wait(2.0)
        if self._on_mail_list():
            # 还在列表上说明刚才那一下退的是详情页，再退一层才关掉信箱。
            self._driver.click(*MAIL_LIST_CLOSE, label="关闭邮箱列表（左上角X）")
            self._driver.wait(2.0)
        self._require_system_view("读完邮件切不回恒星系视图")

    # -- 持久化 -------------------------------------------------------------

    def _ensure_run(self) -> tuple[SqlAlchemyRepository, UUID]:
        if self._repository is not None and self._run_id is not None:
            return self._repository, self._run_id
        session_factory = create_session_factory(create_database_engine(Settings().database_url))
        self._session_factory = session_factory
        self._repository = SqlAlchemyRepository(session_factory)
        self._run_id = _ensure_run_row(session_factory)
        return self._repository, self._run_id

    def _ensure_session_factory(self) -> Any:
        """这条链路自己那套连接。战报截图不走 `SqlAlchemyRepository`，见
        `storage.report_screenshots` 的模块头（旁路数据不该并进攻击链路的账本）。
        """
        self._ensure_run()
        return self._session_factory

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
                origin=self._options.origin or origin(),
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

        **恢复阶梯，逐级加码，每一级只在上一级失败之后才走**（`run()` 开工前置
        的第二级；第一级是 `ensure_game_window`，第三、四级是
        `_reset_to_known_screen` 与 `_require_system_view`）：

        1. 巡检一次。会话好好的就到此为止，一下都不点。
        2. 读到 `UNKNOWN` → 关浮层再问一次。**最坏情况下点到了什么：**
           左上角 (750, 71) 那个 ✕；各种浮层的关闭键都在同一处，而那个位置在
           恒星系视图上什么都不是，点空无害。
        3. 还是 `UNKNOWN` → **先当成「登录还没走完」等一会儿**（上限
           `LOGIN_SETTLE_TIMEOUT_S`）。**最坏情况下点到了什么：什么都没点。**
           2026-08-17 登录流程更新之后，翻页那几秒读出来的画面跟真认不出长得
           一样，而正解是等，不是往下走——整段见
           `scan_coordinates.wait_for_login_if_unrecognised`。
        4. 还是不行 → 关窗重开一次。**最坏情况下点到了什么：什么都没点。**
           重开只往游戏窗口那个句柄送一个 `WM_CLOSE`（等同用户点右上角 ×），
           再由 `ensure_game_window` 拉一个新的；新窗口停在入口页，之后仍旧走
           判据驱动的入口序列，认不出就停。所以「认不出的画面绝不点击」在这一级
           一样成立——它恰恰是**唯一一级完全不在认不出的画面上动手**的。
           配额是 3 次 / 滚动 1 小时（`SessionKeeper._restart_now`），用尽就返回
           拒绝结局，这里照旧抛出去。
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
        # 关完浮层仍然认不出 → 还有一种解释：**登录还没走完**（2026-08-17 起）。
        # 这一级只等不点，而且必须排在下面的关窗重开之前——登录才到一半就把
        # Chrome 关掉，救不了，还会把本来马上就好的会话亲手弄坏。
        session = wait_for_login_if_unrecognised(session, self._keeper())
        if session is None or not session.ready:
            # 阶梯最后一级。走到这里说明关浮层也没用：画面既不是入口序列里的
            # 任何一屏，也读不出导航条——上一轮多半没能正常收尾（进程被强杀、
            # 断电、用户点了任务管理器），而那是常态不是意外。
            detail = session.detail if session else "巡检没返回结果"
            say(f"  会话不可用：{detail}；关窗重开一次再试（兜底策略）")
            session = self._keeper().restart_and_reenter(f"会话不可用：{detail}")
            if not session.ready:
                raise SessionUnavailable(
                    f"会话不可用：{detail}；重开也没能回到游戏内（{session.detail}）；安全停止",
                    recoverable=session.restarts_left > 0,
                )
        if session.reconnected:
            say("已重新登录")
            self._navigator.invalidate()
            # ⚠️ **出发星球那份记忆也要一起清**，理由与清导航缓存**一模一样**：
            # 它记的是重连之前的画面。重连的最后一级是关窗重开 Chrome，游戏重新
            # 走一遍入口序列；这之后当前星球是哪一颗，本仓无从得知。
            #
            # 不清的后果恰恰是 #109 这个功能存在的理由：`switch_needed` 看到
            # 「本轮已经切到 9:250:8」于是不再切，而画面可能已经回到主星——
            # 本轮余下每一发都从主星飞出去，`attack_intents.origin_*` 上却写着
            # 9:250:8，战报永远配不上。**而且一声不响。**
            #
            # 清掉的代价是下一次 `ensure_origin_planet` 多切一次；即便游戏其实
            # 保住了当前星球，那一次也只是点自己那一行、回到自己的地表，无害。
            # 两侧代价差着一整轮的错账，所以宁可多切。
            self._current_planet = None
            return True
        return False

    def _require_system_view(self, what_failed: str) -> None:
        """必须停在恒星系视图，否则关窗重开一次再试；仍然不行才抛。

        **用户口径（2026-08-11）：「切不回就重启，这是兜底策略。」**

        原来这三处（开工、派出之后、读完邮件之后）都是就地 `raise`，整轮停摆、
        退出码 1——而画面上一个「掉线」字样都没有，`SessionKeeper.reconnect` 那条
        重连路根本不会被触发：它的判据是读到「连接已断开」/「无法重新连接」。
        实机 2026-08-11 就是这么倒在「读完邮件切不回恒星系视图」上的。

        - **只重开一次。** 重开完再切不回来就老实抛出去。做成循环的话，服务端
          维护期间会变成「关一次 Chrome、开一次、再关」一直折腾到有人来看。
        - **配额与死会话那条共用**（`SessionKeeper._restart_now` 里的滚动窗口）。
          配额用完时 `restart_and_reenter` 直接返回拒绝结局，这里照样抛。
        - **重开之后不假定自己在游戏内**：`restart_and_reenter` 仍然走判据驱动的
          入口序列，`ensure_system_view` 也照旧读导航栏标签。认不出就停，不乱点。
        """
        if self._navigator.ensure_system_view(self._nav_labels):
            return
        say(f"  {what_failed}；关窗重开一次再试（兜底策略）")
        outcome = self._keeper().restart_and_reenter(what_failed)
        if not outcome.ready:
            raise SessionUnavailable(
                f"{what_failed}；重开也没能回到游戏内（{outcome.detail}）；安全停止",
                recoverable=outcome.restarts_left > 0,
            )
        # 重开之后画面整个换过一遍，导航器那份记忆记的是重开前的坐标。
        self._navigator.invalidate()
        if not self._navigator.ensure_system_view(self._nav_labels):
            raise SessionUnavailable(
                f"{what_failed}；重开之后仍然切不回来；安全停止",
                recoverable=outcome.restarts_left > 0,
            )

    # -- 出发星球 -----------------------------------------------------------

    def planet_switcher(self, *, dry_run: bool = False) -> PlanetSwitcher:
        """建一个切换器。拖动接 `slow_drag`，理由见 `game.planet_list` 的模块头。"""
        return PlanetSwitcher(
            driver=_PlanetListDriver(self._driver),
            read_rows=self._planet_rows,
            read_origin=self._fleet_origin_text,
            say=say,
            record_evidence=self._record_planet_list_overlay,
            dry_run=dry_run,
        )

    def _record_planet_list_overlay(self, message: str, payload: dict[str, Any]) -> None:
        """`PlanetSwitcher` 走到「关浮层重读」那一支时的落地口。

        截图能力是可选的：轻量驱动（尤其单元测试桩）只实现点击和等待，那时
        照样把文字证据写进库——**诊断路径不许因为配图失败而整条丢掉**。
        """
        capture = getattr(self._driver, "capture", None)
        record_planet_list_overlay_retry(
            message, payload, capture=capture if callable(capture) else None
        )

    def ensure_origin_planet(self) -> bool:
        """**开工阶段**把当前星球切到这一轮配的那颗；切不成返回 False。

        ⚠️ **一轮只切一次。** 判「要不要切」的是纯函数
        `domain.planet_switch.switch_needed`，记「已经切到哪」的是
        `self._current_planet`——而那份记忆只在**回读确认之后**才写下去，
        与 `SystemNavigator.current` 是同一条规矩：打过的字不算数，读回来的才算。

        放在这里而不是每个目标前面：出发星球在一轮之内不会变，而一次切换是
        「开浮层 + 可能几次拖动 + 回读」，挂在每个目标上等于每颗星球白花十几秒。

        切完还要把画面拨回恒星系视图——切换会把游戏丢到新星球的地表上，
        而 `_sweep` 的第一件事就是照恒星系视图的坐标导航。
        """
        target = self._options.origin or origin()
        if not switch_needed(target, self._current_planet):
            return True
        say(f"出发星球：切到 {target}")
        # ``NAV_PLANET`` 是**地表**底栏的「行星」按钮；在恒星系视图同一个
        # 像素是别的入口，点下去不会出现坐标列表，OCR 只能安全地读成空。读完
        # 邮箱后流程刻意回到恒星系视图，因此这里必须先回地表，再打开列表。
        if not self._goto_planet_surface():
            self._outcome.busy = "切出发星球前回不到星球地表"
            say(f"  {self._outcome.busy}；这一轮一发都不派")
            return False
        result = self.planet_switcher().switch_to(target)
        if result is not SwitchResult.SWITCHED:
            self._outcome.busy = f"切不到出发星球 {target}（{result.value}）"
            # `SwitchResult` 早就把这两种分开了（「两句话对用户的意思完全不同」），
            # 这里必须把那个区分**带出进程**：翻遍列表都没有 = 配错了坐标，不会
            # 自己好，得让连续失败计数看见它。见 `Outcome.busy_is_permanent`。
            self._outcome.busy_is_permanent = result is SwitchResult.NOT_FOUND
            say(f"  {self._outcome.busy}；这一轮一发都不派")
            if self._outcome.busy_is_permanent:
                say(f"  这颗星球不在你的行星列表里；请核对任务配的出发星球 {target}")
            return False
        self._current_planet = target
        # 浮层与派遣面板都开过，导航栏里是什么已经不可知了。
        self._navigator.invalidate()
        self._require_system_view("切换出发星球之后切不回恒星系视图")
        return True

    # -- 主循环 -------------------------------------------------------------

    def run(self) -> Outcome:
        # 几何先校一遍。窗口被改过尺寸时所有坐标一起失效，而这件事悄无声息——
        # 本轮开工时窗口就是 1536×733，照 1920×917 的坐标点下去全落在别处。
        from evo_helper.game.game_window import ensure_game_window

        ensure_game_window()

        try:
            # ⚠️ **开工那三步也要在 try 里面。** 它们正是环境故障最常倒下的地方
            # （会话回不来、切不到恒星系视图），而落在 try 外面就等于让
            # `SessionUnavailable` 抛穿 `main()`、按 Python 默认的退出码 1 收场——
            # 也就是这次修的那个毛病本身。
            self._ensure_session(force=True)
            self._reset_to_known_screen()
            self._require_system_view("开工时切不到恒星系视图")
            # ⚠️ **显式要求读信箱时，必须排在切星球前面。**
            # 信箱是账号级的，读它跟站在哪颗星球上毫无关系；而切星球是开工阶段
            # 最容易失手的一步（要认坐标、要拖列表、要回读）。
            #
            # 反过来排（落地时如此）的代价：切不过去就直接 return，于是**这一轮
            # 一份战报都不入库**。而「预设舰队攻击后部分战报缺失、回读机制没入库」
            # 正是用户 2026-08-13 报的那个毛病——一个防记账错乱的功能，反过来
            # 成了战报缺失的新来源。切换失手只该挡住派遣，不该连带挡掉读战报。
            # 调度器会在一条航线返航后再次拉起 runner。这里若无条件进信箱，
            # 就会把「等舰队回来继续派」误做成「每次续跑都翻一遍战报」；而反过来
            # 无条件不进信箱，就是 2026-08-15 起那两天——攻击照派、战报一份没读。
            # 判据是**冷却**，理由整段在 `domain.reconcile_cooldown`。
            self._reconcile_if_due()
            if not self.ensure_origin_planet():
                # 切不过去/回读不过时**一发都不派**：舰队会从别的星球飞出去，而
                # `attack_intents.origin_*` 上写着这一轮配的那颗，战报永远配不上。
                # 走到这里战报已经读完入库了，这一轮不算白跑。
                return self._outcome
            self._sweep()
        except MailboxUnreachable as unreachable:
            # ⚠️ **这一档必须排在 `RoundExhausted` 前面**（它也是 `RuntimeError`
            # 的子类，但两者的收场完全相反），而且**这一轮就此打住、不跑目标循环**。
            #
            # 2026-08-12 那夜最刺眼的正是日志顺序：先把 10 发（下一轮 15 发）
            # 一个不落地打印出来，下一行就放弃了它们，然后照常把 386 个目标走了
            # 一遍。那一趟目标循环没有任何意义——库里的态全靠战报推进，战报一份
            # 都没读进来，每个目标只会重复上一轮的判断。
            say(f"这一轮判为失败：{unreachable}")
            self._outcome.failed = str(unreachable)
        except SessionUnavailable as unavailable:
            # 环境故障：走 `Outcome.busy` 那一档，而不是抛穿 `main()` 退 1。
            # 「还有救吗」由异常自己带着（判据是关窗重开配额），这里只负责翻译成
            # `busy_is_permanent`——两个字段合起来正好喂给 `exit_code_for`。
            say(f"这一轮开不了工：{unavailable}")
            self._outcome.busy = str(unavailable)
            self._outcome.busy_is_permanent = not unavailable.recoverable
        except RoundExhausted as exhausted:
            # 资源耗尽**不是失败**：正常收尾、退出码 0。当成失败的话，航线占满
            # （必然会发生）连撞三次就把整条链路自动停用了，而它只是需要等舰队
            # 飞回来。调度器看到 0 就只走冷却，到点再来。
            say(f"这一轮到此为止：{exhausted}")
        return self._outcome

    # -- 当日去重 -----------------------------------------------------------

    def _daily_progress(self, *, refresh: bool = False) -> dict[Coordinate, PirateProgress]:
        """今天（游戏内 UTC 日）每个海盗目标走到哪一步了，按目标坐标查。

        日界走 `domain.scheduler.quota_day_start_utc`，**不许自己写
        `replace(hour=0)`**——那个函数的注释写了为什么（落在本地时刻上就悄悄
        变成本地日历天，两者只在一天里的某几个钟头对得上）。

        `scout_not_before` 也传当日起点：昨天那份侦察报告说的是昨天那批舰队，
        拿它判「今天该不该打」就是照着过期情报派舰队（理由整段在
        `repository.pirate_progress`）。控制台那一侧不传，两者口径本来就不同。
        """
        if refresh or self._daily is None:
            repository, _run_id = self._ensure_run()
            day_start = quota_day_start_utc(datetime.now(UTC))
            self._daily = {
                row.target: row
                for row in repository.pirate_progress(since=day_start, scout_not_before=day_start)
            }
        return self._daily

    def _action_for(self, coordinate: Coordinate) -> PirateAction:
        """这一趟该对这个坐标做什么。判据整段在 `domain.pirate_round.action_for`。

        ⚠️ **这就是那条去重的落点。** 判据（七态 → 动作）2026-08-11 就写好了，
        但只有控制台在读它；`_find_pirates` / `_decide_and_attack` 一次都没问过，
        于是每一轮都当作今天什么都没做过。2026-08-13 通宵的账：侦察 111 发打在
        54 个坐标上（2:137:1~4 各 5 发），攻击只有 12 发。

        今天完全没动过的坐标库里根本没有行，那就是 `NEEDS_SCOUT`——写成显式的
        默认值而不是让 `dict.get` 返回 None 再各处判空。
        """
        row = self._daily_progress().get(coordinate)
        if row is None:
            return action_for(PiratePhase.NEEDS_SCOUT, scout_count=0)
        return action_for(row.phase, scout_count=row.scout_count)

    def _phase_note(self, coordinate: Coordinate) -> str:
        """日志里那个态怎么念。查不到行就是今天还没动过。"""
        row = self._daily_progress().get(coordinate)
        return PHASE_LABELS[PiratePhase.NEEDS_SCOUT if row is None else row.phase]

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
            # ⚠️ **读完信箱必须重取一次当日进度。** 刚才那一趟把新的侦察报告写进了
            # `scout_reports`，而缓存里那份还是进信箱之前的：不重取的话，本轮刚回来
            # 的报告要等到下一轮才被看见，「待侦察报告」会一直挂着，攻击永远慢一拍。
            self._daily_progress(refresh=True)
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

        ⚠️ **侦察派不派要先问今天的账**（`_action_for`）。原先这里是无条件派：
        只要认出是海盗就发一发，于是同样四个坐标每一轮各挨一发。
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
            self._outcome.pirates.append(coordinate)
            action = self._action_for(coordinate)
            if action is PirateAction.DONE:
                # 今天这个坐标已经有结论了。**连 `pirates` 都不进**：进了的话
                # `_sweep` 还要为它翻一趟信箱、再走一次判定，而结论不会变。
                say(f"    今天已经{self._phase_note(coordinate)}；这一天不再碰它")
                continue
            pirates.append(coordinate)
            if action is not PirateAction.SCOUT:
                say(f"    今天已侦察过（{self._phase_note(coordinate)}）；不重复侦察")
                continue
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
        """按今天的账决定这一趟对它做什么。`reading` 是刚翻信箱读到的那份（可能没有）。

        ⚠️ **库先于 `reading`。** 今天那份侦察报告只要已经落库，判定就从库里来——
        `reading` 只是同一份报告的另一条路径，而库那条还额外知道「今天已经打过了」。
        用户口径（2026-08-13）：今天攻击过的坐标不侦查也不攻击；今天侦查过的直接
        用今天那份报告的结论。

        `PirateAction.ATTACK` 那一档是**直接打，不重新侦察**：走到这一档说明今天
        那份报告已经判为「打」，再派一发侦察只是把配额烧掉一发再得出同一个结论。
        """
        from evo_helper.vision.scout_reports import VERDICT_ATTACK

        action = self._action_for(coordinate)
        if action is PirateAction.ATTACK:
            say(f"  {coordinate} 今天那份侦察报告判为「打」；直接攻击，不重新侦察")
            self._attack_checked(coordinate)
            return
        if action is not PirateAction.SCOUT:
            # `DONE`（今天已有结论）与 `WAIT`（侦察发/攻击发还在路上）在这里的
            # 处置一样：不打。⚠️ **`WAIT` 尤其不能漏。** `AWAITING_ATTACK_REPORT`
            # 也是 `WAIT`，漏掉它就会对同一个坐标再打一发——刚才那一发的战报
            # 还没回来，谁都不知道已经打过了，配额一次烧两份。
            say(f"  {coordinate} 今天已经{self._phase_note(coordinate)}；这一趟不打")
            return
        if reading is None:
            # 走到这里是「今天库里没有这个坐标的任何派遣」——`--attack` 不给
            # `--scout` 时的常态，判定只能来自刚翻到的那份报告。
            say(f"  {coordinate} 读不到侦察报告；跳过")
            self._outcome.refused.append((coordinate, "读不到侦察报告"))
            return
        say(f"  {coordinate} 判定 {reading.verdict}：{reading.trigger_ships}")
        if reading.verdict != VERDICT_ATTACK:
            return
        self._attack_checked(coordinate)

    def _attack_checked(self, coordinate: Coordinate) -> None:
        """核一遍面板再打。两条判定路径共用，别再各写一份。"""
        if self._goto_checked(coordinate) is not TargetCheck.CONFIRMED:
            self._outcome.refused.append((coordinate, "攻击前面板认不出"))
            return
        self.attack(coordinate)


def _targets_note(dispatches: Sequence[Any]) -> str:
    """把单子上那几发写成一行人话。日志里没有坐标就无从判断该不该往下翻。"""
    return "、".join(str(item.target) for item in dispatches)


def rematch_note(repository: Any, target: Coordinate, reported_at: datetime) -> str:
    """撞见一封「库里已有」时，顺手把那一行**重新认领一次**，并交回一句话。

    这是 2026-08-11 那四发 AAA 的出口：战报早就读进库了，只是当初认领判据把
    自己那一发侦察也当成了候选，于是记 `AMBIGUOUS`、`dispatch_id` 留空
    （来龙去脉写在 `repository._unmatched_dispatch_candidates`）。判据修好之后，
    已经在库里的那些行**不会自己接上**——而 `has_report_at` 那道去重又保证了
    它们永远不会被重新读一遍。所以要在这里主动重认一次。

    不重开邮件、不重读像素，只是拿现在的判据把旧行重算一遍：一次本地写库。

    ## 这一句话必须说清「库里那一行是谁」

    ⚠️ 原先这条路只有两种输出：补认上了说一句，没补上就**什么都不说**。于是实机
    日志里只剩一句「这份战报（17/08/2026 09:05:46）已经在库里；不重复入库」，
    而攻击日志页上同一个坐标 4:480:6 还挂着「待战报」——用户看到的是两条自相
    矛盾的记录（2026-08-17 报障）。

    真相是**没有矛盾**：库里那一行 `match_status='UNMATCHED'`，它不属于页面上
    那一发；页面那一发派于 08-15 22:13，战报早在信箱停摆的那 44 小时里过期了。
    可这句话当时说不出口，因为日志既没说库里那一行是哪一条、也没说它认没认上
    派遣——「跳过入库」听上去就像「跳过了一次认领机会」。**日志少说一句，
    故障就得连生产库才查得清。**

    所以现在三档都要说出来，并把结构化证据落进 `system_log`：

    - 补认上了：说补上了、认的是哪一发（派出时刻）。
    - 本来就认领着：说它认的是哪一发——这一句才排除了「跳过害得它没认上」。
    - 至今没认领：**明说 `UNMATCHED`**，并点破它不会出现在攻击日志的战果列上。

    **不限流。** 这条路一份战报每趟最多走一次（撞见「库里已有」之后 `_ingest_report`
    就返回了），不是每 tick 都可能触发的那一类。
    """
    before = _report_claims(repository, target, reported_at)
    rematched = bool(repository.rematch_report_at(target, reported_at))
    after = _report_claims(repository, target, reported_at) if rematched else before
    claimed = [claim for claim in after if claim.dispatch_id is not None]
    unclaimed = [claim for claim in after if claim.dispatch_id is None]
    record_system_log(
        "INFO",
        "tools.report_ingest",
        f"{target} 的战报（{reported_at:%Y-%m-%d %H:%M:%S} UTC）库里已有，不重复入库",
        payload={
            "target": str(target),
            "reported_at_utc": reported_at.isoformat(),
            "rematched": rematched,
            "rows": [
                {
                    "report_id": str(claim.report_id),
                    "match_status": claim.match_status,
                    "dispatch_id": None if claim.dispatch_id is None else str(claim.dispatch_id),
                    "dispatched_at_utc": (
                        None
                        if claim.dispatched_at_utc is None
                        else claim.dispatched_at_utc.isoformat()
                    ),
                }
                for claim in after
            ],
            # 认领与否是**跳过入库之前就定下的**，这一次跳过并没有改变它。
            # 带上「之前」那一份，是为了让「跳过害得它没认上」这个猜想当场被排除。
            "claimed_before_skip": sum(1 for claim in before if claim.dispatch_id is not None),
        },
    )
    if not after:
        # 查不到行：只可能是那份战报刚被别的进程删了、或者仓库对象是个不认得
        # `report_claims_at` 的替身。不猜，照实说。
        return ""
    if rematched:
        return f"；这一份原先没认上派遣，刚补认上了（{_claim_note(claimed)}）"
    if claimed:
        return f"；库里那一份认的是{_claim_note(claimed)}"
    statuses = "/".join(sorted({claim.match_status or "?" for claim in unclaimed}))
    return f"；⚠️ 库里那一份至今没认领任何派遣（{statuses}），它的战果不会出现在攻击日志上"


def _report_claims(repository: Any, target: Coordinate, reported_at: datetime) -> tuple[Any, ...]:
    """库里那几行战报的认领状态；仓库替身不认得这个方法时退回空元组。

    ⚠️ **不许让它把这一趟弄死。** 这是一条纯诊断查询，而调用它的地方正夹在
    「读完战报」与「决定还要不要往下开封」之间——一个 `AttributeError` 漏出去
    就是把「战报读不回来」那个故障重新造一遍，只是换了个成因
    （先例见 `_store_report_screenshot`）。
    """
    reader = getattr(repository, "report_claims_at", None)
    if reader is None:
        return ()
    try:
        return tuple(reader(target, reported_at))
    except Exception as error:  # noqa: BLE001 - 见 docstring：诊断路径不许拖累主路径
        say(f"  查不到库里那一份战报的认领状态（{error}）；不影响判据")
        return ()


def _claim_note(claims: Sequence[Any]) -> str:
    """把认领到的那几发写成人话：排障要对的是**派出时刻**，不是 UUID。"""
    if not claims:
        return "某一发派遣"
    return "、".join(
        "派出时刻未知"
        if claim.dispatched_at_utc is None
        else f"{claim.dispatched_at_utc:%m-%d %H:%M:%S} UTC 派出的那一发"
        for claim in claims
    )


def _coordinate_order(coordinate: Coordinate) -> tuple[int, int, int]:
    return (coordinate.galaxy, coordinate.system, coordinate.position)


class StepTimer:
    """给一次派遣的各步计时，收工打一行。**只观测，一个行为都不改。**

    起因（用户 2026-08-14）：实机上每个目标约 45 秒，而日志只在**目标边界**打点，
    中间的导航、开面板、翻预设条、简报闸门一个时刻都没有——于是「45 秒花在哪」
    根本切不开，只能靠猜。这一层就是把猜换成量。

    ⚠️ **用 `time.monotonic()` 而不是墙钟。** 这几个数是拿来相减的，而墙钟会被
    NTP 校时往回拨，拨一次就能拿到负的耗时。`say()` 行首那个时刻是给人对事件的，
    两者用途不同，不要合并。

    收工那一行长这样，方便事后 grep 出来做分解表：

        [耗时] 2:112:19 攻击 共 46s（派出）：开面板 6s 翻预设条 21s 简报 15s 出发 4s

    **每条出路都要打这一行**，包括失败的那几条：昨夜 145 次「找不到预设」正是
    最贵的一档，只记成功的话，分解表上看到的会是一个被幸存者偏差洗过的数。
    """

    def __init__(self, what: str) -> None:
        self._what = what
        self._start = time.monotonic()
        self._mark = self._start
        self._laps: list[tuple[str, float]] = []

    def lap(self, name: str) -> None:
        now = time.monotonic()
        self._laps.append((name, now - self._mark))
        self._mark = now

    def say_total(self, outcome: str) -> None:
        total = time.monotonic() - self._start
        parts = " ".join(f"{name} {secs:.0f}s" for name, secs in self._laps)
        say(f"  [耗时] {self._what} 共 {total:.0f}s（{outcome}）：{parts}")


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


class _PlanetListDriver:
    """把 `LiveDriver` 包成 `game.planet_list` 要的那个操作面。

    只为一件事存在：**纵向拖动必须走 `slow_drag`**。`LiveDriver.drag` 是一步式的
    `dragTo`，游戏面板会把它当成点击（`slow_drag` 的注释里记着这条实测），而这里
    按下的那一点就在星球名那一行——被当成点击的那一下点在什么上面，取决于版面
    有没有微调。包一层比让切换器自己知道「实机要慢拖」干净。
    """

    def __init__(self, driver: LiveDriver) -> None:
        self._driver = driver

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self._driver.click(x, y, label=label)

    def drag_vertical(self, x: int, from_y: int, to_y: int, *, label: str = "") -> None:
        del label  # 慢拖是分步的，`HumanInput` 那条带标签的路径走不通。
        slow_drag(self._driver, from_y, to_y, x=x)

    def wait(self, seconds: float) -> None:
        self._driver.wait(seconds)


class _PresetPickerDriver:
    """预设条专用的分步横拖。

    ``LiveDriver.drag`` 是一步式 ``dragTo``；实机预设条会把它吞掉，始终停在
    ``AAA / 探路``。这里复用行星列表慢拖的原则，但横向范围只落在预设条的安全
    区（由 ``PresetPicker`` 的常量守住），不会触及右侧「保存」按钮。
    """

    def __init__(self, driver: LiveDriver) -> None:
        self._driver = driver

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self._driver.click(x, y, label=label)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = "") -> None:
        del label
        import random

        self._driver.focus()
        origin_x, origin_y = self._driver.origin()
        gui = self._driver._gui  # noqa: SLF001 - 分步拖动需要原始鼠标控制。
        gui.moveTo(origin_x + from_x, origin_y + from_y, random.uniform(0.2, 0.4))
        gui.mouseDown()
        time.sleep(random.uniform(0.10, 0.20))
        for index in range(1, 13):
            ratio = index / 12
            gui.moveTo(
                origin_x + int(from_x + (to_x - from_x) * ratio),
                origin_y + from_y + random.randint(-1, 1),
                random.uniform(0.02, 0.05),
            )
        time.sleep(random.uniform(0.12, 0.25))
        gui.mouseUp()

    def wait(self, seconds: float) -> None:
        self._driver.wait(seconds)


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


def exit_code_for(outcome: Outcome) -> int:
    """这一趟的退出码。两条链路共用（`tools.bot_loop.main` 也调它）。

    四档，按「会不会自己好」分：

    - **单子非空却翻不了信箱**（`Outcome.failed`，升级重启之后还是翻不了）→ `1`。
      排在最前面：这一档比「没派成」严重——那几发的 6 小时钟正在走，
      再丢一轮就永久判缺失。理由整段写在 `MailboxUnreachable`。
    - **没派成、但会自己好**（回读没认出来）→ `EXIT_ENVIRONMENT_BUSY`。
      调度器当成「这会儿轮不到我」，**不计入连续失败**
      （见 `application.mission_supervisor`）。按 1 收场的话，切换星球偶尔不成
      连撞三次就把整条链路停用了，而它只是需要下一轮再试一次。
    - **没派成、而且不会自己好**（列表里根本没这颗星球 = 配错了坐标）→ `1`。
      走正常的异常退出：连撞三次自动停用并报警，这正是我们要的——它自己不会好，
      得有人去改配置。豁免它等于让任务整夜显示「在跑」却一发不派。
    - 其余 → `0`。

    ⚠️ 别把这两档并回一档去。`EXIT_ENVIRONMENT_BUSY` 那一档的语义是
    「外部条件占着，放手就好」，塞进一个永久性故障会把整个自停机制在这条路径上
    架空，而且**停顿看门狗也接不住**：它抓的是「跑着却没进展」，
    而这种情形每轮 30 秒就干净利落地退了。
    """
    if outcome.failed:
        return 1
    if not outcome.busy:
        return 0
    return 1 if outcome.busy_is_permanent else EXIT_ENVIRONMENT_BUSY


def parse_origin(text: str) -> Coordinate:
    parts = text.split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError(
            f"出发星球要写成 银河:恒星系:行星，例如 2:137:18（收到 {text!r}）"
        )
    galaxy, system, position = (int(part) for part in parts)
    return Coordinate(galaxy, system, position)


def parse_system(text: str) -> tuple[int, int]:
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError(f"恒星系要写成 银河:恒星系，例如 2:137（收到 {text!r}）")
    return (int(parts[0]), int(parts[1]))


def main(argv: list[str] | None = None) -> int:
    # 日志出口。装不上就是空操作，`say()` 照常打到控制台。
    install_runner_system_log()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--systems", nargs="+", type=parse_system, required=True)
    parser.add_argument("--scout", action="store_true", help="真的派侦察出去")
    parser.add_argument(
        "--attack",
        action="store_true",
        help="判定为「打」时真的攻击。不配 --scout 时用信箱里已有的侦察报告",
    )
    parser.add_argument("--preset", default=pirate_ui.ATTACK_PRESET_NAME)
    parser.add_argument(
        "--origin",
        type=parse_origin,
        default=None,
        help="出发星球（记账用）。调度器会传；手工跑不给则用 EVO_HELPER_ORIGIN",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="强制翻一趟信箱读当日战报，忽略冷却（手工排障用；不给则按冷却自动决定）",
    )
    args = parser.parse_args(argv)

    import ctypes

    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    options = LoopOptions(
        systems=tuple(args.systems),
        scout=args.scout,
        attack=args.attack,
        preset=args.preset,
        origin=args.origin,
        force_reconcile=args.reconcile,
    )
    mode = "扫描" if not args.scout else ("侦察+攻击" if args.attack else "只侦察")
    listed = ", ".join(f"{galaxy}:{system}" for galaxy, system in options.systems)
    say(f"模式：{mode}；恒星系 {listed}")

    def go() -> int:
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
        return exit_code_for(outcome)

    return run_with_foreground_guard(go)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
