"""海盗战报只记胜负与战损总数（用户口径 2026-08-09，为省性能）。

这条链路刻意**不读逐舰种明细**，所以它不能复用 `LiveReportReader`：
后者要求参战两列非空，还会因为「海盗攻击报告」不可与派遣匹配而整份拒收。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_helper.vision.pirate_reports import (
    OUTCOME_DRAW,
    OUTCOME_FAIL,
    OUTCOME_VICTORY,
    PirateReportUnreadable,
    cross_check_banner,
    parse_outcome,
    read_pirate_report,
)

HEADER = "发件人: System        09/08/2026 04:38:46\n主题: 海盗攻击报告"
VERSUS = "Kucleer  Pirates\n奥格瑞玛  Alien Brood\n[2:137:18]  [2:137:4]"


class _Screens:
    """一屏的取字面。真实实现是 Pillow 裁剪 + Tesseract。"""

    def __init__(
        self,
        *,
        header: str = HEADER,
        versus: str = VERSUS,
        banner: str = "VICTORY",
        units: tuple[str, str] = ("100", "783"),
        losses: tuple[str, str] = ("0", "783"),
    ) -> None:
        self._header = header
        self._versus = versus
        self._banner = banner
        self._units = units
        self._losses = losses

    def report_header(self) -> str:
        return self._header

    def versus_block(self) -> str:
        return self._versus

    def outcome_banner(self) -> str:
        return self._banner

    def unit_totals(self) -> tuple[str, str]:
        return self._units

    def loss_totals(self) -> tuple[str, str]:
        return self._losses


def test_a_complete_pirate_report_yields_outcome_and_losses() -> None:
    reading = read_pirate_report(_Screens(), _Screens())

    assert reading.outcome == OUTCOME_VICTORY
    assert (reading.attacker_losses, reading.defender_losses) == (0, 783)
    assert (reading.attacker_units, reading.defender_units) == (100, 783)
    assert reading.reported_at_utc == datetime(2026, 8, 9, 4, 38, 46, tzinfo=UTC)
    assert reading.raw_time_text == "09/08/2026 04:38:46"
    assert reading.defender_target.position == 4
    assert reading.attacker_origin.position == 18


def test_per_ship_detail_is_not_recorded() -> None:
    """明细是这条链路刻意省掉的东西，不能悄悄留个空壳字段冒充。"""
    reading = read_pirate_report(_Screens(), _Screens())

    assert not hasattr(reading, "fleet")


def test_a_lost_battle_is_computed_from_the_survivors_not_the_banner() -> None:
    """⚠️ 判据是**剩余舰艇数**，不是画面上那行大字（用户口径 2026-08-11）。

    这里故意让两者打架：横幅写着 `VICTORY`，而我方 100 全损、对方 783 一艘没掉。
    按算式我方剩余 0 → `FAIL`。横幅没有推翻算式的资格，只会留一条 warning。
    """
    reading = read_pirate_report(
        _Screens(banner="VICTORY", units=("100", "783"), losses=("100", "0")),
        _Screens(losses=("100", "0")),
    )

    assert reading.outcome == OUTCOME_FAIL


def test_a_draw_needs_no_screenshot_of_a_draw() -> None:
    """平局这一档是**算出来的**，不必先认出一张没人见过的横幅。

    仓库里 7 张详情页只有 `VICTORY` 与 `FAIL` 两种大字；换成按剩余数算之后，
    「两边都还有船」自然就落到 `DRAW`。
    """
    reading = read_pirate_report(
        _Screens(units=("100", "783"), losses=("30", "200")),
        _Screens(losses=("30", "200")),
    )

    assert reading.outcome == OUTCOME_DRAW


def test_a_banner_missing_a_letter_still_snaps() -> None:
    """实测这行大字压在星空上，`VICTORY` 会掉字母。"""
    assert parse_outcome("VICTORV") == OUTCOME_VICTORY
    assert parse_outcome("VICTORY\n") == OUTCOME_VICTORY
    assert parse_outcome("FAlL") == OUTCOME_FAIL


def test_an_unreadable_banner_no_longer_rejects_anything() -> None:
    """横幅降级成交叉校验之后，它读不出来**不再影响**这份记录能不能存。

    胜负来自四个数，横幅没有一票否决权——那五张 bot 实拍上的横幅一度全读成
    `'- a'`，而它们的四个数（拖到底之后）是齐的。
    """
    reading = read_pirate_report(_Screens(banner=""), _Screens())

    assert reading.outcome == OUTCOME_VICTORY


def test_a_report_whose_outcome_cannot_be_computed_is_refused() -> None:
    """胜负与战损是这条记录**唯一**的内容，算不出胜负就没有存的价值。

    判据换了（从「横幅读不出」变成「四个数缺一个」），拒收这条规矩没换。
    """
    with pytest.raises(PirateReportUnreadable, match="算不出胜负"):
        read_pirate_report(_Screens(units=("", "")), _Screens())


class TestTheBannerIsOnlyACrossCheck:
    """横幅**只记不改**：它没有推翻算式的资格，但对不上时要有人看得见。"""

    def test_a_disagreement_leaves_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            cross_check_banner(OUTCOME_FAIL, "VICTORY", where="测试")

        assert "以算出来的为准" in caplog.text

    def test_agreement_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            cross_check_banner(OUTCOME_VICTORY, "VICTORY", where="测试")

        assert caplog.text == ""

    def test_an_unreadable_banner_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """bot 战报缺战损是常态，横幅读不出来也是常态。

        为常态刷 warning 等于把这条校验变成噪声，用不了几次就没人再看它了。
        """
        with caplog.at_level("WARNING"):
            cross_check_banner(OUTCOME_FAIL, "- a", where="测试")
            cross_check_banner(None, "VICTORY", where="测试")

        assert caplog.text == ""


def test_the_third_label_does_not_swallow_the_other_two() -> None:
    """`DRAW` 这一档**手上没有样本**（仓库里 7 张详情页只有 `VICTORY` / `FAIL`）。

    列进词表的前提是它不能把另外两档吸走：三档两两距离都远大于容差 2，
    而 `snap_to_vocabulary` 遇到并列命中还会判歧义返回 None。
    这条钉的就是「多一档没有代价」，而不是「平局读得出来」——后者无从验证。
    """
    assert parse_outcome("DRAW") == OUTCOME_DRAW
    assert parse_outcome("FAIL") == OUTCOME_FAIL
    assert parse_outcome("VICTORY") == OUTCOME_VICTORY
    # 2026-08-11 实拍上真的读出来过的噪声，一段都不许贴上。
    for noise in ("- a", "Z ?", "ee eoooooOomy", "@- b= ie", ") Ai tt Fl:", "re", "-17003"):
        assert parse_outcome(noise) is None, noise


def test_unreadable_losses_reject_the_whole_report() -> None:
    with pytest.raises(PirateReportUnreadable, match="战损"):
        read_pirate_report(_Screens(), _Screens(losses=("0", "")))


def test_unit_totals_are_no_longer_optional() -> None:
    """「单位」从附带信息变成了**判据的输入**：剩余 = 单位 − 损失单位。

    以前它读不出来只是少一个展示字段，现在少了就判不出胜负，而胜负是这条记录的
    正文——所以整份拒收。这是换判据带来的、有意的收紧。
    """
    with pytest.raises(PirateReportUnreadable, match="算不出胜负"):
        read_pirate_report(_Screens(units=("", "")), _Screens())


def test_a_non_pirate_report_is_refused() -> None:
    header = "发件人: System        08/08/2026 13:09:51\n主题: 攻击报告"

    with pytest.raises(PirateReportUnreadable, match="海盗"):
        read_pirate_report(_Screens(header=header), _Screens())


def test_a_one_sided_versus_block_is_refused() -> None:
    """坐标读不全时不能把战报挂到错的目标上。"""
    with pytest.raises(PirateReportUnreadable, match="VS"):
        read_pirate_report(_Screens(versus="Kucleer\n奥格瑞玛\n[2:137:18]"), _Screens())
