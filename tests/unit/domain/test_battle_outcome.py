"""胜负按剩余舰艇数算（用户口径 2026-08-11），不看画面上那行大字。

    剩余 = 单位 − 损失单位
    本方剩余 0        → FAIL
    对方剩余 0        → VICTORY
    两边都还有船      → DRAW

这条规则住在 domain 层是有意的：它是**口径**，以后会变（「全歼」怎么算、
防御设施要不要单独计），而读数那一侧（哪个 ROI、放大几倍）变的原因完全不同。
混在一起，改口径就得动 OCR，而 OCR 的回归样本又证明不了口径对不对。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.battle_outcome import (
    OUTCOME_DRAW,
    OUTCOME_FAIL,
    OUTCOME_VICTORY,
    outcome_from_survivors,
    outcome_from_totals,
    survivors,
)


class TestTheThreeRules:
    def test_our_side_wiped_out_is_a_defeat(self) -> None:
        assert outcome_from_survivors(0, 5) == OUTCOME_FAIL

    def test_their_side_wiped_out_is_a_victory(self) -> None:
        assert outcome_from_survivors(5, 0) == OUTCOME_VICTORY

    def test_neither_side_wiped_out_is_a_draw(self) -> None:
        """**平局不需要样本，它是算出来的。**

        仓库里 7 张详情页只有 `VICTORY` 与 `FAIL` 两种横幅，平局长什么样谁也没见过。
        改成按剩余数算之后，这一档不再依赖认出一张没人见过的图。
        """
        assert outcome_from_survivors(1, 318) == OUTCOME_DRAW

    def test_mutual_annihilation_counts_as_a_defeat(self) -> None:
        """两边同时归零：用户把「本方剩余 0 则战败」列在第一条。

        同归于尽本来也不该记成胜仗——舰队没了，目标却没占到便宜。
        """
        assert outcome_from_survivors(0, 0) == OUTCOME_FAIL


class TestTheArithmetic:
    def test_survivors_is_units_minus_losses(self) -> None:
        assert survivors(783, 783) == 0
        assert survivors(100, 0) == 100

    def test_a_missing_number_yields_no_survivor_count(self) -> None:
        """⚠️ **缺一个数不能拿 0 顶替。**

        「损失单位」那一行要把详情页拖到底才读得到，缺席是常态；把缺席当成 0，
        「没读到」就变成「一艘没损失」，再经这条规则就变成一场胜仗。
        """
        assert survivors(100, None) is None
        assert survivors(None, 0) is None

    def test_losing_more_than_you_had_is_refused(self) -> None:
        """损失多于单位是**不可能**的读数，说明其中一个读错了。

        在两个自相矛盾的数上判胜负，等于把一次 OCR 抖动变成一条战果记录。
        """
        assert survivors(10, 11) is None


class TestFailClosed:
    def test_one_unknown_side_means_no_verdict(self) -> None:
        assert outcome_from_survivors(None, 0) is None
        assert outcome_from_survivors(0, None) is None

    @pytest.mark.parametrize(
        "missing", ["attacker_units", "attacker_losses", "defender_units", "defender_losses"]
    )
    def test_any_of_the_four_numbers_missing_means_no_verdict(self, missing: str) -> None:
        """四个数缺任何一个都判不出。这是**最常见**的情况，不是异常路径：
        没拖到底就没有战损，没有战损就没有战果。"""
        totals: dict[str, int | None] = {
            "attacker_units": 100,
            "attacker_losses": 0,
            "defender_units": 783,
            "defender_losses": 783,
        }
        assert outcome_from_totals(**totals) == OUTCOME_VICTORY  # type: ignore[arg-type]

        totals[missing] = None
        assert outcome_from_totals(**totals) is None  # type: ignore[arg-type]


class TestAgainstTheCapturedReports:
    """用仓库里现成的实拍读数核一遍，别只在造出来的数上打转。"""

    def test_the_pirate_report_we_have_a_screenshot_of(self) -> None:
        """`var/logs/pir1-*.png`：我方 100/0，对方 783/783 → 对方被全歼。

        画面上那行大字正是 `VICTORY`，两条路对得上。
        """
        assert (
            outcome_from_totals(
                attacker_units=100,
                attacker_losses=0,
                defender_units=783,
                defender_losses=783,
            )
            == OUTCOME_VICTORY
        )

    def test_a_probe_that_lost_its_single_ship(self) -> None:
        """探路是拿一艘船去换一个守方数量，回来基本都是 `FAIL`。

        我方 1/1（那一艘没了），对方 319/0 → 本方剩余 0 → FAIL，
        与那五张实拍上的红色 `FAIL` 横幅一致。
        """
        assert (
            outcome_from_totals(
                attacker_units=1,
                attacker_losses=1,
                defender_units=319,
                defender_losses=0,
            )
            == OUTCOME_FAIL
        )
