"""只读战斗详情页那一屏的读法（`LiveReportReader.read_detail_only`）。

bot 那条链路要的是两样东西：「这一发的战报回来了没有」和守方「单位」总数，
两样都在详情页上。逐舰种明细在**回放页**（`ReportLayout.participating_rows`
是对着回放页量的），要拿到它得点开「查看战斗回放」——那个按钮至今没有标定过的
点击坐标，一份报告还要多花两三秒 OCR。所以这条读法只看详情页。

这里守两件事：

1. **不读的东西必须是空的，不能顶替。** 「没读明细」和「对方一艘船都没有」
   在下游长得一模一样，而后者会直接进情报中心。
2. **该守的判据一条都不能因为「只读一屏」而放松。** 主题、时间、VS 块两边全，
   任一不成立就整份拒收——挂错目标比没有战报坏得多。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.vision.live_reports import DETAIL_UI_VERSION, LiveReportReader
from evo_helper.vision.models import PageObservation
from evo_helper.vision.parsers import ReportKind, UnknownUiVersionError

HEADER = "发件人: System                    06/08/2026 11:45:03\n主题: 攻击报告"

VERSUS = (
    "Kucleer                    bot_2_149_17\n"
    "奥格瑞玛                   bot_2_149_17's Planet\n"
    "[2:137:18]                 [2:149:17]"
)


class DetailScreens:
    """只有详情页那一屏能提供的取字面。

    **回放页那两样（参战两列、各回合）在这里是空的**，而且是刻意的：生产上
    这条链路根本没打开过回放页，桩件多给一点就等于测了一条不存在的路。
    """

    def __init__(
        self,
        *,
        header: str = HEADER,
        versus: str = VERSUS,
        units: tuple[str, str] = ("100", "5.36K"),
    ) -> None:
        self._header = header
        self._versus = versus
        self._units = units

    def mail_rows(self) -> list[str]:
        return []

    def report_header(self) -> str:
        return self._header

    def versus_block(self) -> str:
        return self._versus

    def participating_columns(self) -> tuple[str, str]:
        return ("", "")

    def round_columns(self) -> list[tuple[int, str, str]]:
        return []

    def unit_totals(self) -> tuple[str, str]:
        return self._units


def detail_page(version: str | None = DETAIL_UI_VERSION) -> PageObservation:
    return PageObservation(screen="mail_detail", ui_version=version, confidence=0.99)


def _read(**kwargs: object) -> object:
    reader = LiveReportReader(DetailScreens(**kwargs))  # type: ignore[arg-type]
    return reader.read_detail_only(detail_page())


class TestWhatTheDetailScreenGives:
    def test_identity_fields_come_back_whole(self) -> None:
        """认领派遣要的三样：时间、出发坐标、目标坐标。缺一样这份战报就没法归位。"""
        report = _read()

        assert report.kind is ReportKind.ATTACK  # type: ignore[attr-defined]
        assert report.reported_at_utc == datetime(2026, 8, 6, 11, 45, 3, tzinfo=UTC)  # type: ignore[attr-defined]
        assert report.attacker.coordinate.value == Coordinate(2, 137, 18)  # type: ignore[attr-defined]
        assert report.defender.coordinate.value == Coordinate(2, 149, 17)  # type: ignore[attr-defined]

    def test_unit_totals_are_read_from_this_screen(self) -> None:
        """分档就靠这个数。`5.36K` 是大舰队的四舍五入显示，要能解析。"""
        report = _read()

        assert report.attacker_units == 100  # type: ignore[attr-defined]
        assert report.defender_units == 5360  # type: ignore[attr-defined]

    def test_only_the_detail_screen_version_is_recorded(self) -> None:
        """**不填回放页的版本。** 版本标签是「这一屏长什么样」的凭据，
        而这条链路根本没看过那一屏；填上等于替一屏没看过的画面作证。"""
        report = _read()

        assert report.ui_versions == {"battle_detail_ui_version": DETAIL_UI_VERSION}  # type: ignore[attr-defined]


class TestWhatItDeliberatelyLeavesEmpty:
    def test_no_fleet_composition_is_invented(self) -> None:
        """参战两列与各回合一律空着。

        这是本文件最要紧的一条：`to_battle_report` 把它们摊成 `fleet_snapshots`，
        而情报中心按 `side='defender' and round_no is null` 取「这个 bot 有什么船」。
        随便顶一份进去，页面上就会出现一支**根本没读过**的舰队。
        """
        report = _read()

        assert report.participating_attacker == ()  # type: ignore[attr-defined]
        assert report.participating_defender == ()  # type: ignore[attr-defined]
        assert report.rounds == ()  # type: ignore[attr-defined]

    def test_an_empty_participating_column_is_not_an_error_here(self) -> None:
        """`read_report` 见到两列全空会抛「回放还没渲染出来」——那条判据只适用于
        真的打开了回放页的场合。这条路压根没去过那一屏，拿它当故障就是每一份
        战报都读不进来。"""
        assert _read() is not None

    def test_unreadable_unit_totals_stay_none(self) -> None:
        """读不到就留空，**绝不用明细之和顶替**——明细这里本来就没有。"""
        report = _read(units=("", ""))

        assert report.attacker_units is None  # type: ignore[attr-defined]
        assert report.defender_units is None  # type: ignore[attr-defined]


class TestFailClosed:
    def test_a_pirate_report_is_refused(self) -> None:
        """海盗战报不能与 bot 派遣匹配，读进来会闭合错的那一发。"""
        header = "发件人: System   07/08/2026 00:49:56\n主题: 海盗攻击报告"
        with pytest.raises(ValueError, match="not an attack"):
            _read(header=header)

    def test_a_still_rendering_panel_is_refused(self) -> None:
        """刚打开时面板只有背景装饰文字，和「空报告」在字段层面分不出来。"""
        with pytest.raises(UnknownUiVersionError):
            _read(header="-COMMAND OFFICERS\n-TOTAL CREWS\n-17003", versus="")

    def test_an_unreadable_time_is_refused(self) -> None:
        """报告时间既是入库的主键性事实，也是去重与认领派遣的判据。"""
        with pytest.raises(UnknownUiVersionError, match="time"):
            _read(header="主题: 攻击报告")

    def test_a_one_sided_versus_block_is_refused(self) -> None:
        """只读出一边就整份拒收：把一侧坐标当成双方，战报会挂到错的目标上。"""
        with pytest.raises(UnknownUiVersionError, match="versus"):
            _read(versus="Kucleer\n奥格瑞玛\n[2:137:18]")

    def test_an_unknown_detail_version_is_refused(self) -> None:
        reader = LiveReportReader(DetailScreens())  # type: ignore[arg-type]
        with pytest.raises(UnknownUiVersionError):
            reader.read_detail_only(detail_page("battle-detail-v9"))
