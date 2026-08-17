"""海盗战报只记胜负与战损总数（用户口径 2026-08-09，为省性能）。

这条链路刻意**不读逐舰种明细**，所以它不能复用 `LiveReportReader`：
后者要求参战两列非空，还会因为「海盗攻击报告」不可与派遣匹配而整份拒收。

胜负**以画面横幅为准**（用户口径 2026-08-17：「游戏算法更新，剩余舰艇算法
已经不准了，可以读 victory」），横幅读不出来才回落到按剩余舰艇数算的结果。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_helper.vision.pirate_reports import (
    OUTCOME_DRAW,
    OUTCOME_FAIL,
    OUTCOME_VICTORY,
    PirateReportUnreadable,
    decide_outcome,
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


def test_the_banner_wins_when_it_disagrees_with_the_arithmetic() -> None:
    """⚠️ 判据是**画面上那行大字**，不是剩余舰艇数（用户口径 2026-08-17）。

    这里故意让两者打架：横幅写着 `VICTORY`，而我方 100 全损、对方 783 一艘没掉
    （按算式我方剩余 0 → `FAIL`）。生产日志 2026-08-17 18:20:17 见过的正是这个
    形状；用户说「游戏算法更新，剩余舰艇算法已经不准了，可以读 victory」，
    所以现在算式让位。
    """
    reading = read_pirate_report(
        _Screens(banner="VICTORY", units=("100", "783"), losses=("100", "0")),
        _Screens(losses=("100", "0")),
    )

    assert reading.outcome == OUTCOME_VICTORY


def test_a_draw_still_comes_out_of_the_fallback_arithmetic() -> None:
    """平局这一档**没有横幅样本**，只会从兜底算式里出来。

    仓库里 7 张详情页只有 `VICTORY` 与 `FAIL` 两种大字，平局长什么样谁也没见过。
    所以横幅读不出来（这里给一段噪声）而两边都还有船时，回落的算式给出 `DRAW`。
    """
    reading = read_pirate_report(
        _Screens(banner="- a", units=("100", "783"), losses=("30", "200")),
        _Screens(losses=("30", "200")),
    )

    assert reading.outcome == OUTCOME_DRAW


def test_a_banner_missing_a_letter_still_snaps() -> None:
    """实测这行大字压在星空上，`VICTORY` 会掉字母。"""
    assert parse_outcome("VICTORV") == OUTCOME_VICTORY
    assert parse_outcome("VICTORY\n") == OUTCOME_VICTORY
    assert parse_outcome("FAlL") == OUTCOME_FAIL


def test_an_unreadable_banner_falls_back_instead_of_dropping_the_report() -> None:
    """⚠️ **横幅读不出来不许静默丢数据。**

    横幅是第一判据，但它读不出来时这份记录照样要存下去——回落到那四个数
    （我方 100−0 还有船、对方 783−783 被全歼 → `VICTORY`）。
    那五张 bot 实拍上的横幅一度全读成 `'- a'`，而它们的四个数是齐的。
    """
    reading = read_pirate_report(_Screens(banner=""), _Screens())

    assert reading.outcome == OUTCOME_VICTORY


def test_a_report_whose_outcome_cannot_be_decided_at_all_is_refused() -> None:
    """胜负与战损是这条记录**唯一**的内容，两条路都定不出就没有存的价值。

    这里横幅是噪声、「单位」也读不出来——横幅顶不上，算式也算不出。
    """
    with pytest.raises(PirateReportUnreadable, match="定不出胜负"):
        read_pirate_report(_Screens(banner="- a", units=("", "")), _Screens())


class TestTheBannerIsTheVerdict:
    """横幅说了算；算式只当兜底。三条出路都要在日志里认得出来。"""

    def test_a_disagreement_says_the_banner_wins(self, caplog: pytest.LogCaptureFixture) -> None:
        """⚠️ 这条钉的是**日志说的是新规则**。

        旧措辞「以算出来的为准」在新规则下是假话，而这个仓库出过
        「日志说假话比不说更糟、故障因此拖了两天」的事故。
        """
        with caplog.at_level("WARNING"):
            decided = decide_outcome("VICTORY", OUTCOME_FAIL, where="测试")

        assert decided == OUTCOME_VICTORY
        assert "以横幅为准" in caplog.text
        assert "以算出来的为准" not in caplog.text
        # 两边各是什么、原文读到的是什么，都要说清楚，否则事后无从复核。
        assert "VICTORY" in caplog.text
        assert OUTCOME_FAIL in caplog.text

    def test_agreement_says_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            decided = decide_outcome("VICTORY", OUTCOME_VICTORY, where="测试")

        assert decided == OUTCOME_VICTORY
        assert caplog.text == ""

    def test_a_fallback_says_it_is_a_fallback(self, caplog: pytest.LogCaptureFixture) -> None:
        """回落值来自「已知会不准」的那套算式，日后核账要认得出哪些是它。"""
        with caplog.at_level("WARNING"):
            decided = decide_outcome("- a", OUTCOME_FAIL, where="测试")

        assert decided == OUTCOME_FAIL
        assert "回落" in caplog.text

    def test_a_missing_banner_reader_falls_back_silently(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`None` 表示这一屏根本没有横幅取字面——结构性缺席，不是读失败。

        离线入库与旧截图那几条路都走这里，为它们刷 warning 就是把这条日志
        变成噪声，用不了几次就没人再看它了。
        """
        with caplog.at_level("WARNING"):
            decided = decide_outcome(None, OUTCOME_FAIL, where="测试")

        assert decided == OUTCOME_FAIL
        assert caplog.text == ""

    def test_neither_source_yields_nothing_rather_than_a_guess(self) -> None:
        """「没定出胜负」和「打输了」在下游完全不同，绝不拿一档顶替。"""
        assert decide_outcome("- a", None, where="测试") is None
        assert decide_outcome(None, None, where="测试") is None


