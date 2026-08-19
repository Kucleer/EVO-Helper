"""AI 选靶观测器的单元行为：开关零开销、限流、并发上限、解码校验。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from evo_helper.application.ai_targeting import (
    AI_SHADOW_MIN_INTERVAL_S,
    AiShadowObserver,
    stratified_samples,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)

ORIGIN_A = Coordinate(4, 277, 15)
ORIGIN_B = Coordinate(9, 250, 8)

TARGETS = [
    Coordinate(4, 269, 8),
    Coordinate(4, 393, 10),
    Coordinate(9, 245, 14),
    Coordinate(9, 244, 13),
    Coordinate(8, 80, 19),
]


def _settings(api_base: str | None = "https://api.example.test/chat/completions") -> Any:
    return SimpleNamespace(ai_api_base=api_base, ai_api_key="sk-test", ai_model="test-model")


def _scored(*, base: int = 18_000) -> list[ScoredTarget]:
    return [
        ScoredTarget(
            coordinate=coordinate,
            military_score=float(base + index * 1000),
            military_score_at_utc=NOW - timedelta(minutes=index * 30),
        )
        for index, coordinate in enumerate(TARGETS)
    ]


class _FakeConfigRow:
    ai_sample_size: int | None = None
    ai_timeout_seconds: int | None = None
    ai_model: str | None = None
    #: ⚠️ **默认 True**：这些用例钉的是限流 / 并发 / 解码，不是开关。开关自己那条
    #: 用例在 `TestTheSwitchIsCheckedTwice` 里，它显式把这一项关掉。
    ai_shadow_enabled: bool | None = True


class _FakeRepo:
    def __init__(self) -> None:
        self.config = _FakeConfigRow()

    def military_attack_config(self) -> _FakeConfigRow:
        return self.config


def _observer(repository: Any = None, **overrides: Any) -> AiShadowObserver:
    return AiShadowObserver(
        repository if repository is not None else _FakeRepo(),
        _settings(),
        sample_size=60,
        timeout_s=5.0,
        model="injected-model",
        **overrides,
    )


class TestAvailable:
    def test_missing_credentials_disables_the_observer(self) -> None:
        observer = AiShadowObserver(_FakeRepo(), _settings(api_base=None))
        assert not observer.available

    def test_credentials_enable_the_observer(self) -> None:
        assert _observer().available

    def test_an_explicitly_injected_httpx_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = SimpleNamespace(post=lambda *a, **k: _raise())
        monkeypatch.setattr("evo_helper.application.ai_targeting.httpx", fake)
        assert _observer().available

    def test_available_says_nothing_about_the_switch(self) -> None:
        """⚠️ `available` **只看凭据与依赖**，开关关着它照样是 True。

        钉这一条是因为「observer.enabled」这个名字太容易被当成「整条功能开着」
        的保险来用——它从来不是。真正的开关判断有两处：调度器一侧，以及
        `observe()` 里 `_read_knobs` 那一次。
        """
        repository = _FakeRepo()
        repository.config.ai_shadow_enabled = False
        observer = _observer(repository)
        assert observer.available is True


class TestTheSwitchIsCheckedTwice:
    def test_the_observer_refuses_when_the_switch_is_off(self) -> None:
        """⚠️ 调度器一侧已经判过开关，observer **自己再判一次**。

        只有一个调用点时它是冗余的；多一个调用点、或者哪天有人在别处直接调
        `observe()`，这一条就是唯一挡得住「开关关着却真发了请求」的东西。
        用的是 `_read_knobs` 本来就要读的那一行，不多花一次查询。
        """
        repository = _FakeRepo()
        repository.config.ai_shadow_enabled = False
        launched = _observer(repository).observe(
            task_id=2,
            now=NOW,
            run_id=None,
            budget=2,
            candidates=_scored(),
            origins=[ORIGIN_A],
            configured_lines={ORIGIN_A: 4},
            budgets_by_origin={ORIGIN_A: 2},
            account_inflight=0,
            account_limit=None,
            hold=timedelta(minutes=90),
            presets=frozenset({"BBB"}),
            assignments=[],
        )
        assert launched is False

    def test_a_missing_config_row_counts_as_off(self) -> None:
        """配置行读不到时按「关」处理——宁可什么都不发。"""

        class _Broken(_FakeRepo):
            def military_attack_config(self) -> _FakeConfigRow:
                raise ValueError("配置行不存在")

        launched = _observer(_Broken()).observe(
            task_id=2,
            now=NOW,
            run_id=None,
            budget=2,
            candidates=_scored(),
            origins=[ORIGIN_A],
            configured_lines={ORIGIN_A: 4},
            budgets_by_origin={ORIGIN_A: 2},
            account_inflight=0,
            account_limit=None,
            hold=timedelta(minutes=90),
            presets=frozenset({"BBB"}),
            assignments=[],
        )
        assert launched is False


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _raise() -> None:
    raise AssertionError("should not reach network")


class TestObserveZeroCost:
    def test_disabled_observer_never_starts_a_thread(self) -> None:
        observer = AiShadowObserver(_FakeRepo(), _settings(api_base=None))
        launched = observer.observe(
            task_id=2,
            now=NOW,
            run_id=None,
            budget=2,
            candidates=_scored(),
            origins=[ORIGIN_A],
            configured_lines={ORIGIN_A: 4},
            budgets_by_origin={ORIGIN_A: 2},
            account_inflight=0,
            account_limit=None,
            hold=timedelta(minutes=90),
            presets=frozenset({"BBB"}),
            assignments=[],
        )
        assert launched is False

    def test_a_zero_budget_never_starts_a_thread(self) -> None:
        assert not _observer().observe(
            task_id=2,
            now=NOW,
            run_id=None,
            budget=0,
            candidates=_scored(),
            origins=[ORIGIN_A],
            configured_lines={ORIGIN_A: 0},
            budgets_by_origin={ORIGIN_A: 0},
            account_inflight=0,
            account_limit=None,
            hold=timedelta(minutes=90),
            presets=frozenset({"BBB"}),
            assignments=[],
        )

    def test_an_empty_pool_never_starts_a_thread(self) -> None:
        assert not _observer().observe(
            task_id=2,
            now=NOW,
            run_id=None,
            budget=2,
            candidates=[],
            origins=[ORIGIN_A],
            configured_lines={ORIGIN_A: 4},
            budgets_by_origin={ORIGIN_A: 2},
            account_inflight=0,
            account_limit=None,
            hold=timedelta(minutes=90),
            presets=frozenset({"BBB"}),
            assignments=[],
        )


class TestRateLimitAndConcurrency:
    def test_the_same_task_is_throttled_within_the_window(self) -> None:
        monotonic = iter([0.0, 30.0])  # 第二次 30 秒后仍在 60 秒窗口内
        observer = _observer(
            monotonic=lambda: next(monotonic),  # type: ignore[arg-type]
        )
        first = observer.observe(
            task_id=2,
            now=NOW,
            run_id=None,
            budget=2,
            candidates=_scored(),
            origins=[ORIGIN_A],
            configured_lines={ORIGIN_A: 4},
            budgets_by_origin={ORIGIN_A: 2},
            account_inflight=0,
            account_limit=None,
            hold=timedelta(minutes=90),
            presets=frozenset({"BBB"}),
            assignments=[],
        )
        second = observer.observe(
            task_id=2,
            now=NOW,
            run_id=None,
            budget=2,
            candidates=_scored(),
            origins=[ORIGIN_A],
            configured_lines={ORIGIN_A: 4},
            budgets_by_origin={ORIGIN_A: 2},
            account_inflight=0,
            account_limit=None,
            hold=timedelta(minutes=90),
            presets=frozenset({"BBB"}),
            assignments=[],
        )
        assert first is True
        assert second is False

    def test_only_one_worker_runs_at_a_time(self) -> None:
        monotonic = iter([0.0, AI_SHADOW_MIN_INTERVAL_S + 1])
        observer = _observer(
            monotonic=lambda: next(monotonic),  # type: ignore[arg-type]
        )
        # 手动把并发占位满，第二个任务就该被挡下。
        observer._active = 1
        launched = observer.observe(
            task_id=3,
            now=NOW,
            run_id=None,
            budget=2,
            candidates=_scored(),
            origins=[ORIGIN_A],
            configured_lines={ORIGIN_A: 4},
            budgets_by_origin={ORIGIN_A: 2},
            account_inflight=0,
            account_limit=None,
            hold=timedelta(minutes=90),
            presets=frozenset({"BBB"}),
            assignments=[],
        )
        assert launched is False


class TestDecodeAndCheck:
    def _observer(self) -> AiShadowObserver:
        return _observer()

    def _eligible(self) -> tuple[ScoredTarget, ...]:
        return tuple(_scored())

    def _budgets(self) -> dict[Coordinate, int]:
        return {ORIGIN_A: 2, ORIGIN_B: 0}

    def _decode(
        self,
        response: str,
        observer: AiShadowObserver,
        *,
        assignments: tuple[Any, ...] = (),
    ) -> tuple[Any, Any, Any, Any]:
        from evo_helper.domain.ai_targeting import SoftReference

        eligible = self._eligible()
        if not assignments:
            # 让「BBB」出现在可选的预设集合里（从算法实际用到的预设提取）。
            # 坐标刻意用 AI 没选的那个，overlap 恒为 0。
            assignments = (
                SimpleNamespace(coordinate=eligible[2].coordinate, origin=ORIGIN_A, preset="BBB"),
            )
        reference = SoftReference(
            military={item.coordinate: item.military_score or 0.0 for item in eligible},
            reading_age_hours={item.coordinate: 0.5 for item in eligible},
            round_trip_minutes={
                item.coordinate: {ORIGIN_A: 40.0, ORIGIN_B: 200.0} for item in eligible
            },
            last_attack_at={item.coordinate: None for item in eligible},
            protected_until={item.coordinate: None for item in eligible},
            now=NOW,
        )
        return observer._decode_and_check(  # noqa: SLF001 - 钉的就是这条真路
            response,
            eligible,
            (ORIGIN_A, ORIGIN_B),
            self._budgets(),
            2,
            assignments,
            reference,
        )

    def test_invalid_json_is_reported(self) -> None:
        status, violations, ai_picks, _ = self._decode("not json at all", self._observer())
        assert status == "invalid_json"
        assert any(item["code"] == "invalid_json" for item in violations)
        assert ai_picks is None

    def test_missing_picks_is_a_schema_violation(self) -> None:
        status, violations, ai_picks, _ = self._decode('{"notes": "hi"}', self._observer())
        assert status == "schema_violation"
        assert any(item["code"] == "missing_picks" for item in violations)

    def test_a_valid_but_wrong_pick_set_is_rejected(self) -> None:
        response = (
            '{"picks": ['
            '{"target": "1:1:1", "origin": "1:1:1", "preset": "XXX"},'
            ' {"target": "1:1:2", "origin": "1:1:1", "preset": "XXX"}]}'
        )
        status, violations, ai_picks, _ = self._decode(response, self._observer())
        assert status == "schema_violation"
        codes = {item["code"] for item in violations}
        assert "unknown_target" in codes
        assert "unknown_origin" in codes
        assert "unknown_preset" in codes

    def test_a_clean_pick_set_is_ok(self) -> None:
        first, second = TARGETS[0], TARGETS[1]
        response = (
            '{"picks": ['
            f'{{"target": "{first}", "origin": "{ORIGIN_A}", "preset": "BBB", "military": 18000,'
            ' "reading_age_hours": 0.5, "round_trip_minutes": 40, "reason": "x"},'
            f' {{"target": "{second}", "origin": "{ORIGIN_A}", "preset": "BBB"}}]}}'
        )
        status, violations, ai_picks, overlap = self._decode(response, self._observer())
        assert status == "ok"
        assert violations == []
        assert ai_picks is not None
        assert overlap == 0


def test_stratified_samples_never_exceeds_the_size_and_keeps_every_cell() -> None:
    candidates = tuple(_scored() + _scored(base=40_000))
    sample = stratified_samples(candidates, [ORIGIN_A], sample_size=4)
    assert len(sample.targets) <= 4
    # 样本里的坐标必须来自候选池。
    coordinates = {item.coordinate for item in sample.targets}
    assert coordinates <= {item.coordinate for item in candidates}


class TestSamplingNeverDropsAKey:
    """⚠️ **两个抽样键（最强 / 最新）一个都不许丢。**

    用「最强 / 最新」而不是现有得分 `军力 ÷ 往返`，本身就是为了不把要验证的那条
    公式的答案泄露给 AI（方案 2.2）。旧版的降配阶梯最后一级是「每格只取军力最高
    1 个」，在生产量级上恰恰是常态——那一级把「最新」整个丢掉了，等于丢掉这个设计。
    """

    #: 8 个格子（每个银河一格），每格 4 个目标。
    #:
    #: **格内军力序与新鲜度序刻意相反**：position 1 是这一格军力最高的（读数最旧），
    #: position 4 是读数最新的（军力最低）。两个抽样键因此指向不同的行——
    #: 指向同一行的话，丢掉一个键也看不出来。
    CELLS = 8
    STRONGEST_POSITION = 1
    FRESHEST_POSITION = 4

    def _pool(self) -> list[ScoredTarget]:
        return [
            ScoredTarget(
                coordinate=Coordinate(galaxy, 120, rank + 1),
                military_score=float(9_000 - rank * 1_000),
                military_score_at_utc=NOW - timedelta(hours=4 - rank),
            )
            for galaxy in range(1, self.CELLS + 1)
            for rank in range(4)
        ]

    def test_even_one_per_cell_keeps_both_keys(self) -> None:
        """★ 预算紧到「每格只发得起一个」时，两个键**都**要在样本里露面。

        这一条就是旧实现的死穴：它降到「每格只取军力最高 1 个」，
        `FRESHEST_POSITION` 一个都不会出现。
        """
        sample = stratified_samples(self._pool(), [ORIGIN_A], sample_size=self.CELLS)
        assert sample.cells_total == self.CELLS
        assert sample.max_per_cell == 1, "预算只够每格一个"
        positions = {item.coordinate.position for item in sample.targets}
        assert self.STRONGEST_POSITION in positions, "「军力最高」这个键在样本里没了"
        assert self.FRESHEST_POSITION in positions, "「读数最新」这个键在样本里没了"

    def test_the_reported_shape_matches_what_was_actually_taken(self) -> None:
        """`cells_covered` / `max_per_cell` 必须与真取到的一致——prompt 照它说话。"""
        sample = stratified_samples(self._pool(), [ORIGIN_A], sample_size=self.CELLS)
        assert sample.cells_covered == self.CELLS
        assert sample.max_per_cell == 1
        roomy = stratified_samples(self._pool(), [ORIGIN_A], sample_size=1000)
        assert roomy.max_per_cell > 1, "预算宽裕时每格该拿到不止一个"

    def test_the_sample_size_is_a_hard_ceiling(self) -> None:
        sample = stratified_samples(self._pool(), [ORIGIN_A, ORIGIN_B], sample_size=5)
        assert len(sample.targets) == 5
