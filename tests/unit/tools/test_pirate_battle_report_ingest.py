"""海盗攻击战报也要有人读——**这条链路以前一份都没读过**。

用户口径（2026-08-11）：「海盗攻击报告，你没有读取，海盗攻击成功（战斗判定都是
根据剩余舰艇），你需要记录对应的攻击任务完成。」

bot 那条链路的同一个死结刚修过（PR #91）：没人读战报 → `battle_reports` 里没有行
→ 攻击日志的战果列永远是「待战报」、「这一发打完了没有」永远落不了库。海盗这边
连读的代码都没有：`vision.pirate_reports.read_pirate_report` 一直只挂在离线入口
`tools.ingest_pirate_report`（要人手工喂两张截图）上，活链路从来不调它。

这里守的是**活链路那一侧的接线**：两屏怎么取、胜负从哪来、去重与拒收怎么落。
读法本身（四个数怎么算出胜负、横幅只作交叉校验）在
`tests/unit/vision/test_pirate_reports.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.domain.battle_outcome import OUTCOME_FAIL, OUTCOME_VICTORY
from evo_helper.domain.models import Coordinate
from evo_helper.tools.pirate_loop import MailRow, PirateLoop, ReportIngest
from evo_helper.vision.parsers import ReportKind

TARGET = Coordinate(2, 137, 4)
REPORTED_AT = datetime(2026, 8, 9, 4, 38, 46, tzinfo=UTC)

HEADER = "发件人: System        09/08/2026 04:38:46\n主题: 海盗攻击报告"
VERSUS = "Kucleer  Pirates\n奥格瑞玛  Alien Brood\n[2:137:18]  [2:137:4]"

ROW = MailRow(
    index=0,
    subject="海盗攻击报告",
    raw_time_text="09/08/2026 04:38:46",
    reported_at_utc=REPORTED_AT,
    kind=ReportKind.PIRATE,
)


class _Screens:
    """一屏详情页的取字面。生产上是 Pillow 裁剪 + Tesseract。"""

    def __init__(
        self,
        *,
        header: str = HEADER,
        units: tuple[str, str] = ("100", "783"),
        losses: tuple[str, str] = ("0", "783"),
    ) -> None:
        self._header = header
        self._units = units
        self._losses = losses

    def report_header(self) -> str:
        return self._header

    def versus_block(self) -> str:
        return VERSUS

    def outcome_banner(self) -> str:
        return "VICTORY"

    def unit_totals(self) -> tuple[str, str]:
        return self._units

    def loss_totals(self) -> tuple[str, str]:
        return self._losses


class _Repository:
    def __init__(self, *, already_stored: bool = False) -> None:
        self.already_stored = already_stored
        self.appended: list[Any] = []

    def has_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
        return self.already_stored

    def append_report(self, report: Any) -> None:
        self.appended.append(report)


def _loop(repository: _Repository, *, bottom: _Screens | None = None) -> tuple[Any, list[str]]:
    """一个只装了「读一封海盗战报」所需零件的 `PirateLoop`。"""
    events: list[str] = []
    loop = PirateLoop.__new__(PirateLoop)
    loop._ensure_run = lambda: (repository, None)
    loop._bottom_screens = lambda: (events.append("拖到底"), bottom or _Screens())[1]
    return loop, events


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    from evo_helper.tools import pirate_loop

    monkeypatch.setattr(pirate_loop, "say", lambda _line: None)


def test_a_readable_pirate_report_is_stored_with_its_outcome() -> None:
    """本文件的重点：读通了就落库，战果与战损一起落。

    攻击日志的战果列、以及「这一发打完了没有」都接在 `battle_reports` 上，
    靠 `append_report` 按坐标与时间认领那一发派遣。
    """
    repository = _Repository()
    loop, _events = _loop(repository)

    assert loop._ingest_report(ROW, _Screens()) is ReportIngest.STORED
    (report,) = repository.appended
    assert report.outcome == OUTCOME_VICTORY
    assert (report.attacker_losses, report.defender_losses) == (0, 783)
    assert report.defender_target == TARGET
    assert report.reported_at_utc == REPORTED_AT


def test_the_outcome_comes_from_the_survivors_not_the_banner() -> None:
    """⚠️ 胜负按**剩余舰艇数**算（用户口径 2026-08-11），不看画面上那行大字。

    这里让两者打架：横幅写着 `VICTORY`，而我方 100 全损、对方 783 一艘没掉。
    按算式我方剩余 0 → `FAIL`。横幅没有推翻算式的资格。
    """
    repository = _Repository()
    loop, _events = _loop(repository, bottom=_Screens(losses=("100", "0")))

    assert loop._ingest_report(ROW, _Screens(losses=("100", "0"))) is ReportIngest.STORED
    assert repository.appended[0].outcome == OUTCOME_FAIL


def test_the_losses_row_is_read_from_the_dragged_screen() -> None:
    """「损失单位」只有把详情页拖到底才读得到，七张实拍没有一张例外。

    不拖就没有战损，没有战损就算不出胜负（`剩余 = 单位 − 损失单位`），
    这一份会被整份拒收——而拒收看起来和「信箱里没有战报」一模一样。
    """
    repository = _Repository()
    loop, events = _loop(repository)

    loop._ingest_report(ROW, _Screens())

    assert events == ["拖到底"]


def test_a_report_that_cannot_be_scored_is_refused_whole() -> None:
    """四个数缺一个就算不出胜负 → **整份拒收，不存半份**。

    这条记录的全部内容就是胜负与战损，缺了没有存的价值；而一条看起来像数据的
    残缺记录，没有人会再回头核。拒收也不早停：下面还躺着别的战报。
    """
    repository = _Repository()
    loop, _events = _loop(repository, bottom=_Screens(losses=("", "")))

    assert loop._ingest_report(ROW, _Screens()) is ReportIngest.UNREADABLE
    assert repository.appended == []


def test_a_report_of_another_kind_is_refused() -> None:
    """主题不是「海盗攻击报告」就不当海盗战报读。

    主题筛偏往「开」的一侧倒（读不出也照开），所以这一层必须自己再认一次，
    否则一封 bot 的攻击报告会被按海盗战报存进去、认领错那一发派遣。
    """
    repository = _Repository()
    loop, _events = _loop(repository)
    header = "发件人: System        09/08/2026 04:38:46\n主题: 攻击报告"

    assert loop._ingest_report(ROW, _Screens(header=header)) is ReportIngest.UNREADABLE
    assert repository.appended == []


def test_a_report_already_in_the_database_is_not_stored_twice() -> None:
    """信箱里那几行每趟都在。没有这道去重，一份战报会每趟复制一行。

    判据取**报告时间**（游戏自己写在报告上的字），不受本地时钟与重跑影响。
    """
    repository = _Repository(already_stored=True)
    loop, _events = _loop(repository)

    assert loop._ingest_report(ROW, _Screens()) is ReportIngest.KNOWN
    assert repository.appended == []


def test_an_unreadable_report_does_not_stop_the_trip() -> None:
    """⚠️ 「读不出来」与「库里已有」必须分开：只有后者是早停的凭据。

    把读坏的那一封也当成早停，就是让一次 OCR 抖动把今天剩下的战报全部放弃——
    而它们就躺在同一趟信箱的下面几行。
    """
    repository = _Repository()
    loop, _events = _loop(repository, bottom=_Screens(losses=("", "")))

    assert loop._ingest_report_row(ROW, _Screens()) is False


def test_a_known_report_stops_the_opening() -> None:
    """读到库里已有的那一份就不再开封（用户口径 2026-08-11）。

    信箱从新往旧排、入库也从新往旧写，所以它往下的每一份都必然已经在库里了。
    """
    repository = _Repository(already_stored=True)
    loop, _events = _loop(repository)

    assert loop._ingest_report_row(ROW, _Screens()) is True