def test_the_third_label_does_not_swallow_the_other_two() -> None:
    """`DRAW` 这一档**手上没有样本**（仓库里 7 张详情页只有 `VICTORY` / `FAIL`）。

    列进词表的前提是它不能把另外两档吸走：三档两两距离都远大于容差 2，
    而 `snap_to_vocabulary` 遇到并列命中还会判歧义返回 None。
    这条钉的就是「多一档没有代价」，而不是「平局读得出来」——后者无从验证。

    横幅升回第一判据（2026-08-17）之后这一条更要紧：那串噪声任何一段被吸上，
    都会直接变成库里一条假战果。
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


def test_unit_totals_are_only_needed_when_the_banner_fails() -> None:
    """「单位」退回成**兜底算式的输入**：横幅读得出来时它缺席无所谓。

    2026-08-11 那版把它收紧成「读不出就整份拒收」（因为胜负只能靠算），
    2026-08-17 横幅升回第一判据之后这条收紧自然松开——但只在横幅顶得上的时候。
    横幅也读不出来时仍旧整份拒收（见
    `test_a_report_whose_outcome_cannot_be_decided_at_all_is_refused`）。
    """
    reading = read_pirate_report(_Screens(units=("", "")), _Screens())

    assert reading.outcome == OUTCOME_VICTORY
    assert (reading.attacker_units, reading.defender_units) == (None, None)
    # 战损照旧是硬要求：它是这条记录的另一半正文，不受横幅影响。
    assert (reading.attacker_losses, reading.defender_losses) == (0, 783)


def test_a_non_pirate_report_is_refused() -> None:
    header = "发件人: System        08/08/2026 13:09:51\n主题: 攻击报告"

    with pytest.raises(PirateReportUnreadable, match="海盗"):
        read_pirate_report(_Screens(header=header), _Screens())


def test_a_one_sided_versus_block_is_refused() -> None:
    """坐标读不全时不能把战报挂到错的目标上。"""
    with pytest.raises(PirateReportUnreadable, match="VS"):
        read_pirate_report(_Screens(versus="Kucleer\n奥格瑞玛\n[2:137:18]"), _Screens())
