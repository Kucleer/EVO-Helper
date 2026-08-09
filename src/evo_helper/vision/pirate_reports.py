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

**拖到底是可标定的姿势**：实测同一份报告拖 280px 与拖 520px 落点完全一致
（面板夹到底了），所以「损失单位」相对「战斗详情」横幅的偏移是固定的，
`ImageReportScreens.loss_totals()` 据此定位。

这条链路刻意不复用 `LiveReportReader`：那边要求参战两列非空（这里根本不读），
而且「海盗攻击报告」在 `classify_report_subject` 里不可与派遣匹配，会被整份拒收。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from evo_helper.domain.models import Coordinate
from evo_helper.domain.text import snap_to_vocabulary
from evo_helper.vision.parsers import (
    GAME_DISPLAY_ZONE,
    ReportKind,
    classify_report_subject,
    parse_report_timestamp,
    parse_versus_block,
)
from evo_helper.vision.report_layout import Region

#: 胜负横幅的两个取值。库里存英文原文——它就是游戏画面上的字，不做翻译，
#: 免得「界面中文」与「库里中文」哪天不一致时说不清存的是哪个。
OUTCOME_VICTORY = "VICTORY"
OUTCOME_FAIL = "FAIL"
OUTCOME_LABELS = (OUTCOME_VICTORY, OUTCOME_FAIL)

#: 胜负横幅所在的 ROI（标定视口 1920×879，即整窗截图裁掉 38px 标题栏之后）。
#: 实机量于 2026-08-09：整窗 917 空间里 `VICTORY` 占 y 278–322，减 38 得 240–284。
OUTCOME_ROI = Region(800, 232, 1130, 292)


#: 横幅吸附容差：按词长的三分之一取整。`VICTORY` 允许错两个字母、`FAIL` 允许错一个。
#: 这行大字是半透明的、压在星空背景上，实测会掉字母（`VICTORV`）。
#: 不能放得更宽：容差一旦超过一半词长，`FAIL` 和别的四字母噪声就分不开了。
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


class PirateReportScreens(Protocol):
    """海盗战报要用到的取字面。前三个来自未滚动那屏，`loss_totals` 来自拖到底那屏。"""

    def report_header(self) -> str: ...

    def versus_block(self) -> str: ...

    def outcome_banner(self) -> str: ...

    def unit_totals(self) -> tuple[str, str]: ...

    def loss_totals(self) -> tuple[str, str]: ...


def parse_outcome(raw: str) -> str | None:
    """把横幅文字贴回 `VICTORY` / `FAIL`；贴不上返回 None。

    只取字母：OCR 会在大字周围带出星空的碎点（`VICTORY .`）。
    """
    letters = "".join(char for char in raw.upper() if char.isalpha())
    return snap_to_vocabulary(letters, OUTCOME_LABELS, max_distance=OUTCOME_TOLERANCE)


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

    outcome = parse_outcome(detail.outcome_banner())
    if outcome is None:
        raise PirateReportUnreadable("读不出胜负横幅（VICTORY / FAIL）")

    losses = _totals(bottom.loss_totals())
    if losses is None:
        raise PirateReportUnreadable("读不出战损总数")

    units = _totals(detail.unit_totals())
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
    )


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
    from evo_helper.domain.fleet_tier import parse_fleet_count

    left = parse_fleet_count(texts[0]) if texts[0] else None
    right = parse_fleet_count(texts[1]) if texts[1] else None
    if left is None or right is None:
        return None
    return (left, right)


__all__ = [
    "OUTCOME_FAIL",
    "OUTCOME_LABELS",
    "OUTCOME_ROI",
    "OUTCOME_VICTORY",
    "PirateReportReading",
    "PirateReportUnreadable",
    "parse_outcome",
    "read_pirate_report",
]
