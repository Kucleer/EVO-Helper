"""诊断页「AI 选靶（影子）」那几个比率的口径。

## ⚠️ 这个文件在补一个真的错

上一版把「硬校验通过率」算成 `status == ok` 除以**全部记录**，于是**超时和 HTTP
错误被算成「硬校验没过」**。后果不是数字难看一点，是**把人引到错的地方去**：
LLM 服务挂了一晚上，页面上显示的却是「模型不守规则」——一个该去查网络和额度，
一个该去改 prompt，善后完全相反。

现在分四层，每层的分母各不相同（见 `_ai_health` 的文档串）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from evo_helper.web.app import _ai_health


def _row(status: str, *, overlap: int | None = 1, budget: int = 2, violations: str = "[]") -> Any:
    return SimpleNamespace(
        status=status,
        overlap=overlap if status == "ok" else None,
        budget=budget,
        violations_json=violations,
    )


def test_no_rows_gives_every_number_as_none() -> None:
    """没有数据时全给 None——页面显示「—」，**不显示 0%**。

    0% 会被读成「一次都没过」，而真相是「一次都还没跑过」。
    """
    health = _ai_health([])
    assert set(health.values()) == {None}


class TestTransportFailuresDoNotCountAsRuleBreaking:
    """★ 审查抓到的那个口径错误。"""

    def test_a_timeout_only_lowers_the_call_rate(self) -> None:
        health = _ai_health([_row("ok"), _row("timeout")])
        assert health["call_rate"] == 0.5
        # ⚠️ 拿到回答的那一条 JSON 与硬校验都过了 → 后两项必须是 100%。
        assert health["json_rate"] == 1.0
        assert health["hard_rate"] == 1.0

    def test_an_http_error_only_lowers_the_call_rate(self) -> None:
        health = _ai_health([_row("ok"), _row("http_error"), _row("ok")])
        assert health["call_rate"] == round(2 / 3, 3)
        assert health["hard_rate"] == 1.0

    def test_all_transport_failures_leave_the_later_rates_undefined(self) -> None:
        """一次都没拿到回答时，后面几项没有分母——给 None，不给 0。"""
        health = _ai_health([_row("timeout"), _row("http_error")])
        assert health["call_rate"] == 0.0
        assert health["json_rate"] is None
        assert health["hard_rate"] is None


class TestEachRateHasItsOwnDenominator:
    def test_bad_json_lowers_the_json_rate_but_not_the_call_rate(self) -> None:
        health = _ai_health([_row("ok"), _row("invalid_json")])
        assert health["call_rate"] == 1.0
        assert health["json_rate"] == 0.5
        # ⚠️ 解析不出来的那条**不进硬校验的分母**：它压根没走到硬校验。
        assert health["hard_rate"] == 1.0

    def test_a_schema_violation_lowers_only_the_hard_rate(self) -> None:
        health = _ai_health([_row("ok"), _row("schema_violation")])
        assert health["call_rate"] == 1.0
        assert health["json_rate"] == 1.0
        assert health["hard_rate"] == 0.5

    def test_the_three_layers_move_independently(self) -> None:
        """一条一档，四个数各说各的那一层。"""
        health = _ai_health(
            [
                _row("ok"),
                _row("schema_violation"),
                _row("invalid_json"),
                _row("timeout"),
            ]
        )
        assert health["call_rate"] == 0.75  # 4 条里 3 条拿到了回答
        assert health["json_rate"] == round(2 / 3, 3)  # 3 条回答里 2 条能解析
        assert health["hard_rate"] == 0.5  # 2 条解析出来的里 1 条过了硬校验


class TestSelfConsistency:
    def test_a_self_consistency_violation_lowers_only_that_rate(self) -> None:
        health = _ai_health(
            [
                _row("ok"),
                _row("ok", violations='[{"code": "self_consistency_military"}]'),
            ]
        )
        assert health["hard_rate"] == 1.0
        assert health["self_consistency_rate"] == 0.5

    def test_a_rule_violation_does_not_lower_the_self_consistency_rate(self) -> None:
        """⚠️ 「规则遵守」和「数字自洽」是两件事，不许混在一个数里。"""
        health = _ai_health([_row("ok", violations='[{"code": "rule_in_protection"}]')])
        assert health["self_consistency_rate"] == 1.0

    def test_it_is_none_when_nothing_reached_ok(self) -> None:
        assert _ai_health([_row("schema_violation")])["self_consistency_rate"] is None


class TestAverageOverlap:
    def test_it_averages_only_the_ok_rows(self) -> None:
        health = _ai_health([_row("ok", overlap=2), _row("ok", overlap=0), _row("timeout")])
        assert health["avg_overlap"] == 1.0

    def test_it_is_none_without_any_ok_row(self) -> None:
        assert _ai_health([_row("timeout")])["avg_overlap"] is None
