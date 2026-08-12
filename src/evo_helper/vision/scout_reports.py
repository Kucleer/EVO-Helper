"""侦察报告的读取：只为回答「这个海盗打不打」。

海盗攻击链路的判定输入就这一样东西——`game.pirate_ui.PIRATE_TRIGGER_SHIPS`
那四个舰种各有多少。所以这里**不读**资源、不读建筑、不读全部 21 行战舰，
只按名字取那四行。

两屏（与海盗战报同构，原因也一样：内容在一屏里放不下）：

- **未滚动**：开头那段话，含出发与目标坐标 —— 用来核对「这份报告是不是刚侦察的那一位」
- **拖到底**：战舰清单尾段，四个判定舰种都在这一段

⚠️ **必须按名字取数，不能按行序对位。** 实测逐行对位会掉行（`钛能守卫者`
整行没被认出），之后每一行的数字都串位——`拦截导弹` 读成 5，真值是 0。
串位比读不出更危险：数字看着都合理，没有任何地方会报错。

⚠️ **四个舰种缺一个就整份拒收。** 缺席有两种可能：这一屏没滚到底，
或者那一行没读出来。两种都不能当成 0——当成 0 就会把一个有舰队的海盗
判成空位而放过（轻），或者把一份没读全的报告当成读全了（重）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scan_bounds import MAX_POSITION, SYSTEMS_PER_GALAXY, TOTAL_GALAXIES
from evo_helper.domain.scout_verdict import (
    VERDICT_ATTACK,
    VERDICT_SKIP,
    VERDICT_UNREADABLE,
    verdict_for,
)
from evo_helper.game.pirate_ui import PIRATE_TRIGGER_SHIPS
from evo_helper.vision.parsers import (
    COORDINATE_RE,
    GAME_DISPLAY_ZONE,
    ReportKind,
    classify_report_subject,
    parse_report_timestamp,
)
from evo_helper.vision.report_layout import ColumnBand, Region

#: 开头那句话的**第一行**（两个坐标都在这一行上）。标定视口 1920×879，
#: 实机量于 2026-08-09：整窗 917 空间里这一行占 y 244–278，减 38px 标题栏得 206–240。
#:
#: 上下各留了几像素余量，这不是随手给的：收窄到 210–234 之后，7× 那档就读不出东西了，
#: 只有 3× 还能读——一行字的上下留白本身就是 tesseract 切行的依据。
SCOUT_INTRO_LINE_ROI = Region(725, 206, 1195, 240)

#: 坐标合法性：银河 1–9、恒星系 1–499、位号 1–20（`domain.scan_bounds` 的既有事实）。
#: 数字白名单读出来的候选里混着噪声，靠这个范围把 `382:137:4` 这种挡掉。
MAX_GALAXY = TOTAL_GALAXIES
MAX_SYSTEM = SYSTEMS_PER_GALAXY
MAX_POSITION_VALUE = MAX_POSITION

#: 战舰清单的横向范围与可读的纵向范围（拖到底那一屏，879 空间）。
SCOUT_SHIP_BAND = ColumnBand(720, 1210)
SCOUT_SHIP_TOP = 200
SCOUT_SHIP_BOTTOM = 762

#: 数量列的横向范围。**写死，不现场量。**
#:
#: ⚠️ 这是一次真实事故的补丁（2026-08-09）：原先用 `number_column()` 现场量，
#: 而这份清单**整列都是 0** 时每格只有一个窄窄的 `0`，够宽的墨迹段只剩下面板左边
#: 那层水印（`-17003` / `COMMAND OFFICERS`）——量出来的「数字列」是 (731, 808)，
#: 于是读到的「数量」是水印里的数字。判定因此把一个四项全 0 的海盗当成有舰队，
#: **真的打了一发出去**。日志里同一封报告两次读成 `{'噬能截击者': 8}` 与
#: `{'深空吞噬者': 2}`，前后不一致本身就是这个错的指纹。
#:
#: 这一列右对齐、贴着面板右沿，位置本来就是固定的；写死既准又挡住了水印。
#: 左界给到 1090 是为了容下 `5.36K` 这样的五字符读数（水印最右到 x≈950）。
SCOUT_COUNT_BAND = (1090, 1195)


class ScoutReportUnreadable(RuntimeError):
    """这一屏读不出判定所需的东西。判定输入不全时**不许猜**——
    猜错的方向不是「白打一发」，而是「把舰队送去挨打」。"""


@dataclass(frozen=True)
class PirateScoutReading:
    """一份海盗侦察报告里，判定用得上的那部分。"""

    raw_time_text: str
    reported_at_utc: datetime
    origin: Coordinate
    target: Coordinate
    #: 只含 `PIRATE_TRIGGER_SHIPS` 那四个舰种，而且**可能不全**。
    #: 不是对方的全部舰队，也不要拿它当舰队快照存。
    trigger_ships: dict[str, int]
    #: 这四个里没读出来的那些。数量为 0 的格子是一个孤零零的 `0`，
    #: 实测最容易读空——**读空绝不能当成 0**。
    missing: tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        """打、不打、还是不下结论。判据见 `domain.scout_verdict.verdict_for`。

        规则住在 domain，是因为它还有第二个消费者：仓储要按库里那份侦察报告
        回答「这个海盗走到哪一步了」（`domain.pirate_round`），而 storage
        不能 import vision。两边各写一份，就会出现「界面上说不值得打、
        而链路当时判的是没看清」这种谁也说不清的分叉。
        """
        return verdict_for(self.trigger_ships, unread=self.missing)

    @property
    def worth_attacking(self) -> bool:
        return self.verdict == VERDICT_ATTACK


class ScoutReportScreens(Protocol):
    def report_header(self) -> str: ...

    def scout_intro_texts(self) -> list[str]: ...

    def named_counts(
        self,
        wanted: tuple[str, ...],
        band: ColumnBand,
        top: int,
        bottom: int,
        *,
        count_band: tuple[int, int] | None = ...,
    ) -> dict[str, int]: ...


def parse_intro_coordinates(texts: Sequence[str]) -> tuple[Coordinate, Coordinate] | None:
    """从开头那行的若干候选读法里挑出「从哪打到哪」；挑不出返回 None。

    原文形如：`你从[2:137:18]奥格瑞玛派出的侦察探测器已对[2:137:4] Alien Brood完成侦察`，
    出发在前、目标在后——这是句子结构决定的，不是巧合。

    判据是「**恰好**两个坐标，且都在合法范围内」。三个都要：

    - 不是「取前两个」：噪声读法 `2:137:18 382:137:4 3` 也有两个，但第二个是脏的。
      **多出来的东西说明这一遍读脏了，整遍作废**，而不是从脏读里挑前两个。
    - 范围检查挡住 `382:137:4` 这类把中文笔画并进数字的读法。
    - 一遍不合格就换下一遍配方——只要有一遍干净，就不必在脏读上冒险。
    """
    for text in texts:
        found = [_coordinate(match.group(0)) for match in COORDINATE_RE.finditer(text)]
        valid = [item for item in found if item is not None]
        if len(found) == 2 and len(valid) == 2:
            return (valid[0], valid[1])
    return None


def read_pirate_scout(
    header: ScoutReportScreens,
    ships: ScoutReportScreens,
    *,
    expected_target: Coordinate | None = None,
) -> PirateScoutReading:
    """读一份侦察报告。`header` 是未滚动那屏，`ships` 是拖到底那屏。

    `expected_target` 给了就核对：报告里的目标必须正是刚侦察的那一位。
    不核对的话，一份**上一轮**的侦察报告会让这一轮朝没侦察过的坐标打出去——
    而信箱是按时间倒序的，看起来永远像是「最新的那封」。
    """
    subject = _subject_of(header.report_header())
    if subject is None or classify_report_subject(subject) is not ReportKind.SCOUT:
        raise ScoutReportUnreadable(f"这不是侦察报告：主题读作 {subject!r}")

    raw_time = _time_text_of(header.report_header())
    reported_at = (
        parse_report_timestamp(raw_time, GAME_DISPLAY_ZONE) if raw_time is not None else None
    )
    if raw_time is None or reported_at is None:
        raise ScoutReportUnreadable("报告头里没有可读的时间")

    coordinates = parse_intro_coordinates(header.scout_intro_texts())
    if coordinates is None:
        raise ScoutReportUnreadable("开头那行里读不出干净的出发与目标坐标")
    origin, target = coordinates
    if expected_target is not None and target != expected_target:
        raise ScoutReportUnreadable(
            f"这份报告的目标是 {target}，不是刚侦察的 {expected_target}；拒绝据此判定"
        )

    counts = ships.named_counts(
        PIRATE_TRIGGER_SHIPS,
        SCOUT_SHIP_BAND,
        SCOUT_SHIP_TOP,
        SCOUT_SHIP_BOTTOM,
        count_band=SCOUT_COUNT_BAND,
    )
    missing = tuple(name for name in PIRATE_TRIGGER_SHIPS if name not in counts)
    if len(missing) == len(PIRATE_TRIGGER_SHIPS):
        # 四个一个都没读到，说明这一屏根本不是战舰清单（多半没拖到底）。
        # 部分缺失则交给 `verdict` 去判——缺的那几格可能压根不影响结论。
        raise ScoutReportUnreadable("这一屏读不到任何判定舰种；多半是没拖到底")

    return PirateScoutReading(
        raw_time_text=raw_time,
        reported_at_utc=reported_at,
        origin=origin,
        target=target,
        trigger_ships=counts,
        missing=missing,
    )


def _coordinate(text: str) -> Coordinate | None:
    """把 `2:137:4` 解析成坐标；超出合法范围就当没读到。"""
    digits = [part for part in text.strip("[]").split(":") if part.isdigit()]
    if len(digits) != 3:
        return None
    galaxy, system, position = (int(part) for part in digits)
    if not 1 <= galaxy <= MAX_GALAXY:
        return None
    if not 1 <= system <= MAX_SYSTEM:
        return None
    if not 1 <= position <= MAX_POSITION_VALUE:
        return None
    return Coordinate(galaxy, system, position)


def _subject_of(header: str) -> str | None:
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("主题"):
            return stripped.split(":", 1)[-1].split("：", 1)[-1].strip() or None
    return None


def _time_text_of(header: str) -> str | None:
    from evo_helper.vision.parsers import REPORT_TIME_RE

    match = REPORT_TIME_RE.search(header)
    return match.group(0) if match is not None else None


__all__ = [
    "SCOUT_INTRO_LINE_ROI",
    "SCOUT_SHIP_BAND",
    "SCOUT_SHIP_BOTTOM",
    "SCOUT_SHIP_TOP",
    "VERDICT_ATTACK",
    "VERDICT_SKIP",
    "VERDICT_UNREADABLE",
    "PirateScoutReading",
    "ScoutReportUnreadable",
    "parse_intro_coordinates",
    "read_pirate_scout",
]
