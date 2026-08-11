"""bot 目标的「攻击侦查 → 分档 → 攻击」自动化。

    # 只看目标认不认得出，一次点击都不派（默认）
    python -m evo_helper.tools.bot_loop --targets 2:137:14

    # 攻击侦查：用「探路」预设打一发，回来读战报分档
    python -m evo_helper.tools.bot_loop --targets 2:137:14 --probe

    # 完整：侦查 → 分档 → 用该档预设攻击
    python -m evo_helper.tools.bot_loop --targets 2:137:14 --probe --attack

与海盗那条链路的区别只在**判定依据**：

- 海盗看侦察报告里几个特定舰种的数量（`vision.scout_reports`），
  因为海盗要么有舰队要么没有，不需要分档。
- bot 看**攻击侦查打回来的战报**里守方的「单位」总数，按 `domain.fleet_tier`
  分成 2K–5K / 5K–8K / 8K+ 三档，各档一个预设（AAA / BBB / CCC）。
  2K 以下不派——用户明确说过那个量级不值得为它挑组合。

所以导航、简报闸门、选预设、写 intent/dispatch 全部复用 `pirate_loop.PirateLoop`；
这里只换目标识别与判定。

## 一趟推一态，战报在开工那一趟信箱里收

五个态见 `domain.bot_round.phase_of`。两个等待态（`AWAITING_PROBE_REPORT` 与
`AWAITING_ATTACK_REPORT`）的出路是**开工那一趟信箱**（父类 `reconcile_today`）：
把认得出的攻击报告读出来写进 `battle_reports`，`phase_of` 才看得到 `has_report`。

这一步以前**没有人做**，于是每个目标都永久停在等战报（实机一整夜 152 次），
而唯一读战报的代码只挂在 `NEEDS_ATTACK` 分支上——读战报的代码只在读过战报
之后才会被执行。补上之后它仍然一份都收不到，原因换成了**信箱窗口太小**：
两条链路的报告混在同一个收件箱里按时间倒序排，海盗链路整夜产出攻击报告，
而收取只盲开最上面 6 行。实机 2026-08-11 四趟全部报「翻不到」。

**而这两修都只修了探路那一半。** 收取当时只挑 `AWAITING_PROBE_REPORT` 的目标进
信箱，攻击发的战报没人读；后来两个等待态一起交进来，仍旧是一张**会漏项的名单**。
现在干脆不按名单收：这条路径认归属本来就只靠 VS 块里的目标坐标，翻信箱只靠
「攻击报告」这个主题，两种发的战报长得一模一样——读到就存，名单也就漏不了态。

收取与「数今天已经打了几发」共用同一趟信箱，理由见 `PirateLoop.reconcile_today`。

## 只读详情页，但那是**两屏**

分档防的是**量级错**，不是末位误差（见 `domain.fleet_tier` 模块头）。「单位」总数是
详情页上独立给出的一个数，一个 ROI 就读到——**前提是它画出来了**。

bot 战报比海盗战报多一行「生成卫星概率」，「战斗详情」横幅因此下移约 30px，
「单位」整行落到面板可视区之外。2026-08-11 的五张实拍里四张如此（第五张恰好
没有那一行，「单位」就读得出来）。所以详情页要拖到底再拍一屏，只为这一行。

而 VS 块与那行 `VICTORY` / `FAIL` 大字**只在没拖过的那一屏上**——拖到底之后
它们都滚出了可视区。两屏各读各的，见 `BotLoop._bottom_screens` 与
`LiveReportReader.read_detail_only`。

## 胜负是算出来的，不看那行大字

用户口径（2026-08-11）：剩余 = 单位 − 损失单位；本方剩余 0 判负、对方被全歼判胜、
两边都有船判平（`domain.battle_outcome`）。于是拖那一屏从「补一个展示字段」变成了
**判据的输入**——「损失单位」只有拖到底才读得到，不拖就永远算不出战果。

逐舰种明细则**整整差一屏**：参战战舰那两列在**回放页**上（`ReportLayout.
participating_rows` 是对着回放页量的，`tools.ingest_report` 也是从 replay 那一屏取的），
要拿到它得点开「查看战斗回放」——而那个按钮至今没有标定过的点击坐标，一份报告
还要多花两三秒 OCR。所以这条链路只读详情页，`fleet_snapshots` 一行不写。
先例是海盗战报：刻意只记胜负与战损总数（用户口径 2026-08-09，为省性能）。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from evo_helper.domain.bot_round import BotPhase, DispatchFact, phase_of
from evo_helper.domain.fleet_preset import DEFAULT_PRESET
from evo_helper.domain.fleet_tier import FleetTier, tier_for
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_BOT
from evo_helper.game import pirate_ui
from evo_helper.tools.pirate_loop import LoopOptions, PirateLoop, ReportIngest, TargetCheck

# `say` 从**定义它的**模块导入。`pirate_loop` 只是转手，而 strict mypy 的
# `no_implicit_reexport` 不认转手——从那边导会报 does not explicitly export。
from evo_helper.tools.scan_coordinates import LiveDriver, make_ocr, say
from evo_helper.vision.parsers import ReportKind

#: 攻击侦查用的预设标题：探路（`domain.fleet_preset.DEFAULT_PRESET`）。
PROBE_PRESET = DEFAULT_PRESET.name

#: 「这个目标在等一份战报」的那几个态。**两个都要在这里。**
#:
#: 写成一个具名集合而不是在 `_sweep` 里手写 `phase is ...`：那种写法漏一个态
#: 不报错、不留日志，只是那一档目标再也不被收取——`AWAITING_ATTACK_REPORT`
#: 就是这么漏掉的（见 `_sweep`）。往 `BotPhase` 里加新的等待态时，
#: 编译器同样不会提醒，但至少只有这一处要改。
_AWAITING_REPORT = frozenset({BotPhase.AWAITING_PROBE_REPORT, BotPhase.AWAITING_ATTACK_REPORT})


@dataclass
class BotOptions:
    targets: tuple[Coordinate, ...]
    probe: bool
    attack: bool
    #: 本轮从何时算起。早于这个时刻的派遣属于上一轮，不参与本轮判态。
    round_started_at: datetime | None = None


class BotLoop(PirateLoop):
    """复用海盗那条链路的驱动，换成 bot 的识别与分档判定。"""

    TARGET_KIND: str = TARGET_KIND_BOT

    #: bot 星球是**有主**面板，按钮排布和敌对海盗那套完全不同。不覆盖的话每一发
    #: 都会点在空白处，然后倒在「找不到预设 探路」上——实机上这条链路就是这么
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
        # 父类要一个 LoopOptions；预设按档现选，这里先填探路。
        super().__init__(
            driver,
            ocr,
            LoopOptions(
                systems=(), scout=options.probe, attack=options.attack, preset=PROBE_PRESET
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

    # -- 判定 ---------------------------------------------------------------

    def _scan_mail(
        self,
        wanted: Sequence[Coordinate],
        visit: Callable[[Coordinate, Any], None],
        *,
        not_before: datetime | None = None,
    ) -> set[Coordinate]:
        """**一趟信箱**：把认得出的那几份战报交给 `visit`。返回没找到的目标。

        为什么一趟读完而不是「一个目标进一次」：进出信箱要切视图、开面板、翻标签，
        每次还要慢拖三下，一趟十几秒；而这些报告本来就并排躺在同一页上。

        找报告靠 **VS 块里的目标坐标**核对，不靠行号：行序随新邮件变，
        而报告自己写着打的是谁。

        翻信箱的姿势（关浮层、先读主题再决定开不开、翻屏、按时间早停）由父类的
        `_scan_mail_rows` 统一管——两条链路共用一份，见那边的模块级说明。
        这里只提供「这一封是不是我要的」和「拿到之后干什么」。
        """
        from evo_helper.vision.parsers import parse_versus_block

        remaining = set(wanted)

        def visit_row(row: Any, page: Any) -> bool:
            versus = parse_versus_block(page.versus_block(), "ocr")
            if versus is None:
                # 读不出来**不猜**：猜错就把战报挂到别的 bot 头上，接着按它的
                # 舰队量去挑攻击组合。
                say(f"  第 {row.index} 行的 VS 块读不出来；不猜它是谁的战报")
                return False
            target = versus.defender.coordinate.value
            if target not in remaining:
                say(f"  第 {row.index} 行是 {target} 的战报，不在这一趟要收的目标里")
                return False
            remaining.discard(target)
            say(f"  第 {row.index} 行是 {target} 的战报")
            visit(target, page)
            return not remaining

        if not remaining:
            return remaining
        self._scan_mail_rows(
            wanted=ReportKind.ATTACK, label="攻击战报", visit=visit_row, not_before=not_before
        )
        return remaining

    def _ingest_report(self, row: Any, page: Any) -> ReportIngest:
        """开工那一趟里读到的一封「攻击报告」：认出打的是谁，读通就入库。

        **探路发与攻击发共用这一条路径，而且必须共用。** 两种发打的是同一个坐标、
        走的是同一条攻击链路、产出的报告主题同为「攻击报告」，认归属靠的又都是 VS
        块里的目标坐标——这里从头到尾没有一处读得到「这一份是哪个预设打的」，
        也不需要读。按预设各写一条读法，只会让下一次修补又只修好一半。

        这一步以前**根本不存在**，而它是整条链路的死结：`phase_of` 要看到
        `DispatchFact.has_report` 才放目标往下走，那个字段来自 `battle_reports`
        里有没有一行指着这发派遣；而全仓没有任何代码为 bot 写过那张表。于是每个
        目标都永久停在 `AWAITING_PROBE_REPORT`——实机跑一整夜，那一态出现 152 次，
        `NEEDS_ATTACK` 出现 0 次。

        ⚠️ **不按「是不是本轮在等的那个目标」筛。** 读到就存。原先只把
        `AWAITING_PROBE_REPORT` 那几个目标交进来，攻击发的战报于是没人读；
        后来两个等待态一起交，仍旧是一张会漏项的名单。名单越窄，漏的越多，
        而这条路径认归属本来就只靠报告自己写的目标坐标——按名单筛纯属多余。
        先例是侦察报告那条链路（`PirateLoop.collect_scout_reports` 里那段注释）。

        入库走 `append_report`，它会按「出发坐标 + 目标坐标 + 时间就近」自己认领
        那一发派遣（置 `dispatch_id` 与 `match_status='MATCHED'`），这里不另做匹配。
        """
        from evo_helper.vision.parsers import parse_versus_block

        versus = parse_versus_block(page.versus_block(), "ocr")
        if versus is None:
            # 读不出来**不猜**：猜错就把战报挂到别的 bot 头上，接着按它的
            # 舰队量去挑攻击组合。
            say(f"  第 {row.index} 行的 VS 块读不出来；不猜它是谁的战报")
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
            # 那发派遣判掉、允许重新探路（见 `repository.bot_dispatch_facts`）。
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
            say(f"  {target} 这份战报（{live.raw_time_text}）已经在库里；不重复入库")
            return ReportIngest.KNOWN
        repository.append_report(to_battle_report(live, report_id=uuid4()))
        # 战果是算出来的，所以算不出时要把**四个输入**一起说出来——否则日志上只有
        # 一句「算不出」，没人知道是哪一个数没读到，而它们分别对应两条不同的毛病
        # （没拖到底 / 那一屏的行位置偏了）。
        say(
            f"  {target} 战报入库：{live.raw_time_text}，"
            f"战果 {live.outcome or '算不出'}"
            f"（我 {live.attacker_units}−{live.attacker_losses}，"
            f"敌 {live.defender_units}−{live.defender_losses}）"
        )
        return ReportIngest.STORED

    def read_defender_units(self, coordinate: Coordinate) -> int | None:
        """去信箱把这个目标最近那份攻击报告的守方「单位」总数读回来。

        只读详情页的一个 ROI。**这是兜底路径**：正常情况下这个数在收报告那一趟
        已经读过并入库了，`_tier_and_attack` 先问库（`latest_defender_units`）。

        没拖过的那一屏上读不到时要拖到底再读一次，理由与 `_bottom_screens` 同——
        bot 战报的「单位」那一行多半压根不在可视区里。这里不能只修入库那一条路：
        兜底路径读不出来就返回 None，而 None 会让 `_tier_and_attack` 整个跳过。
        """
        found: int | None = None

        def visit(target: Coordinate, page: Any) -> None:
            nonlocal found
            units = page.unit_totals()[1]
            # 判据是「读不出来」，不是「读出来是 0」——0 是一条真读数。
            if _count(units) is None:
                units = self._bottom_screens().unit_totals()[1]
            found = _count(units)
            say(f"  {target} 的战报：守方单位 {units!r} → {found}")

        self._scan_mail((coordinate,), visit)
        return found

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

        收取那一步认归属只靠报告自己写的目标坐标，不认名单——所以两个等待态
        （`AWAITING_PROBE_REPORT` / `AWAITING_ATTACK_REPORT`）不需要在这里列出来，
        也就不会再出现「名单漏了一个态，那一档目标永远收不到战报」这种事。
        `AWAITING_ATTACK_REPORT` 当初就是这么被漏掉的：注释写着「等攻击战报……
        这一趟没事可做」，而那句话预设了别处有人收，别处却没有人。

        态在开头一次算完，收进来的战报本轮就作数：`reconcile_today` 排在
        `_sweep` 之前，所以刚回来的探路战报这一趟就能拿去分档。**仍旧一趟只推进
        一态**——每个目标在这个循环里只走一个分支。
        """
        for coordinate in self._bot.targets:
            phase = self._phase_of(coordinate)
            say(f"目标 {coordinate}（{phase.value}）")
            if phase is BotPhase.NEEDS_PROBE:
                self._probe(coordinate)
            elif phase is BotPhase.NEEDS_ATTACK:
                self._tier_and_attack(coordinate)
            elif phase in _AWAITING_REPORT:
                self._say_still_waiting(coordinate)
            # `DONE` 无事可做。

    def _say_still_waiting(self, coordinate: Coordinate) -> None:
        """还在等战报的目标，日志上要分清「还没到点」和「到点了却没翻到」。

        ⚠️ **这句话要说准。** 原先统一说「还没出现在信箱最上面几行」，把
        「窗口不够大」说成了「报告还没到」——而实机上正因就是前者：海盗链路整夜
        产出攻击报告，6 行窗口被别人的报告占满，六个目标一视同仁地报「翻不到」，
        连续四趟都是同一句。两者的处置完全相反（一个要把窗口开大、一个要接着等）。
        """
        repository, _run_id = self._ensure_run()
        due = dict(repository.bot_report_due_at((coordinate,), since=self._round_start()))
        expected = due.get(coordinate, (None, None))[1]
        if expected is not None and expected > datetime.now(UTC):
            say(f"  战报预计 {expected:%H:%M:%S} UTC 才产生；接着等")
        else:
            say("  战报到点了却没翻到；下一趟再来")

    def _phase_of(self, coordinate: Coordinate) -> BotPhase:
        """这个目标这一趟走到哪一步了。

        **只认目标模式（默认档，一次点击都不做）不查库。** 那一档根本不派，
        没有派遣事实可言，查库只会凭空要求一个数据库。`_probe` 自己会在
        `probe=False` 时停在识别那一步，所以这里直接当成「该去看一眼」。
        """
        if not self._bot.probe:
            return BotPhase.NEEDS_PROBE
        return phase_of(self._dispatch_facts(coordinate))

    def _dispatch_facts(self, coordinate: Coordinate) -> tuple[DispatchFact, ...]:
        """本轮针对这个目标已经派过哪些发、战报回来了没有。"""
        repository, _run_id = self._ensure_run()
        return tuple(repository.bot_dispatch_facts(coordinate, since=self._round_start()))

    def _probe(self, coordinate: Coordinate) -> None:
        """派一发探路。走的是攻击链路，所以简报上写的是「攻击」。"""
        if self._goto_checked(coordinate) is not TargetCheck.CONFIRMED:
            return
        self._outcome.pirates.append(coordinate)
        if not self._bot.probe:
            return
        if self.attack(coordinate, preset=PROBE_PRESET):
            self._outcome.scouted.append(coordinate)

    def _tier_and_attack(self, coordinate: Coordinate) -> None:
        """探路战报已回：取守方单位数、分档、按档位真打。

        **先问库。** 走到这一态的前提就是「本轮的探路战报已经入库」，那个数在
        开工那一趟信箱已经读过了；再进一趟信箱既多花十几秒，翻到的
        还可能是上一轮的报告（信箱那条路没有时间闸门）。库里没有才现场读一次。
        """
        if not self._bot.attack:
            return
        units = self._stored_defender_units(coordinate)
        if units is None:
            units = self.read_defender_units(coordinate)
        if units is None:
            say(f"  {coordinate} 读不到战报里的守方单位数；不打")
            self._outcome.refused.append((coordinate, "读不到守方单位数"))
            return
        tier = tier_for(units)
        preset = tier.preset
        say(f"  {coordinate} 守方 {units} → {tier.value}；预设 {preset or '（不派）'}")
        if preset is None:
            self._outcome.refused.append((coordinate, f"{tier.value}，不值得打"))
            self._mark_skipped(coordinate)
            return
        if self._goto_checked(coordinate) is not TargetCheck.CONFIRMED:
            self._outcome.refused.append((coordinate, "攻击前面板认不出"))
            return
        self.attack(coordinate, preset=preset)

    def _stored_defender_units(self, coordinate: Coordinate) -> int | None:
        """本轮已入库的守方「单位」总数；没有就 None（调用方现场再读一次）。"""
        repository, _run_id = self._ensure_run()
        return repository.latest_defender_units(coordinate, since=self._round_start())

    def _mark_skipped(self, coordinate: Coordinate) -> None:
        """把「分档说不值得打」记进库，否则下一趟又会重新分一次档。"""
        repository, _run_id = self._ensure_run()
        repository.mark_bot_target_skipped(coordinate, since=self._round_start())

    def _round_start(self) -> datetime:
        """本轮从何时算起。**绝不返回 None。**

        `--round-started-at` 是可选的（手工跑时没人会填），但 `None` 一路传到
        仓储那边就是「不限时间范围」：`mark_bot_target_skipped(since=None)` 会把
        这个坐标**历史上每一轮的每一条 intent** 全刷成跳过。手工跑一次
        `--probe --attack`，只要有一个目标被分档判成「不值得打」就会触发。

        所以这里兜底成**当日 UTC 00:00**。取当天而不是「此刻」，是因为一趟里
        先派出的那几发必须仍算本轮；取 UTC 而不是本地时区，是因为游戏内时间
        一律 UTC+0（见 `vision.parsers` 的 `GAME_DISPLAY_ZONE`）。
        """
        if self._bot.round_started_at is not None:
            return self._bot.round_started_at
        return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _count(text: str) -> int | None:
    from evo_helper.domain.fleet_tier import parse_fleet_count

    return parse_fleet_count(text) if text else None


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", nargs="+", type=parse_target, required=True)
    parser.add_argument("--probe", action="store_true", help="真的用「探路」打一发攻击侦查")
    parser.add_argument("--attack", action="store_true", help="拿到战报后按档位真的攻击")
    parser.add_argument(
        "--round-started-at",
        type=parse_round_start,
        default=None,
        help="本轮起始时刻（ISO 8601，必须带时区）。调度器会传；手工跑不给则按当日 UTC 00:00 算",
    )
    args = parser.parse_args(argv)

    if args.attack and not args.probe:
        parser.error("--attack 需要 --probe：没有攻击侦查打回来的战报就没有分档依据")

    import ctypes

    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    options = BotOptions(
        targets=tuple(args.targets),
        probe=args.probe,
        attack=args.attack,
        round_started_at=args.round_started_at,
    )
    mode = "只认目标" if not args.probe else ("侦查+攻击" if args.attack else "只侦查")
    listed = ", ".join(str(target) for target in options.targets)
    say(f"模式：{mode}；目标 {listed}")

    driver = LiveDriver(allow_actions=args.probe or args.attack)
    driver.window()
    outcome = BotLoop(driver, make_ocr(), options).run()
    say(
        f"完成：目标 {len(outcome.pirates)} 个，侦查 {len(outcome.scouted)} 发，"
        f"攻击 {len(outcome.attacked)} 发，拦下 {len(outcome.refused)} 次"
    )
    for coordinate, reason in outcome.refused:
        say(f"  [拦下] {coordinate} {reason}")
    return 0


__all__ = ["BotLoop", "BotOptions", "FleetTier", "main"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
