"""Read the live mail list -> attack report -> battle replay chain.

The reader is deliberately free of screenshots, ROI geometry and OCR. It asks a
:class:`ReportScreens` implementation for the text of each named region, so the
navigation and safety rules stay testable without a browser, and the geometry
lives with the adapter that owns the window.

Every step fails closed. An unknown UI version, a half-rendered panel, a
missing side or an unreadable time raises instead of yielding a partial report:
a report that is silently wrong closes the wrong dispatch.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from evo_helper.domain.battle_outcome import outcome_from_totals
from evo_helper.domain.battle_resources import parse_resource_grid
from evo_helper.domain.records import BattleResourceEntry
from evo_helper.vision.models import (
    FleetLine,
    PageObservation,
    ReplayRound,
    VersusBlock,
    VersusSide,
)
from evo_helper.vision.parsers import (
    GAME_DISPLAY_ZONE,
    ReportKind,
    UnknownUiVersionError,
    classify_report_subject,
    parse_fleet_column,
    parse_mail_rows_v2,
    parse_replay_rounds,
    parse_report_timestamp,
    parse_versus_block,
)

logger = logging.getLogger(__name__)

#: 战斗详情页的界面版本标签。单拎成常量是因为**取图那一侧也要用它**：
#: 活链路自己造 `PageObservation` 时得填这个字，抄一份字面量迟早两边分家。
DETAIL_UI_VERSION = "battle-detail-v2"

SUPPORTED_DETAIL_VERSIONS = frozenset({DETAIL_UI_VERSION})
SUPPORTED_REPLAY_VERSIONS = frozenset({"battle-replay-v2"})


@dataclass(frozen=True)
class AttackReportRow:
    """A mail row that is eligible to be opened as an attack report."""

    subject: str
    sender: str | None
    raw_time_text: str
    reported_at_utc: datetime


@dataclass(frozen=True)
class ReadTiming:
    """How long one report took to read, split by stage.

    A single total says a read was slow; the per-stage split says which OCR
    call to attack. Recorded for failed reads too — a read that dies after
    thirty seconds is exactly what this is meant to surface.
    """

    stages: tuple[tuple[str, float], ...] = ()
    total_seconds: float = 0.0

    @property
    def slowest(self) -> tuple[str, float]:
        return max(self.stages, key=lambda item: item[1], default=("none", 0.0))

    def summary(self) -> str:
        parts = ", ".join(f"{name} {seconds:.2f}s" for name, seconds in self.stages)
        return f"{self.total_seconds:.2f}s ({parts})" if parts else f"{self.total_seconds:.2f}s"


class _StageTimer:
    """Accumulates per-stage elapsed time from an injected clock."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._started = clock()
        self._mark = self._started
        self._stages: list[tuple[str, float]] = []

    def stage(self, name: str) -> None:
        now = self._clock()
        self._stages.append((name, now - self._mark))
        self._mark = now

    def finish(self) -> ReadTiming:
        return ReadTiming(stages=tuple(self._stages), total_seconds=self._clock() - self._started)


