"""海盗攻击报告的轻量读取：只要**胜负**与**战损总数**。

用户口径（2026-08-09）：海盗战报只记胜利/失败与战损总数，不记逐舰种明细。
理由是性能——明细要进回放页、读两列名称与数量、还要反复重拍直到合计对上，
一份报告要两三秒；而海盗全是同一个预设打的，明细没有分析价值。
战损也只要总数，不要按舰种拆。

于是这条链路只看**详情页**，两屏：

- **未滚动**：主题、报告时间、VS 块（双方坐标）、`VICTORY`/`FAIL` 横幅、「单位」总数
- **拖到底**：「损失单位」总数

为什么要两屏：未滚动时「损失单位」那一行正好被面板下沿切掉，读出来的是半行字；
而拖到底之后 `VICTORY` 横幅又滚出了可视区。两样东西不在同一屏上。

⚠️ **胜负以横幅为准**（用户口径 2026-08-17：「游戏算法更新，剩余舰艇算法已经
不准了，可以读 victory」）。横幅读不出来才回落到按剩余舰艇数算
（`domain.battle_outcome`）。仲裁与日志都在 `decide_outcome` 里。

「整份拒收」的判据跟着变成**两条路都定不出胜负**才拒——这条记录的全部内容就是
胜负与战损，缺了没有存的价值；但只要横幅读得出来，四个数缺几个都不影响。

**拖到底是可标定的姿势**：实测同一份报告拖 280px 与拖 520px 落点完全一致
（面板夹到底了），所以「损失单位」相对「战斗详情」横幅的偏移是固定的，
`ImageReportScreens.loss_totals()` 据此定位。

这条链路刻意不复用 `LiveReportReader`：那边要求参战两列非空（这里根本不读），
而且「海盗攻击报告」在 `classify_report_subject` 里不可与派遣匹配，会被整份拒收。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from evo_helper.domain.battle_outcome import (
    OUTCOME_DRAW,
    OUTCOME_FAIL,
    OUTCOME_LABELS,
    OUTCOME_VICTORY,
    outcome_from_totals,
)
from evo_helper.domain.battle_resources import parse_resource_grid
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import BattleResourceEntry
from evo_helper.domain.text import snap_to_vocabulary
from evo_helper.vision.parsers import (
    GAME_DISPLAY_ZONE,
    ReportKind,
    classify_report_subject,
    parse_report_timestamp,
    parse_versus_block,
)
from evo_helper.vision.report_layout import Region

logger = logging.getLogger(__name__)

#: 胜负横幅所在的 ROI（标定视口 1920×879，即整窗截图裁掉 38px 标题栏之后）。
#: 实机量于 2026-08-09：整窗 917 空间里 `VICTORY` 占 y 278–322，减 38 得 240–284。
#:
#: 2026-08-11 在 7 张实拍上复量了一遍横幅墨迹的外接框（判据见
#: `optional.report_screens.outcome_banner`）：`FAIL` 占整窗 x 907–1030、
#: y 281–310，`VICTORY` 占 x 844–1084、y 281–313，七张**逐像素一致**。
#: 减去标题栏就是 y 243–275，两侧都落在这个框里，所以框不用动——
#: 当年读不出来的原因不在几何，在那层压着的幽灵文字。
OUTCOME_ROI = Region(800, 232, 1130, 292)


#: 横幅吸附容差：按词长的三分之一取整，取全表最大的那一档（`VICTORY` 的 2）。
#: 这行大字是半透明的、压在星空背景上，实测会掉字母（`VICTORV`）。
#: 不能放得更宽：容差一旦超过一半词长，四字母的那两档就和别的四字母噪声分不开了。
#:
#: ⚠️ 2026-08-17 起横幅是**第一判据**（见 `decide_outcome`），吸错的后果重新变回
#: 「库里记错一场战果」。这更是不放松容差的理由：宁可吸不上、回落到算式，
#: 也不要把一段噪声吸成 `FAIL`。
#:
#: **这不是偏好项**：取值由 OCR 读出的字形与词表长度决定，调了就是错。
def _tolerance(label: str) -> int:
    return max(1, round(len(label) / 3))


OUTCOME_TOLERANCE = max(_tolerance(label) for label in OUTCOME_LABELS)


class PirateReportUnreadable(RuntimeError):
    """这一屏读不出海盗战报该有的内容。

    **一律整份拒收**，不存半份：这条记录的全部内容就是胜负与战损，
    缺哪一样都让剩下的东西失去意义——而一条看起来像数据的残缺记录，
    没有人会再回头核。
    """


@dataclass(frozen=True)
class PirateReportReading:
    """一份海盗战报的轻量读数。**没有** `fleet` 字段，这是故意的。"""

    raw_time_text: str
    reported_at_utc: datetime
    attacker_origin: Coordinate
    defender_target: Coordinate
    attacker_name: str | None
    defender_name: str | None
    outcome: str
    attacker_losses: int
    defender_losses: int
    attacker_units: int | None = None
    defender_units: int | None = None
    #: 「获得资源」那 12 格里**非零**的几格。空元组既可能是白打一发、也可能是
    #: 那一屏没读全（后者留 warning）——判据见 `domain.battle_resources`。
    #:
    #: 这一项**不进「整份拒收」的判据**：这条链路的存在理由是胜负与战损，
    #: 收获读不出来时那两样照样有意义，拒收等于因为附加项丢掉主产物。
    resources: tuple[BattleResourceEntry, ...] = ()


class PirateReportScreens(Protocol):
    """海盗战报要用到的取字面。前三个来自未滚动那屏，`loss_totals` 来自拖到底那屏。"""

    def report_header(self) -> str: ...

    def versus_block(self) -> str: ...

    def outcome_banner(self) -> str: ...

    def unit_totals(self) -> tuple[str, str]: ...

    def loss_totals(self) -> tuple[str, str]: ...


def parse_outcome(raw: str) -> str | None:
    """把横幅文字贴回 `VICTORY` / `FAIL` / `DRAW`；贴不上返回 None。

    只取字母：OCR 会在大字周围带出星空的碎点（`VICTORY .`）。

    ⚠️ **这是第一判据**（用户口径 2026-08-17）。贴不上时由 `decide_outcome`
    回落到按剩余舰艇数算的结果——贴错比贴不上坏得多，所以容差不许放松。
    """
    letters = "".join(char for char in raw.upper() if char.isalpha())
    return snap_to_vocabulary(letters, OUTCOME_LABELS, max_distance=OUTCOME_TOLERANCE)


def decide_outcome(banner_text: str | None, computed: str | None, *, where: str) -> str | None:
    """定这一仗算赢算输：**以画面横幅为准**，横幅读不出来才回落到算式。

    用户口径（2026-08-17）：

    > 游戏算法更新，剩余舰艇算法已经不准了，可以读 victory

    所以优先级和 2026-08-11 那版**正好相反**：横幅是游戏自己给的结论，
    「剩余 = 单位 − 损失单位」推出来的那个降级成参考。

    ## 这不是配置项

    「以横幅为准」由游戏的战斗结算算法决定，不由用户的处境决定：没有哪种场景下
    「宁可信一套已知不准的算术」会让结果更合用，只会让它更错。所以硬编码，
    不留开关（仓库口径：改这个值会让结果变「错」的，就是常量而不是旋钮）。

    ## 三条出路，每条都留得下痕迹

    1. 横幅读得出、和算式打架 → 用横幅，留 warning（说清两边各是什么、原文是什么）。
    2. 横幅读不出、算式算得出 → **回落**，留 warning 说明这一条是回落来的。
       不许静默：回落值现在是「已知会不准」的那一套，日后核账要认得出哪些是它。
    3. 两条都不成 → 返回 None。**绝不猜**，也绝不拿另一档顶替——
       「没定出胜负」和「打输了」在下游完全不同（见 `domain.records.BattleReport`）。

    `banner_text` 传 `None` 表示**这一屏根本没有横幅取字面**（离线入库、旧截图、
    以及没实现 `outcome_banner` 的取字面对象），这是结构性缺席、不是读失败，
    所以静默回落、不刷 warning。传 `""` 或噪声才算「读了但读不出」。

    日志频率：这三条都**每份战报最多一条**，不在 tick 循环里，不用限流。

    两边一致时什么都不说：那是常态，为常态刷日志等于把这几条变成噪声。
    """
    banner = parse_outcome(banner_text) if banner_text is not None else None
    if banner is not None:
        if computed is not None and computed != banner:
            logger.warning(
                "%s：画面横幅读作 %s（原文 %r），按剩余舰艇数算出的却是 %s；"
                "以横幅为准（游戏算法更新后剩余舰艇数已不可信，用户口径 2026-08-17）",
                where,
                banner,
                banner_text.strip() if banner_text is not None else "",
                computed,
            )
        return banner
    if banner_text is not None and computed is not None:
        logger.warning(
            "%s：画面横幅读不出战果（原文 %r），回落到按剩余舰艇数算出的 %s；"
            "这一条是回落来的，剩余舰艇数在游戏算法更新后已不保证准确",
            where,
            banner_text.strip(),
            computed,
        )
    return computed


def read_pirate_report(
    detail: PirateReportScreens,
    bottom: PirateReportScreens,
    *,
    source: str = "ocr",
) -> PirateReportReading:
    """读一份海盗战报。`detail` 是未滚动那屏，`bottom` 是拖到底那屏。

    两个参数允许是同一个对象（测试里就是），生产链路上是两张截图各一个实例——
    同一个实例读两屏会把上一屏的像素当成这一屏，这个坑在别处踩过。
    """
    header = detail.report_header()
    subject = _subject_of(header)
    if subject is None or classify_report_subject(subject) is not ReportKind.PIRATE:
        raise PirateReportUnreadable(f"这不是海盗攻击报告：主题读作 {subject!r}")

    raw_time = _time_text_of(header)
    reported_at = (
        parse_report_timestamp(raw_time, GAME_DISPLAY_ZONE) if raw_time is not None else None
    )
    if raw_time is None or reported_at is None:
        raise PirateReportUnreadable("报告头里没有可读的时间")

    versus = parse_versus_block(detail.versus_block(), source)
    if versus is None:
        raise PirateReportUnreadable("VS 块不完整；拒收单边战报，免得挂到错的目标上")

    losses = _totals(bottom.loss_totals())
    if losses is None:
        raise PirateReportUnreadable("读不出战损总数")

    units = _totals(detail.unit_totals())
    # 胜负**以横幅为准**（用户口径 2026-08-17），算出来的只当横幅读不出来时的兜底。
    computed = outcome_from_totals(
        attacker_units=units[0] if units is not None else None,
        attacker_losses=losses[0],
        defender_units=units[1] if units is not None else None,
        defender_losses=losses[1],
    )
    outcome = decide_outcome(detail.outcome_banner(), computed, where=f"海盗战报 {raw_time}")
    if outcome is None:
        raise PirateReportUnreadable(
            "定不出胜负：画面横幅读不出战果，按剩余舰艇数也算不出来"
            "（「单位」与「损失单位」四个数缺了至少一个）"
        )

    return PirateReportReading(
        raw_time_text=raw_time,
        reported_at_utc=reported_at,
        attacker_origin=versus.attacker.coordinate.value,
        defender_target=versus.defender.coordinate.value,
        attacker_name=versus.attacker.player,
        defender_name=versus.defender.player,
        outcome=outcome,
        attacker_losses=losses[0],
        defender_losses=losses[1],
        attacker_units=units[0] if units is not None else None,
        defender_units=units[1] if units is not None else None,
        # 收获读的是**未滚动那一屏**（`detail`）：拖到底之后这一块整个滚出可视区。
        resources=_resources(detail, where=f"海盗战报 {raw_time}"),
    )


def _resources(detail: PirateReportScreens, *, where: str) -> tuple[BattleResourceEntry, ...]:
    """「获得资源」那 12 格。读不全就一格都不要——**绝不补 0**。

    用 getattr 取而不是写进 `PirateReportScreens` 协议，与 `live_reports._resources`
    同一个理由：写进协议会打断所有既有实现，而收获是增强项。
    """
    reader = getattr(detail, "resource_cells", None)
    if reader is None:
        return ()
    entries = parse_resource_grid(reader())
    if entries is None:
        logger.warning("%s 的「获得资源」没读全；这一份不记收获，也不补 0", where)
        return ()
    return entries


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


def _totals(texts: tuple[str, str]) -> tuple[int, int] | None:
    """双方各一个数；任一读不出就返回 None（由调用方决定拒收还是留空）。"""
    from evo_helper.domain.fleet_counts import parse_fleet_count

    left = parse_fleet_count(texts[0]) if texts[0] else None
    right = parse_fleet_count(texts[1]) if texts[1] else None
    if left is None or right is None:
        return None
    return (left, right)


__all__ = [
    "OUTCOME_DRAW",
    "OUTCOME_FAIL",
    "OUTCOME_LABELS",
    "OUTCOME_ROI",
    "OUTCOME_VICTORY",
    "PirateReportReading",
    "PirateReportUnreadable",
    "decide_outcome",
    "parse_outcome",
    "read_pirate_report",
]
