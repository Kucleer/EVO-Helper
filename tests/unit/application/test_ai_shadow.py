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
    ai_shadow_enabled: bool | None = None


class _FakeRepo:
    def __init__(self) -> None:
        self.config = _FakeConfigRow()

    def military_attack_config(self) -> _FakeConfigRow:
        return self.config


def _observer(**overrides: Any) -> AiShadowObserver:
    return AiShadowObserver(
        _FakeRepo(),
        _settings(),
        sample_size=60,
        timeout_s=5.0,
        model="injected-model",
        **overrides,
    )


class TestEnabled:
    def test_missing_credentials_disables_the_observer(self) -> None:
        observer = AiShadowObserver(_FakeRepo(), _settings(api_base=None))
        assert not observer.enabled

    def test_credentials_enable_the_observer(self) -> None:
        assert _observer().enabled

    def test_an_explicitly_injected_httpx_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = SimpleNamespace(post=lambda *a, **k: _raise())
        monkeypatch.setattr("evo_helper.application.ai_targeting.httpx", fake)
        assert _observer().enabled


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
            eligible=_scored(),
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
            eligible=_scored(),
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
            eligible=[],
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
            eligible=_scored(),
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
            eligible=_scored(),
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
            eligible=_scored(),
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
    eligible = tuple(_scored() + _scored(base=40_000))
    samples = stratified_samples(eligible, [ORIGIN_A], sample_size=4)
    assert len(samples) <= 4
    # 样本里的坐标必须来自 eligible。
    coordinates = {item.coordinate for item in samples}
    assert coordinates <= {item.coordinate for item in eligible}
