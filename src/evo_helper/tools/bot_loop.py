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

## 为什么读「单位」总数而不是逐舰种明细

分档防的是**量级错**，不是末位误差（见 `domain.fleet_tier` 模块头）。「单位」总数是
详情页上独立给出的一个数，一个 ROI 就读到；逐舰种明细要进回放页、读两列、
还要反复重拍到合计对上，一份报告多花两三秒，而它对分档没有增量价值。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from evo_helper.domain.fleet_preset import DEFAULT_PRESET
from evo_helper.domain.fleet_tier import FleetTier, tier_for
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_BOT
from evo_helper.tools.pirate_loop import (
    MAIL_FIRST_ROW_Y,
    MAIL_ROW_PITCH,
    MAIL_ROW_X,
    MAIL_SCAN_ROWS,
    PANEL_DRAG_FROM_Y,
    PANEL_DRAG_TO_Y,
    LoopOptions,
    PirateLoop,
    say,
    slow_drag,
)
from evo_helper.tools.scan_coordinates import LiveDriver, make_ocr

#: 攻击侦查用的预设标题：探路（`domain.fleet_preset.DEFAULT_PRESET`）。
PROBE_PRESET = DEFAULT_PRESET.name


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

    def is_bot_target(self, coordinate: Coordinate) -> bool:
        """行星面板上是不是这个 bot。

        名字与坐标都要核，判据与坐标扫描器共用一套（`vision.scan_reading`）：
        导航栏偶尔会停在别的位号上，那时面板是真的、只是不是请求的那一位。
        """
        from evo_helper.game.system_navigator import crop_reader
        from evo_helper.vision.scan_reading import read_panel_confirming

        requested = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
        panel = read_panel_confirming(crop_reader(self._driver.capture(), self._ocr), requested)
        if not panel.confirms(requested):
            say(f"  坐标核对不过：面板读作 {panel.coordinate_text!r}，请求的是 {requested}")
            return False
        if not panel.is_bot:
            say(f"  {coordinate} 不是 bot（面板名 {panel.display_name!r}）")
            return False
        return True

    # -- 判定 ---------------------------------------------------------------

    def read_defender_units(self, coordinate: Coordinate) -> int | None:
        """去信箱把这个目标最近那份攻击报告的守方「单位」总数读回来。

        只读详情页的一个 ROI。找报告靠**VS 块里的目标坐标**核对，不靠行号：
        行序随新邮件变，而报告自己写着打的是谁。
        """
        from evo_helper.vision.optional.report_screens import ImageReportScreens
        from evo_helper.vision.parsers import parse_versus_block
        from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport

        def screens() -> Any:
            image = crop_to_viewport(self._driver.capture())
            return ImageReportScreens(
                image,
                layout_for_viewport(image.width, image.height),
                tesseract_cmd=_tesseract(),
            )

        if not self._goto_planet_surface():
            raise RuntimeError("切不到自己星球地表，读不了信箱；安全停止")
        self._open_mail()
        for _ in range(3):
            slow_drag(self._driver, PANEL_DRAG_TO_Y, PANEL_DRAG_FROM_Y)
        found: int | None = None
        for row in range(MAIL_SCAN_ROWS):
            if not self._settle(self._on_mail_list):
                say(f"  第 {row} 行之前已经不在邮件列表上了；停止翻行")
                break
            self._driver.click(
                MAIL_ROW_X, MAIL_FIRST_ROW_Y + row * MAIL_ROW_PITCH, label="打开邮件"
            )
            self._driver.wait(2.4)
            page = screens()
            versus = parse_versus_block(page.versus_block(), "ocr")
            if versus is not None and versus.defender.coordinate.value == coordinate:
                units = page.unit_totals()[1]
                found = _count(units)
                say(f"  第 {row} 行是 {coordinate} 的战报：守方单位 {units!r} → {found}")
            self._driver.click(*_mail_back(), label="返回")
            self._driver.wait(2.0)
            if found is not None:
                break
        self._close_mail()
        return found

    # -- 主循环 -------------------------------------------------------------

    def run(self) -> Any:  # noqa: D401 - 覆盖父类的海盗循环
        """一趟只把每个目标推进一态，然后退出。

        **不在进程内等战报。** 原先每个目标 `time.sleep(600)`，五个目标就是
        五十分钟独占鼠标，而这段时间本该拿去跑扫描。抵达时间已经写进
        `attack_dispatches.expected_report_at_utc`，到点由调度器把这条链路
        重新叫起来——这正是 `domain.report_wait` 模块头写的那条路。
        """
        from evo_helper.domain.bot_round import BotPhase
        from evo_helper.game.game_window import ensure_game_window

        ensure_game_window()
        self._reset_to_known_screen()
        if not self._navigator.ensure_system_view(self._nav_labels):
            raise RuntimeError("切不到恒星系视图；停止而不是往固定坐标乱点")

        for coordinate in self._bot.targets:
            phase = self._phase_of(coordinate)
            say(f"目标 {coordinate}（{phase.value}）")
            if phase is BotPhase.NEEDS_PROBE:
                self._probe(coordinate)
            elif phase is BotPhase.NEEDS_ATTACK:
                self._tier_and_attack(coordinate)
            # 其余三态这一趟没事可做：等战报，或已走完。
        return self._outcome

    def _phase_of(self, coordinate: Coordinate) -> Any:
        """这个目标这一趟走到哪一步了。

        **只认目标模式（默认档，一次点击都不做）不查库。** 那一档根本不派，
        没有派遣事实可言，查库只会凭空要求一个数据库。`_probe` 自己会在
        `probe=False` 时停在识别那一步，所以这里直接当成「该去看一眼」。
        """
        from evo_helper.domain.bot_round import BotPhase, phase_of

        if not self._bot.probe:
            return BotPhase.NEEDS_PROBE
        return phase_of(self._dispatch_facts(coordinate))

    def _dispatch_facts(self, coordinate: Coordinate) -> tuple[Any, ...]:
        """本轮针对这个目标已经派过哪些发、战报回来了没有。"""
        # 仓储上的 `bot_dispatch_facts` 由调度器那一批任务提供。这里按 Any 取，
        # 好让这条链路能先独立合入——两边在同一波次里并行改。
        # TODO(Task 7): `bot_dispatch_facts` 合流后删掉这个 Any，改回
        # `repository, _run_id = self._ensure_run()`，让 mypy 真的检查这个调用。
        repository: Any = self._ensure_run()[0]
        return tuple(repository.bot_dispatch_facts(coordinate, since=self._round_start()))

    def _probe(self, coordinate: Coordinate) -> None:
        """派一发探路。走的是攻击链路，所以简报上写的是「攻击」。"""
        self._navigator.goto(coordinate)
        if not self.is_bot_target(coordinate):
            return
        self._outcome.pirates.append(coordinate)
        if not self._bot.probe:
            return
        if self.attack(coordinate, preset=PROBE_PRESET):
            self._outcome.scouted.append(coordinate)

    def _tier_and_attack(self, coordinate: Coordinate) -> None:
        """探路战报已回：读守方单位数、分档、按档位真打。"""
        if not self._bot.attack:
            return
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
        self._navigator.goto(coordinate)
        if not self.is_bot_target(coordinate):
            self._outcome.refused.append((coordinate, "攻击前面板认不出"))
            return
        self.attack(coordinate, preset=preset)

    def _mark_skipped(self, coordinate: Coordinate) -> None:
        """把「分档说不值得打」记进库，否则下一趟又会重新分一次档。"""
        # TODO(Task 7): `mark_bot_target_skipped` 合流后删掉这个 Any，改回
        # `repository, _run_id = self._ensure_run()`，让 mypy 真的检查这个调用。
        repository: Any = self._ensure_run()[0]
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


def _mail_back() -> tuple[int, int]:
    from evo_helper.tools.pirate_loop import MAIL_BACK

    return MAIL_BACK


def _tesseract() -> str:
    from evo_helper.tools.scan_coordinates import TESSERACT_PATH

    return str(TESSERACT_PATH)


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
