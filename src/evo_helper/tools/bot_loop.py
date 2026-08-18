"""bot 目标的「直接 BBB 攻击 → 读战报 → 收工」自动化。

    # 只看目标认不认得出，一次点击都不派（默认）
    python -m evo_helper.tools.bot_loop --targets 2:137:14

    # 真打：用预设 BBB 打一发，回来读战报
    python -m evo_helper.tools.bot_loop --targets 2:137:14 --attack

与海盗那条链路的区别只在**判定依据**：

- 海盗先派侦察，看侦察报告里几个特定舰种的数量（`vision.scout_reports`）决定打不打；
- bot **不做任何前置侦查**，直接用预设 BBB 打一发，战报回来就收工。战报里的胜负
  仍然按 `domain.battle_outcome` 算（剩余 = 单位 − 损失）并入库，但它只进日志与
  情报中心，**不再决定要不要补刀**——「平局就再打一次」已按用户口径
  （2026-08-17）移除。

所以导航、简报闸门、选预设、写 intent/dispatch 全部复用 `pirate_loop.PirateLoop`；
这里只换目标识别与判定。

## 为什么不再探路、不再分档（用户口径 2026-08-13）

> 基于第一条，bot攻击模式变更，不再进行攻击侦查，直接用预设BBB进行攻击，
> 如果同一坐标攻击结果为平局，则继续进行攻击

后半句已经作废——用户口径（2026-08-17）：「bot 攻击移除平局再打一次机制」。
现在**一个目标本轮就打一发**，战报回来就收工，平局也一样（`domain.bot_round`）。

「第一条」是战报缺失那件事。原先每个目标要**两发**才走得完（探路一发拿守方
单位数、按 `fleet_tier` 分档再打一发），也就是每个目标要等**两份**战报；而
2026-08-12 通宵的实测是：bot 攻击发 21 发、战报只认领上 6 份，其余全卡在
等战报。少一发就少一份要等的战报，这条链路的完成率直接跟着翻倍。

分档整套（`domain.fleet_tier`、`/tiers` 页、`scheduler_config` 上那三列、
`--tier-thresholds`）已随之删除，不是留成死配置。

## 一趟推一态，战报在开工那一趟信箱里收

三个态见 `domain.bot_round.phase_of`。等待态（`AWAITING_ATTACK_REPORT`）的出路是
**开工那一趟信箱**（父类 `reconcile_today`）：把认得出的攻击报告读出来写进
`battle_reports`，`phase_of` 才看得到 `has_report` 和那一发的战果。

这一步以前**没有人做**，于是每个目标都永久停在等战报（实机一整夜 152 次），
而唯一读战报的代码只挂在「该攻击了」那条分支上——读战报的代码只在读过战报
之后才会被执行。补上之后它仍然一份都收不到，原因换成了**信箱窗口太小**：
两条链路的报告混在同一个收件箱里按时间倒序排，海盗链路整夜产出攻击报告，
而收取只盲开最上面 6 行。实机 2026-08-11 四趟全部报「翻不到」。

**而这两修都只修了探路那一半。** 收取当时只挑等探路战报的目标进信箱，攻击发的
战报没人读；后来两个等待态一起交进来，仍旧是一张**会漏项的名单**。现在干脆不按
名单收：这条路径认归属本来就只靠 VS 块里的目标坐标，翻信箱只靠「攻击报告」这个
主题，读到就存，名单也就漏不了态。

收取与「数今天已经打了几发」共用同一趟信箱，理由见 `PirateLoop.reconcile_today`。

## 只读详情页，但那是**两屏**

bot 战报比海盗战报多一行「生成卫星概率」，「战斗详情」横幅因此下移约 30px，
「单位」整行落到面板可视区之外。2026-08-11 的五张实拍里四张如此（第五张恰好
没有那一行，「单位」就读得出来）。所以详情页要拖到底再拍一屏。

而 VS 块与那行 `VICTORY` / `FAIL` 大字**只在没拖过的那一屏上**——拖到底之后
它们都滚出了可视区。两屏各读各的，见 `BotLoop._bottom_screens` 与
`LiveReportReader.read_detail_only`。

## 胜负以那行大字为准

用户口径（2026-08-17）：「游戏算法更新，剩余舰艇算法已经不准了，可以读 victory」。
所以战果先读横幅；读不出来才回落到「剩余 = 单位 − 损失单位」那套算式
（`domain.battle_outcome`），仲裁见 `vision.pirate_reports.decide_outcome`。

战果曾经也是**判据的输入**（平局要重打），2026-08-17 移除那条规则之后它退回成
纯展示字段：还是照样定、照样入库、照样在日志页和情报中心里看得到，只是不再
决定要不要再打一发。拖到底那一屏仍然要拖——战损那两个数只在那一屏上，
而它是页面「战损 我 X · 敌 Y」的来源，也是横幅读不出来时的兜底输入。

逐舰种明细则**整整差一屏**：参战战舰那两列在**回放页**上（`ReportLayout.
participating_rows` 是对着回放页量的，`tools.ingest_report` 也是从 replay 那一屏取的），
要拿到它得点开「查看战斗回放」——而那个按钮至今没有标定过的点击坐标，一份报告
还要多花两三秒 OCR。所以这条链路只读详情页，`fleet_snapshots` 一行不写。
先例是海盗战报：刻意只记胜负与战损总数（用户口径 2026-08-09，为省性能）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from evo_helper.domain.bot_round import BOT_ATTACK_PRESET, BotPhase, DispatchFact, phase_of
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_BOT
from evo_helper.game import pirate_ui
from evo_helper.tools.pirate_loop import (
    LoopOptions,
    PirateLoop,
    ReportIngest,
    TargetCheck,
    exit_code_for,
    parse_origin,
    rematch_note,
)

# `say` 从**定义它的**模块导入。`pirate_loop` 只是转手，而 strict mypy 的
# `no_implicit_reexport` 不认转手——从那边导会报 does not explicitly export。
from evo_helper.tools.runner_logging import install_runner_system_log
from evo_helper.tools.scan_coordinates import (
    LiveDriver,
    make_console_encoding_safe,
    make_ocr,
    run_with_foreground_guard,
    say,
)
from evo_helper.vision.parsers import ReportKind


@dataclass
class BotOptions:
    """这一轮的参数。"""

    targets: tuple[Coordinate, ...]
    #: 真的动鼠标派舰队。False 是默认档：站过去认一眼，一次点击都不做。
    attack: bool
    #: 本轮从何时算起。早于这个时刻的派遣属于上一轮，不参与本轮判态——
    #: 也就是上一轮打过的目标，这一轮照样要打一发。
    round_started_at: datetime | None = None
    #: 这一轮记账用的出发星球。语义与理由同 `pirate_loop.LoopOptions.origin`：
    #: 多个 bot 任务的区别就在这一个参数上，让 runner 自己去猜，两个任务的账
    #: 会记到同一颗星球上。
    origin: Coordinate | None = None
    #: 军力任务会逐目标带标题；缺项才是旧区域攻击的 BBB。绝不 OCR 校验预设内容。
    presets: dict[Coordinate, str] | None = None
    #: **强制**在这一轮开始前翻一次信箱，忽略冷却。仅手工排障用。
    #: 语义与理由同 `pirate_loop.LoopOptions.force_reconcile`：默认档不是
    #: 「不翻」而是「按冷却翻」，判据在 `domain.reconcile_cooldown`。
    force_reconcile: bool = False
    #: 本进程最多真正派出多少发。调度器按当前出发星球的空闲航线数传入，避免
    #: 盲目点到游戏的「航线已满」弹窗；手工运行不传则不设上限。
    max_dispatches: int | None = None


class BotLoop(PirateLoop):
    """复用海盗那条链路的驱动，换成 bot 的识别与胜负判定。"""

    TARGET_KIND: str = TARGET_KIND_BOT

    #: bot 星球是**有主**面板，按钮排布和敌对海盗那套完全不同。不覆盖的话每一发
    #: 都会点在空白处，然后倒在「找不到预设」上——实机上这条链路就是这么
    #: 一发都没派出去过的。
    ATTACK_BUTTON: tuple[int, int] = pirate_ui.BOT_ATTACK_BUTTON

    #: bot 这边**两种认不出都自愈**（父类只对 `MISMATCH` 自愈）。理由和海盗那边
    #: 正好相反：目标是扫描库里已经记过的 bot，站上去读不出 bot 本身就是异常，
    #: 而不是常态；而且一轮只有几个目标，多复位一次的代价远小于漏派一发。
    #: 这也保住了这条路径实机验证过的行为——原先失败就重试，不分是哪一种。
    RETRY_CHECKS: frozenset[TargetCheck] = frozenset({TargetCheck.MISMATCH, TargetCheck.ABSENT})

    #: 打 bot 的战报主题是「攻击报告」；海盗战是「海盗攻击报告」，走父类那一档。
    RECONCILE_KIND: ReportKind = ReportKind.ATTACK
    REPORT_LABEL: str = "攻击战报"

    def __init__(self, driver: LiveDriver, ocr: Any, options: BotOptions) -> None:
        # 父类要一个 LoopOptions。`scout=False`：这条链路不派任何前置侦查发了。
        # `origin` 必须原样带过去——父类的 `_record_intent` 读的是**父类那一份**
        # options，漏传的话这条链路写进 `attack_intents` 的出发坐标会退回全局
        # 主星，而多任务的区别恰恰就在这一个坐标上。
        super().__init__(
            driver,
            ocr,
            LoopOptions(
                systems=(),
                scout=False,
                attack=options.attack,
                preset=BOT_ATTACK_PRESET,
                origin=options.origin,
                force_reconcile=options.force_reconcile,
            ),
        )
        self._bot = options

    # -- 识别 ---------------------------------------------------------------

    def check_target(self, coordinate: Coordinate) -> TargetCheck:
        """行星面板上是不是这个 bot。自愈由父类的 `_goto_checked` 统一管。

        名字与坐标都要核，判据与坐标扫描器共用一套（`vision.scan_reading`）：
        导航栏偶尔会停在别的位号上，那时面板是真的、只是不是请求的那一位。

        判据本身没有放松的余地：实机那一轮里有一次读到的是**上一个目标的星系**
        （请求 2:321:5，面板显示 2:320:5），放松判据就等于打错星球。

        核对通过就 `navigator.confirm()`——那次核对本身就是导航栏的回读证据，
        导航器只信这种有证据的记忆（见 `SystemNavigator` 的类注释）。**不是 bot
        也照样确认**：确认的是「导航栏停在这一位」，与那一位上住着谁无关。
        """
        from evo_helper.game.system_navigator import crop_reader
        from evo_helper.vision.scan_reading import read_panel_confirming

        requested = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
        panel = read_panel_confirming(crop_reader(self._driver.capture(), self._ocr), requested)
        if not panel.confirms(requested):
            say(f"  坐标核对不过：面板读作 {panel.coordinate_text!r}，请求的是 {requested}")
            self._dump_coord_mismatch("bot-coord-mismatch")
            return TargetCheck.MISMATCH
        self._navigator.confirm(coordinate)
        if not panel.is_bot:
            say(f"  {coordinate} 不是 bot（面板名 {panel.display_name!r}）")
            return TargetCheck.ABSENT
        return TargetCheck.CONFIRMED

    # -- 收战报 -------------------------------------------------------------

    def _ingest_report(self, row: Any, page: Any) -> ReportIngest:
        """开工那一趟里读到的一封「攻击报告」：认出打的是谁，读通就入库。

        这一步以前**根本不存在**，而它是整条链路的死结：`phase_of` 要看到
        `DispatchFact.has_report` 才放目标往下走，那个字段来自 `battle_reports`
        里有没有一行指着这发派遣；而全仓没有任何代码为 bot 写过那张表。于是每个
        目标都永久停在等战报——实机跑一整夜，那一态出现 152 次。

        ⚠️ **不按「是不是本轮在等的那个目标」筛。** 读到就存。原先只把一部分等待
        态的目标交进来，另一档的战报于是没人读；名单越窄，漏的越多，而这条路径
        认归属本来就只靠报告自己写的目标坐标——按名单筛纯属多余。
        先例是侦察报告那条链路（`PirateLoop.collect_scout_reports` 里那段注释）。

        入库走 `append_report`，它会按「出发坐标 + 目标坐标 + 时间就近」自己认领
        那一发派遣（置 `dispatch_id` 与 `match_status='MATCHED'`），这里不另做匹配。
        """
        from evo_helper.vision.parsers import compact_ocr_text, parse_versus_block

        text = page.versus_block()
        versus = parse_versus_block(text, "ocr")
        if versus is None:
            # 读不出来**不猜**：猜错就把战报挂到别的 bot 头上，而那份战报的战果
            # 会决定那个坐标要不要再挨一发。
            #
            # **原文要跟着一起说出去**：坐标那一格现在会在几套配方平票时返回空串
            # （`vision.scan_reading.vote_coordinate`），只说一句「读不出来」的话，
            # 「配方吵架」与「压根没读到字」在日志里长得一模一样。
            say(
                f"  第 {row.index} 行的 VS 块读不出来；不猜它是谁的战报。"
                f"当时读到：{compact_ocr_text(text)}"
            )
            return ReportIngest.UNREADABLE
        return self._ingest_battle_report(versus.defender.coordinate.value, page)

    def _ingest_battle_report(self, target: Coordinate, page: Any) -> ReportIngest:
        """把详情页上这一份读成 `BattleReport` 并入库。读不出来就放过，不存半份。

        三值而不是布尔：「库里已有」是开工那一趟的早停凭据，「读不出来」不是——
        理由见 `ReportIngest`。
        """
        from uuid import uuid4

        from evo_helper.application.report_ingest import to_battle_report
        from evo_helper.vision.live_reports import DETAIL_UI_VERSION, LiveReportReader
        from evo_helper.vision.models import PageObservation
        from evo_helper.vision.parsers import UnknownUiVersionError

        try:
            live = LiveReportReader(page).read_detail_only(
                PageObservation(screen="mail_detail", ui_version=DETAIL_UI_VERSION, confidence=1.0),
                bottom=self._bottom_screens(),
            )
        except (UnknownUiVersionError, ValueError) as error:
            # 读不出来不是「没有战报」。这一份就放着，等 `MAX_REPORT_AGE` 到点把
            # 那发派遣判掉、允许重打一发（见 `repository.bot_dispatch_facts`）。
            say(f"  {target} 的战报读不出来：{error}")
            self._dump_frame("battle-report-unreadable")
            return ReportIngest.UNREADABLE
        # VS 块读了两遍（翻行时一遍、这里一遍），两遍必须指向同一个目标。
        # 不核的话，一次 OCR 抖动就能把这份战报挂到别人头上。
        if live.defender.coordinate.value != target:
            say(f"  {target} 的战报复核不过：这一份写的是 {live.defender.coordinate.value}")
            return ReportIngest.UNREADABLE
        repository, _run_id = self._ensure_run()
        if repository.has_report_at(target, live.reported_at_utc):
            # 已在库里的那一行未必认领上了派遣，顺手重认一次（理由见 `rematch_note`）。
            note = rematch_note(repository, target, live.reported_at_utc)
            say(f"  {target} 这份战报（{live.raw_time_text}）已经在库里；不重复入库{note}")
            return ReportIngest.KNOWN
        report_id = uuid4()
        repository.append_report(to_battle_report(live, report_id=report_id))
        # 战果是算出来的，所以算不出时要把**四个输入**一起说出来——否则日志上只有
        # 一句「算不出」，没人知道是哪一个数没读到，而它们分别对应两条不同的毛病
        # （没拖到底 / 那一屏的行位置偏了）。战果已不再影响要不要再打一发，
        # 但它是库里那一行的战果列，算不出就是永久的空洞。
        say(
            f"  {target} 战报入库：{live.raw_time_text}，"
            f"战果 {live.outcome or '算不出'}"
            f"（我 {live.attacker_units}−{live.attacker_losses}，"
            f"敌 {live.defender_units}−{live.defender_losses}）"
        )
        # 截图与海盗那条链路共用同一个落点（父类 `_store_report_screenshot`）：
        # 两条链路读的是同一块面板、存的是同一张表，判据只该有一份。
        self._store_report_screenshot(report_id, page)
        return ReportIngest.STORED

    # -- 主循环 -------------------------------------------------------------

    def _sweep(self) -> None:
        """一趟只把每个目标推进一态，然后退出。

        **不在进程内等战报。** 原先每个目标 `time.sleep(600)`，五个目标就是
        五十分钟独占鼠标，而这段时间本该拿去跑扫描。抵达时间已经写进
        `attack_dispatches.expected_report_at_utc`，到点由调度器把这条链路
        重新叫起来——这正是 `domain.report_wait` 模块头写的那条路。

        ⚠️ **覆盖的是 `_sweep` 而不是 `run`。** 原先这里覆盖 `run()`，把开工前置
        （校几何、确认会话、复位画面、切视图）抄了一遍，还漏了父类那个
        `except RoundExhausted`。两个后果都在实机上发生过：

        - `RoundExhausted("同时派遣的舰队数量已达上限")` 从这里漏出去，进程按
          退出码 1 收场；航线占满是**必然**会发生的事，连撞三次调度器就把整条
          bot 链路自动停用了（2026-08-11 02:43 实测）。
        - 后来给父类 `run()` 加的断线重连，这条链路一行都没吃到——因为它压根
          不走父类的 `run()`。

        覆盖 `_sweep` 之后这两件事都由父类统一管，不会再各写一份。

        ⚠️ **战报已经在开工那一趟信箱里收过了**（父类 `reconcile_today`），
        这里不再进第二趟。等战报的目标可能有几十个，而它们的报告本来就并排躺在
        同一页上；开工那一趟为了数今天的战报数**本来就要把那一页翻一遍**，
        顺手把认得出的都开了、都入了库，比另起一趟省下整套「切视图 → 开面板 →
        慢拖回顶 → 翻页 → 关面板」（约 20 秒）。

        态在开头一次算完，收进来的战报本趟就作数：`reconcile_today` 排在
        `_sweep` 之前，所以刚回来的战报这一趟就能让对应目标转入 `DONE`。
        **仍旧一趟只推进一态**——每个目标在这个循环里只走一个分支。
        """
        dispatched = 0
        for coordinate in self._bot.targets:
            if self._bot.max_dispatches is not None and dispatched >= self._bot.max_dispatches:
                say(f"  已派出 {dispatched} 发，达到本轮空闲航线预算；其余目标留待返航后继续")
                break
            phase = self._phase_of(coordinate)
            say(f"目标 {coordinate}（{phase.value}）")
            if phase is BotPhase.NEEDS_ATTACK:
                if self._attack_once(coordinate):
                    dispatched += 1
            elif phase is BotPhase.AWAITING_ATTACK_REPORT:
                self._say_still_waiting(coordinate)
            # `DONE` 无事可做。

    def _say_still_waiting(self, coordinate: Coordinate) -> None:
        """还在等战报的目标，日志上要分清三件事，一件都不许含糊。

        ⚠️ **这句话要说准。** 原先统一说「还没出现在信箱最上面几行」，把
        「窗口不够大」说成了「报告还没到」——而实机上正因就是前者：海盗链路整夜
        产出攻击报告，6 行窗口被别人的报告占满，六个目标一视同仁地报「翻不到」，
        连续四趟都是同一句。两者的处置完全相反（一个要把窗口开大、一个要接着等）。

        ⚠️ **第二次，同一个毛病，代价更大。** 改成上面那版之后，「战报到点了却
        没翻到；下一趟再来」这句话是在**一次信箱都没开**的情况下打出来的：它
        只查了库。而 2026-08-15 21:59 起本轮压根不翻信箱（见
        `domain.reconcile_cooldown` 的模块头），于是这条链路整整两天、每一轮、
        每一个目标都在说「翻不到战报」——**说的是一句假话**，而正是这句假话让
        「战报一份都没读回来」这件事拖了两天没人发现。日志把「我找过了，没有」
        和「我根本没去找」说成同一句，就等于把故障伪装成常态。

        所以现在按三档说：

        - 还没到点：接着等，与信箱无关。
        - 到点了，**本轮翻过信箱**没找到：这才是原来那句话，可以照说。
        - 到点了，**本轮没翻信箱**：说清没翻的理由，并带上**上次真正翻信箱是
          什么时候**——那才是用户判断「这一发到底有没有人去看过」的依据。
        """
        repository, _run_id = self._ensure_run()
        due = dict(repository.bot_report_due_at((coordinate,), since=self._round_start()))
        expected = due.get(coordinate, (None, None))[1]
        if expected is not None and expected > datetime.now(UTC):
            say(f"  战报预计 {expected:%H:%M:%S} UTC 才产生；接着等")
            return
        # `getattr` 而不是直接取：手工调子方法（补录入口、离线工具）时 `run()`
        # 没走过，这个字段可能压根没被建出来。取不到就按「翻过了」说——那是
        # `run()` 走完之后的常态，而这一句只在真的没翻时才该换措辞。
        decision = getattr(self, "_reconcile_decision", None)
        if decision is not None and not decision.sweep:
            last = decision.last_reconciled_at_utc
            when = f"{last:%Y-%m-%d %H:%M:%S} UTC" if last is not None else "从来没翻过"
            say(f"  战报到点了，但**本轮没翻信箱**（{decision.note}）；上次真正翻信箱：{when}")
            return
        say("  本轮翻过信箱，没找到这一发的战报；下一趟再来")

    def _phase_of(self, coordinate: Coordinate) -> BotPhase:
        """这个目标这一趟走到哪一步了。

        **只认目标模式（默认档，一次点击都不做）不查库。** 那一档根本不派，
        没有派遣事实可言，查库只会凭空要求一个数据库。`_attack_once` 自己会在
        `attack=False` 时停在识别那一步，所以这里直接当成「该去看一眼」。
        """
        if not self._bot.attack:
            return BotPhase.NEEDS_ATTACK
        return phase_of(self._dispatch_facts(coordinate))

    def _dispatch_facts(self, coordinate: Coordinate) -> tuple[DispatchFact, ...]:
        """本轮针对这个目标已经打过哪几发、战报回来了没有、打成了什么。"""
        repository, _run_id = self._ensure_run()
        return tuple(repository.bot_dispatch_facts(coordinate, since=self._round_start()))

    def _attack_once(self, coordinate: Coordinate) -> bool:
        """对这个坐标打一发 BBB。

        走到这里只有两种情形：本轮第一发，或者上一发的战报被判定永远不会来、
        整条剔掉之后重来的一发（`repository.bot_dispatch_facts`）。**两种在这里
        没有分别**——打法完全一样，分开写只会多一处可能和判态那边分家的地方。
        「还能不能再打」的判定只有 `phase_of` 一处。
        """
        if self._goto_checked(coordinate) is not TargetCheck.CONFIRMED:
            return False
        self._outcome.pirates.append(coordinate)
        if not self._bot.attack:
            return False
        preset = (self._bot.presets or {}).get(coordinate, BOT_ATTACK_PRESET)
        return self.attack(coordinate, preset=preset)

    def _round_start(self) -> datetime:
        """本轮从何时算起。**绝不返回 None。**

        `--round-started-at` 是可选的（手工跑时没人会填），但 `None` 一路传到
        仓储那边就是「不限时间范围」：`bot_dispatch_facts(since=None)` 会把这个
        坐标**历史上每一发**都算进本轮，于是上一轮（乃至上个月）打过的目标看起来
        像是「这一轮早就打完了」，这一轮一发都不会派。

        所以这里兜底成**当日 UTC 00:00**。取当天而不是「此刻」，是因为一趟里
        先派出的那几发必须仍算本轮；取 UTC 而不是本地时区，是因为游戏内时间
        一律 UTC+0（见 `vision.parsers` 的 `GAME_DISPLAY_ZONE`）。
        """
        if self._bot.round_started_at is not None:
            return self._bot.round_started_at
        return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def parse_round_start(text: str) -> datetime:
    """本轮起始时刻。**必须带时区**，否则拒收。

    `datetime.fromisoformat` 对 naive 值一声不响地照收，而这个值是要拿去和库里
    的 UTC 时间戳比大小的。SQLite 上比较 naive 与 aware 不报错，只是结果悄悄偏
    掉时差——上一轮的派遣被算进本轮，于是这一轮的目标看起来「已经打过了」。
    仓储那边用 `_require_utc` 守同一条线，这里在入口就守住。
    """
    try:
        value = datetime.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"认不出的 ISO 8601 时刻 {text!r}：{error}") from error
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"{text!r} 没带时区。要写成 UTC，例如 2026-08-09T00:00:00+00:00 或 …Z"
        )
    return value.astimezone(UTC)


def parse_target(text: str) -> Coordinate:
    parts = text.split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError(
            f"坐标要写成 银河:恒星系:行星，例如 2:137:14（收到 {text!r}）"
        )
    galaxy, system, position = (int(part) for part in parts)
    return Coordinate(galaxy, system, position)


def parse_target_assignment(text: str) -> tuple[Coordinate, str | None]:
    """解析 ``坐标=预设标题``；不带等号保持旧 CLI 的 BBB 语义。"""
    coordinate_text, separator, preset = text.partition("=")
    coordinate = parse_target(coordinate_text)
    if not separator:
        return coordinate, None
    if not preset.strip():
        raise argparse.ArgumentTypeError("预设标题不能为空")
    return coordinate, preset


def main(argv: list[str] | None = None) -> int:
    make_console_encoding_safe()  # 必须在 parse_args 之前，理由见那个函数
    # 日志出口。装不上就是空操作，`say()` 照常打到控制台。
    install_runner_system_log()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", nargs="+", type=parse_target_assignment, required=True)
    parser.add_argument(
        "--attack", action="store_true", help=f"真的用预设 {BOT_ATTACK_PRESET} 打，每个目标一发"
    )
    parser.add_argument(
        "--origin",
        type=parse_origin,
        default=None,
        help="出发星球（记账用）。调度器会传；手工跑不给则用 EVO_HELPER_ORIGIN",
    )
    parser.add_argument(
        "--round-started-at",
        type=parse_round_start,
        default=None,
        help="本轮起始时刻（ISO 8601，必须带时区）。调度器会传；手工跑不给则按当日 UTC 00:00 算",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="强制翻一趟信箱读当日攻击战报，忽略冷却（手工排障用；不给则按冷却自动决定）",
    )
    parser.add_argument(
        "--max-dispatches",
        type=int,
        default=None,
        help="本进程最多实际派出多少发；调度器按该出发点空闲航线数传入",
    )
    args = parser.parse_args(argv)
    if args.max_dispatches is not None and args.max_dispatches < 1:
        parser.error("--max-dispatches 必须至少为 1")

    import ctypes

    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    options = BotOptions(
        targets=tuple(item[0] for item in args.targets),
        attack=args.attack,
        round_started_at=args.round_started_at,
        origin=args.origin,
        presets={item[0]: item[1] for item in args.targets if item[1] is not None} or None,
        force_reconcile=args.reconcile,
        max_dispatches=args.max_dispatches,
    )
    mode = "真打" if args.attack else "只认目标"
    listed = ", ".join(
        f"{target}={(options.presets or {}).get(target, BOT_ATTACK_PRESET)}"
        for target in options.targets
    )
    say(f"模式：{mode}；目标 {listed}")

    def go() -> int:
        driver = LiveDriver(allow_actions=args.attack)
        driver.window()
        outcome = BotLoop(driver, make_ocr(), options).run()
        say(
            f"完成：目标 {len(outcome.pirates)} 个，攻击 {len(outcome.attacked)} 发，"
            f"拦下 {len(outcome.refused)} 次"
        )
        for coordinate, reason in outcome.refused:
            say(f"  [拦下] {coordinate} {reason}")
        # 退出码与海盗那条共用一份判据（`exit_code_for`）：切不到出发星球时两边都要
        # 报 `EXIT_ENVIRONMENT_BUSY`，各写一份迟早分家。
        return exit_code_for(outcome)

    return run_with_foreground_guard(go)


__all__ = ["BotLoop", "BotOptions", "main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