@dataclass(frozen=True)
class LiveBattleReport:
    """A fully read attack report, ready for strict dispatch matching."""

    kind: ReportKind
    raw_time_text: str
    reported_at_utc: datetime
    attacker: VersusSide
    defender: VersusSide
    participating_attacker: tuple[FleetLine, ...]
    participating_defender: tuple[FleetLine, ...]
    rounds: tuple[ReplayRound, ...]
    #: Per-screen UI versions. Section 3 forbids one label for the whole chain.
    ui_versions: dict[str, str]
    #: 战斗详情页的「单位」总数，双方各一。**不是** participating 之和——
    #: 大舰队的数量是四舍五入显示（`5.36K`），相加凑不出精确总数。
    #: 读不到时为 None，绝不用明细之和顶替。
    attacker_units: int | None = None
    defender_units: int | None = None
    #: 详情页的「损失单位」总数，双方各一。它是 `outcome` 的**输入之一**，
    #: 不只是展示字段：剩余 = 单位 − 损失单位。
    #: 这一行要把详情页拖到底才读得到，所以缺席是常态。
    attacker_losses: int | None = None
    defender_losses: int | None = None
    #: `VICTORY` / `FAIL` / `DRAW`，**算出来的**，不是从横幅读的：
    #: 本方剩余 0 判负、对方被全歼判胜、两边都有船判平
    #: （用户口径 2026-08-11，判据在 `domain.battle_outcome`）。
    #:
    #: ⚠️ **算不出就是 None，绝不拿别的东西顶替。** 「没算出胜负」和「打输了」
    #: 在下游完全不同：后者会进攻击日志的战果列，显示成一场根本没读过的败仗。
    outcome: str | None = None
    #: 「获得资源」那 12 格里**非零**的几格（用户口径 2026-08-17：只统计这 12 个值）。
    #:
    #: ⚠️ **空元组有两种来源**：12 格全是 0（白打一发），以及那一屏没读全。
    #: 后者会留一条 warning，但这个字段本身分不开——分得开的判据在
    #: `domain.battle_resources.parse_resource_grid`，它读不全就一格都不给，
    #: 免得把「没读到」当成 0 传下去。
    resources: tuple[BattleResourceEntry, ...] = ()
    #: How long this report took to read, split by stage.
    timing: ReadTiming = field(default_factory=ReadTiming)


class ReportScreens(Protocol):
    """Per-region OCR text for the screen the adapter is currently showing."""

    def mail_rows(self) -> list[str]:
        """One string per mail row: subject, sender and timestamp lines."""
        ...

    def report_header(self) -> str:
        """The 发件人 / 主题 header block of an opened report."""
        ...

    def versus_block(self) -> str:
        """The two-column VS block, columns separated by run-of-spaces."""
        ...

    def participating_columns(self) -> tuple[str, str]:
        """The 参战战舰 attacker and defender columns."""
        ...

    def round_columns(self) -> list[tuple[int, str, str]]:
        """``(round_number, attacker_column, defender_column)`` per round."""
        ...


