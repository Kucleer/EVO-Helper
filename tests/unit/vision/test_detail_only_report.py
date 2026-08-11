"""只读战斗详情页那一屏的读法（`LiveReportReader.read_detail_only`）。

bot 那条链路要的是两样东西：「这一发的战报回来了没有」和守方「单位」总数，
两样都在详情页上。逐舰种明细在**回放页**（`ReportLayout.participating_rows`
是对着回放页量的），要拿到它得点开「查看战斗回放」——那个按钮至今没有标定过的
点击坐标，一份报告还要多花两三秒 OCR。所以这条读法只看详情页。

胜负是**算**出来的（用户口径 2026-08-11，判据在 `domain.battle_outcome`）：
剩余 = 单位 − 损失单位，本方剩余 0 判负、对方被全歼判胜、两边都有船判平。
画面上那行 `VICTORY` / `FAIL` 大字只做交叉校验，没有推翻算式的资格。

这里守三件事：

1. **不读的东西必须是空的，不能顶替。** 「没读明细」和「对方一艘船都没有」
   在下游长得一模一样，而后者会直接进情报中心。
2. **算不出胜负就留空**，尤其不许让横幅在这时顶上来。四个数缺一个就判不出，
   而「损失单位」要拖到底才读得到，所以缺席是常态。
3. **该守的判据一条都不能因为「只读一屏」而放松。** 主题、时间、VS 块两边全，
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
        losses: tuple[str, str] = ("", ""),
        banner: str = "FAIL",
    ) -> None:
        self._header = header
        self._versus = versus
        self._units = units
        self._losses = losses
        self._banner = banner

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

    def loss_totals(self) -> tuple[str, str]:
        """默认空着——「损失单位」那一行**只有拖到底那一屏**才读得到。

        七张实拍里没有一张在没拖的那屏上读到过它，所以桩件的默认值就该是空。
        """
        return self._losses

    def outcome_banner(self) -> str:
        return self._banner


def detail_page(version: str | None = DETAIL_UI_VERSION) -> PageObservation:
    return PageObservation(screen="mail_detail", ui_version=version, confidence=0.99)


def _read(bottom: object | None = None, **kwargs: object) -> object:
    reader = LiveReportReader(DetailScreens(**kwargs))  # type: ignore[arg-type]
    return reader.read_detail_only(detail_page(), bottom=bottom)


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

    def test_the_losses_are_read_too_because_the_verdict_needs_them(self) -> None:
        """战损既是页面上「战损 我 X · 敌 Y」的来源，也是算胜负的输入之一。"""
        report = _read(losses=("30", "200"))

        assert report.attacker_losses == 30  # type: ignore[attr-defined]
        assert report.defender_losses == 200  # type: ignore[attr-defined]

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

    def test_an_uncomputable_outcome_is_not_turned_into_a_defeat(self) -> None:
        """⚠️ 「没算出胜负」和「打输了」在下游完全不同：后者会进攻击日志的战果列。

        没拖到底就没有战损，没有战损就算不出胜负——这是**常态**，不是异常路径。
        画面上的 `FAIL` 大字**不许**在这时顶上来：用户明确说了不看游戏内的提示。
        """
        assert _read(banner="FAIL").outcome is None  # type: ignore[attr-defined]
        assert _read(banner="VICTORY", losses=("30", "")).outcome is None  # type: ignore[attr-defined]

    def test_the_banner_never_overrides_the_arithmetic(self) -> None:
        """两者打架时以算式为准（横幅只留 warning）。

        这里横幅写着 `VICTORY`，而我方 100 全损、对方 5360 一艘没掉——判 `FAIL`。
        """
        report = _read(banner="VICTORY", units=("100", "5.36K"), losses=("100", "0"))

        assert report.outcome == "FAIL"  # type: ignore[attr-defined]

    def test_a_screens_object_without_a_banner_reader_still_reads(self) -> None:
        """横幅是增强项：提供不了的取字面实现照样能读出一份完整战报，
        而且**胜负照样算得出来**——它本来就不看横幅。

        与 `unit_totals` 同一个 getattr 路子：写进协议会打断所有既有实现。
        """

        class _NoBanner(DetailScreens):
            outcome_banner = None  # type: ignore[assignment]

        report = LiveReportReader(_NoBanner(losses=("100", "0"))).read_detail_only(  # type: ignore[arg-type]
            detail_page()
        )

        assert report.outcome == "FAIL"
        assert report.defender_units == 5360


class TestTheScrolledScreen:
    """「单位」多半、「损失单位」一定**不在**没拖过的那一屏上，得拖到底再拍一屏。

    成因：bot 战报比海盗战报多一行「生成卫星概率」，「战斗详情」横幅下移约 30px，
    「单位」整行落到面板可视区之外。2026-08-11 的五张实拍里四张如此，
    第五张恰好没有那一行、「单位」就读得出来（`1` / `319`）；而「损失单位」在它
    下面一行，七张实拍**没有一张**在没拖那屏上读得到。

    换判据之后这一屏从「补一个展示字段」变成了**判据的输入**：
    不拖就没有战损，没有战损就算不出胜负。
    """

    def test_the_bottom_screen_fills_in_units_the_first_screen_lacks(self) -> None:
        report = _read(bottom=DetailScreens(units=("1", "319")), units=("", ""))

        assert report.attacker_units == 1  # type: ignore[attr-defined]
        assert report.defender_units == 319  # type: ignore[attr-defined]

    def test_the_first_screen_wins_when_it_already_has_them(self) -> None:
        """看得见就别再问第二屏——那是另一次截图，没理由为已经读到的数再赌一次。"""
        report = _read(bottom=DetailScreens(units=("9", "9")))

        assert report.attacker_units == 100  # type: ignore[attr-defined]
        assert report.defender_units == 5360  # type: ignore[attr-defined]

    def test_the_losses_always_come_from_the_bottom_screen(self) -> None:
        """战损**只在第二屏上**，不像「单位」那样先问第一屏。

        没拖过的那屏上这一行被面板下沿切掉，读回来是半行字（实测读作 `'.'`）；
        把它当数据用，就等于在半行字上判胜负。
        """
        report = _read(bottom=DetailScreens(losses=("1", "0")), units=("1", "319"))

        assert (report.attacker_losses, report.defender_losses) == (1, 0)  # type: ignore[attr-defined]

    def test_the_two_screens_together_produce_the_verdict(self) -> None:
        """这就是实机上那条路：第一屏出身份与「单位」，第二屏出战损，然后算。

        我方 1 艘全损 → 剩余 0 → `FAIL`，与那五张探路战报的横幅一致。
        """
        report = _read(bottom=DetailScreens(losses=("1", "0")), units=("1", "319"))

        assert report.outcome == "FAIL"  # type: ignore[attr-defined]

    def test_the_banner_is_never_read_from_the_bottom_screen(self) -> None:
        """⚠️ **拖到底之后横幅已经滚出可视区了。**

        实测那一屏的同一个 ROI 读作 `'Z ?'`（`var/logs/pir1-bottom.png`）。
        横幅只做交叉校验，而校验取的必须是**没拖过那一屏**的读数——拿滚过之后
        那段画面里的噪声去校验，只会刷出一串假 warning。
        """
        report = _read(
            bottom=DetailScreens(losses=("1", "0"), banner="VICTORY"),
            units=("1", "319"),
            banner="FAIL",
        )

        assert report.outcome == "FAIL"  # type: ignore[attr-defined]

    def test_the_bottom_screen_is_optional(self) -> None:
        """不给第二屏也要能读——`read_report` 与离线入库那条路都不拖。
        读不到就诚实地空着，不因为「少了一屏」而整份拒收。"""
        report = _read(units=("", ""))

        assert report.defender_units is None  # type: ignore[attr-defined]
        assert report.outcome is None  # type: ignore[attr-defined]


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