class LiveReportReader:
    def __init__(
        self,
        screens: ReportScreens,
        source: str = "ocr",
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._screens = screens
        self._source = source
        self._clock = clock

    def list_attack_reports(self, page: PageObservation) -> tuple[AttackReportRow, ...]:
        """Return only the rows that may be matched against a dispatch.

        ``海盗攻击报告`` contains ``攻击报告`` as a substring but is a pirate
        battle, and the live secondary tabs do not filter by report type, so the
        subject is the only thing separating them.
        """
        observation = parse_mail_rows_v2(
            page, self._screens.mail_rows(), GAME_DISPLAY_ZONE, self._source
        )
        rows: list[AttackReportRow] = []
        for item in observation.items:
            if not classify_report_subject(item.subject).is_dispatch_matchable:
                continue
            if item.raw_time_text is None:
                continue
            reported_at = parse_report_timestamp(item.raw_time_text, GAME_DISPLAY_ZONE)
            if reported_at is None:
                continue
            rows.append(
                AttackReportRow(
                    subject=item.subject,
                    sender=item.owner.value if item.owner is not None else None,
                    raw_time_text=item.raw_time_text,
                    reported_at_utc=reported_at,
                )
            )
        return tuple(rows)

    def read_report(
        self, detail_page: PageObservation, replay_page: PageObservation
    ) -> LiveBattleReport:
        timer = _StageTimer(self._clock)
        try:
            report = self._read_report(detail_page, replay_page, timer)
        except Exception as error:
            timing = timer.finish()
            logger.warning("read attack report failed after %s: %s", timing.summary(), error)
            raise
        logger.info(
            "read attack report in %s; slowest stage %s",
            report.timing.summary(),
            report.timing.slowest[0],
        )
        return report

    def read_detail_only(
        self, detail_page: PageObservation, *, bottom: object | None = None
    ) -> LiveBattleReport:
        """只读**战斗详情页**：报告时间、双方、「单位」与「损失单位」，据此算胜负。

        与 `read_report` 的差别是 `participating_*` 与 `rounds` 一律为空——
        因为那些东西**不在详情页上**：参战战舰那两列的行界（`ReportLayout.
        participating_rows`）是对着**回放页**量的，`tools.ingest_report` 也是从
        replay 那一屏取的。详情页上同一段 y 坐标正压着 VS 块。

        所以「只读详情页」换掉的不是几次 OCR，是**整整一屏**：要拿到逐舰种明细
        就得点开「查看战斗回放」，而那个按钮至今没有标定过的点击坐标，一份报告
        还要多花两三秒。取舍的先例就在仓库里——海盗战报刻意只记胜负与战损总数
        （用户口径 2026-08-09，为省性能，见 `vision.pirate_reports` 模块头）。

        ⚠️ **空就是空，不拿别的东西顶替。** 「没读逐舰种明细」和「对方一艘船都
        没有」在下游长得一模一样，而后者会直接进情报中心。这条与
        `attacker_units`、`outcome` 那两条注释是同一个规矩：读不到就留空。

        其余判据一条不放松：主题必须是可匹配派遣的攻击报告、时间必须读得出、
        VS 块必须两边都全——单边战报会被挂到错的目标上。

        ## 胜负是**算**出来的

        剩余 = 单位 − 损失单位；本方剩余 0 判负、对方被全歼判胜、两边都有船判平
        （用户口径 2026-08-11，判据在 `domain.battle_outcome`）。画面上那行
        `VICTORY` / `FAIL` 大字只做交叉校验，两边都算得出而结论不一致时留一条
        warning——用户明确说了不看游戏内的提示，横幅没有推翻算式的资格。

        代价说在前面：**四个数缺一个就判不出**，而「损失单位」那一行恰恰要拖到底
        才读得到（见下）。所以没有第二屏时 `outcome` 多半是 None，这是诚实的空，
        不是故障。

        ## `bottom`：拖到底之后的那一屏

        bot 战报比海盗战报**多一行**「生成卫星概率」，「战斗详情」横幅因此下移约
        30px，「单位」那一行整个落到面板可视区之外。2026-08-11 的五张实拍里
        四张如此（第五张恰好没有那一行，「单位」就读得出来，读数 `1` / `319`），
        所以这不是定位错、是那一行**根本没画出来**——只能拖。
        「损失单位」在它下面一行，更是**只有**拖到底才读得到。

        这一步因此从「补一个展示字段」变成了**判据的输入**：不拖就没有战损，
        没有战损就算不出胜负。

        ⚠️ **时间与 VS 块必须仍旧从没拖过的那一屏读**——拖到底之后它们都滚出了
        可视区。所以两屏各有各的用处，不能互相顶替：
        「单位」先问第一屏、读不到再问第二屏；「损失单位」只在第二屏上。
        """
        timer = _StageTimer(self._clock)
        self._require_version(detail_page, SUPPORTED_DETAIL_VERSIONS, "battle detail")
        kind, raw_time, reported_at = self._header_facts(timer)
        versus = self._versus(timer)
        attacker_units, defender_units = self._unit_totals(self._screens)
        if bottom is not None and attacker_units is None and defender_units is None:
            attacker_units, defender_units = self._unit_totals(bottom)
        # 「损失单位」只在拖到底那一屏上（没拖时被面板下沿切掉，读出来是半行字）。
        # 没给第二屏就问第一屏——多半读不到，那就诚实地留空。
        attacker_losses, defender_losses = self._loss_totals(
            bottom if bottom is not None else self._screens
        )
        outcome = outcome_from_totals(
            attacker_units=attacker_units,
            attacker_losses=attacker_losses,
            defender_units=defender_units,
            defender_losses=defender_losses,
        )
        self._cross_check_banner(outcome, raw_time)
        timer.stage("outcome")
        resources = self._resources(self._screens, where=raw_time)
        timer.stage("resources")
        return LiveBattleReport(
            kind=kind,
            raw_time_text=raw_time,
            reported_at_utc=reported_at,
            attacker=versus.attacker,
            defender=versus.defender,
            participating_attacker=(),
            participating_defender=(),
            rounds=(),
            # 只有详情页这一屏的版本。**不填回放页的**：版本标签是「这一屏长什么
            # 样」的凭据，而这条链路根本没看过那一屏。
            ui_versions={"battle_detail_ui_version": str(detail_page.ui_version)},
            attacker_units=attacker_units,
            defender_units=defender_units,
            attacker_losses=attacker_losses,
            defender_losses=defender_losses,
            outcome=outcome,
            resources=resources,
            timing=timer.finish(),
        )

    def _resources(self, screens: object, *, where: str) -> tuple[BattleResourceEntry, ...]:
        """「获得资源」那 12 格。读不全就一格都不要——**绝不补 0**。

        **接在读战报这一趟里**：这一块就在未滚动那一屏上（和 VS 块、存档截图
        同一屏像素），不额外开一次导航。

        用 getattr 取而不是写进 `ReportScreens` 协议，与 `_unit_totals` 同一个
        理由：写进去会打断所有既有的实现，而资源是增强项——提供不了的实现
        照样能读出一份完整报告。

        读不全时留一条 warning：交出去的空元组和「12 格全是 0」长得一样，
        不吭一声的话，这条链路哪天整块失灵都没人看得见。
        """
        reader = getattr(screens, "resource_cells", None)
        if reader is None:
            return ()
        entries = parse_resource_grid(reader())
        if entries is None:
            logger.warning("战报 %s 的「获得资源」没读全；这一份不记收获，也不补 0", where)
            return ()
        return entries

    def _header_facts(self, timer: _StageTimer) -> tuple[ReportKind, str, datetime]:
        """报告头上的三件事：类型、时间原文、UTC 时间。任一读不出就抛。"""
        header = self._screens.report_header()
        timer.stage("header")
        subject = _subject_from_header(header)
        if subject is None:
            raise UnknownUiVersionError(
                "report header has no 主题 line; the panel is still rendering"
            )
        kind = classify_report_subject(subject)
        if not kind.is_dispatch_matchable:
            raise ValueError(f"not an attack report: {subject} ({kind.value})")

        raw_time = _time_text_from_header(header)
        reported_at = (
            parse_report_timestamp(raw_time, GAME_DISPLAY_ZONE) if raw_time is not None else None
        )
        if raw_time is None or reported_at is None:
            raise UnknownUiVersionError("report header has no readable time")
        return kind, raw_time, reported_at

    def _versus(self, timer: _StageTimer) -> VersusBlock:
        versus = parse_versus_block(self._screens.versus_block(), self._source)
        timer.stage("versus")
        if versus is None:
            raise UnknownUiVersionError("versus block is incomplete; refusing a one-sided report")
        return versus

    def _unit_totals(self, screens: object) -> tuple[int | None, int | None]:
        """「单位」总数是独立来源，读不到就留空——**绝不用明细之和顶替**。

        大舰队的逐行数量是四舍五入显示（`5.36K`），相加得出的「总数」是假的。

        用 getattr 取而不是写进协议：写进去会打断所有既有的 ReportScreens 实现，
        而总数是增强项——提供不了的实现照样能读出一份完整报告。
        """
        reader = getattr(screens, "unit_totals", None)
        totals = reader() if reader is not None else ("", "")
        return (_unit_count(totals[0]), _unit_count(totals[1]))

    def _loss_totals(self, screens: object) -> tuple[int | None, int | None]:
        """「损失单位」总数，双方各一。读不到就留空——它是胜负的输入，不能猜。

        与 `_unit_totals` 同一个 getattr 路子，理由也一样：写进协议会打断所有
        既有的 `ReportScreens` 实现。
        """
        reader = getattr(screens, "loss_totals", None)
        totals = reader() if reader is not None else ("", "")
        return (_unit_count(totals[0]), _unit_count(totals[1]))

    def _cross_check_banner(self, computed: str | None, where: str) -> None:
        """算出来的胜负与画面横幅对不上就留一条 warning。**只记不改。**

        用户明确说了不看游戏内的提示，所以横幅没有推翻算式的资格；但两者都读得出
        还不一致，说明其中一条链路坏了——那正是值得有人看一眼的事。

        **算不出胜负时连读都不读**：横幅那一次 OCR 只为校验，而这时没有东西可校，
        白花约 0.3 秒。bot 战报缺战损是常态，省下的正是常态那一路。

        **不在这里抛。** 海盗那条链路算不出胜负就整份拒收，因为那份记录**只有**
        胜负与战损；bot 这条链路的主业是「战报回来了没有」与守方单位数，
        为一个算不出的战果丢掉整份战报，会让目标重新卡回 `AWAITING_PROBE_REPORT`
        ——那正是这条链路上一次的死结。
        """
        if computed is None:
            return
        from evo_helper.vision.pirate_reports import cross_check_banner

        reader = getattr(self._screens, "outcome_banner", None)
        if reader is None:
            return
        cross_check_banner(computed, reader(), where=f"攻击战报 {where}")

    def _read_report(
        self,
        detail_page: PageObservation,
        replay_page: PageObservation,
        timer: _StageTimer,
    ) -> LiveBattleReport:
        self._require_version(detail_page, SUPPORTED_DETAIL_VERSIONS, "battle detail")
        self._require_version(replay_page, SUPPORTED_REPLAY_VERSIONS, "battle replay")

        kind, raw_time, reported_at = self._header_facts(timer)
        versus = self._versus(timer)

        attacker_text, defender_text = self._screens.participating_columns()
        timer.stage("fleet")
        participating_attacker = parse_fleet_column(attacker_text, self._source)
        participating_defender = parse_fleet_column(defender_text, self._source)
        if not participating_attacker and not participating_defender:
            raise UnknownUiVersionError(
                "participating fleet is empty on both sides; the replay had not rendered"
            )

        rounds = parse_replay_rounds(self._screens.round_columns(), self._source)
        timer.stage("rounds")

        attacker_units, defender_units = self._unit_totals(self._screens)
        attacker_losses, defender_losses = self._loss_totals(self._screens)
        outcome = outcome_from_totals(
            attacker_units=attacker_units,
            attacker_losses=attacker_losses,
            defender_units=defender_units,
            defender_losses=defender_losses,
        )
        self._cross_check_banner(outcome, raw_time)

        return LiveBattleReport(
            kind=kind,
            raw_time_text=raw_time,
            reported_at_utc=reported_at,
            attacker=versus.attacker,
            defender=versus.defender,
            participating_attacker=participating_attacker,
            participating_defender=participating_defender,
            rounds=rounds,
            ui_versions={
                "battle_detail_ui_version": str(detail_page.ui_version),
                "battle_replay_ui_version": str(replay_page.ui_version),
            },
            attacker_units=attacker_units,
            defender_units=defender_units,
            attacker_losses=attacker_losses,
            defender_losses=defender_losses,
            outcome=outcome,
            timing=timer.finish(),
        )

    @staticmethod
    def _require_version(page: PageObservation, supported: frozenset[str], screen: str) -> None:
        if page.ui_version not in supported:
            raise UnknownUiVersionError(f"unsupported {screen} UI version: {page.ui_version}")


def _subject_from_header(header: str) -> str | None:
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("主题"):
            return stripped.split(":", 1)[-1].split("：", 1)[-1].strip() or None
    return None


def _time_text_from_header(header: str) -> str | None:
    from evo_helper.vision.parsers import REPORT_TIME_RE

    match = REPORT_TIME_RE.search(header)
    return match.group(0) if match is not None else None


def _unit_count(text: str) -> int | None:
    """把「单位」读数解析成艘数；认不出返回 None。"""
    from evo_helper.domain.fleet_counts import parse_fleet_count

    return parse_fleet_count(text) if text else None
