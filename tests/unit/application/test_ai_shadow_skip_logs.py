"""影子观测**跳过时**留下的痕迹。

## ⚠️ 这个文件在补什么

审查发现的那个洞：用户把开关打开，`.env` 少一个键、或者 `httpx` 没装，
于是 `observe()` 一路 `return False`，**页面上什么都不发生，而库里一个字都
查不到原因**。这正是 CLAUDE.md 那条「日志不说话，故障拖了两天」的复发形态。

判据不是「有没有打日志」，是**出事时能不能只靠库里的日志定位**。

## 两种限流，别混

- **「可用 ↔ 不可用」是状态跃迁**：变了就立刻写，不受窗口约束。同一个状态
  连着一百轮也只写一条。
- **「这一下跳过了」按 120 秒窗口压**（`SKIP_LOG_THROTTLE_S`，抄
  `record_unrecognised_screen` 的先例）——它每一轮攻击都可能触发。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from evo_helper.application.ai_targeting import (
    AI_SHADOW_MIN_INTERVAL_S,
    SKIP_LOG_THROTTLE_S,
    AiShadowObserver,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget
from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    current_system_log_sink,
    install_system_log_sink,
    shutdown_system_log_sink,
)

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
ORIGIN_A = Coordinate(4, 277, 15)


class Collector:
    def __init__(self) -> None:
        self.records: list[SystemLogRecord] = []

    def __call__(self, batch: Any) -> None:
        self.records.extend(batch)

    def messages(self) -> list[str]:
        _flush()
        return [record.message for record in self.records]


@pytest.fixture
def collector() -> Iterator[Collector]:
    sink_collector = Collector()
    install_system_log_sink(
        SystemLogSink(sink_collector, flush_interval_s=0.01), context=SystemLogContext()
    )
    try:
        yield sink_collector
    finally:
        shutdown_system_log_sink()


def _flush() -> None:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


class _FakeConfigRow:
    ai_sample_size: int | None = None
    ai_timeout_seconds: int | None = None
    ai_model: str | None = None
    ai_shadow_enabled: bool | None = True


class _FakeRepo:
    def __init__(self) -> None:
        self.config = _FakeConfigRow()

    def military_attack_config(self) -> _FakeConfigRow:
        return self.config


def _settings(api_base: str | None = "https://api.example.test/chat/completions") -> Any:
    return SimpleNamespace(ai_api_base=api_base, ai_api_key="sk-test", ai_model="test-model")


def _pool() -> list[ScoredTarget]:
    return [
        ScoredTarget(
            coordinate=Coordinate(4, 269 + index, 8),
            military_score=float(18_000 + index * 500),
            military_score_at_utc=NOW,
        )
        for index in range(4)
    ]


def _observe(observer: AiShadowObserver, *, task_id: int = 2) -> bool:
    return observer.observe(
        task_id=task_id,
        now=NOW,
        run_id=None,
        budget=2,
        candidates=_pool(),
        origins=[ORIGIN_A],
        configured_lines={ORIGIN_A: 4},
        budgets_by_origin={ORIGIN_A: 2},
        account_inflight=0,
        account_limit=None,
        hold=timedelta(minutes=90),
        presets=frozenset({"BBB"}),
        assignments=[],
    )


class TestUnavailableSaysWhy:
    def test_missing_credentials_are_named_in_the_log(self, collector: Collector) -> None:
        """★ 「开关开着却什么都不发生」必须查得出原因。"""
        observer = AiShadowObserver(_FakeRepo(), _settings(api_base=None))
        assert _observe(observer) is False
        messages = collector.messages()
        assert any("观测不可用" in message for message in messages), messages
        assert any("凭据" in message for message in messages), messages

    def test_a_missing_httpx_is_named_in_the_log(
        self, collector: Collector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("evo_helper.application.ai_targeting.httpx", None)
        observer = AiShadowObserver(_FakeRepo(), _settings())
        assert _observe(observer) is False
        assert any("httpx 未安装" in message for message in collector.messages())

    def test_the_same_state_is_only_logged_once(self, collector: Collector) -> None:
        """⚠️ 状态没变就不再写——不然一整夜每一轮都刷一条同样的话。"""
        observer = AiShadowObserver(_FakeRepo(), _settings(api_base=None))
        for _ in range(5):
            _observe(observer)
        assert sum("观测不可用" in m for m in collector.messages()) == 1

    def test_recovery_is_logged_as_a_transition(self, collector: Collector) -> None:
        """从「不可用」回到「可用」也是一次跃迁，要写。"""
        settings = _settings(api_base=None)
        observer = AiShadowObserver(_FakeRepo(), settings)
        assert _observe(observer) is False
        settings.ai_api_base = "https://api.example.test/chat/completions"
        _observe(observer)
        assert any("观测恢复可用" in m for m in collector.messages())


class TestSkipsAreLoggedButThrottled:
    def test_the_switch_being_off_is_logged(self, collector: Collector) -> None:
        repository = _FakeRepo()
        repository.config.ai_shadow_enabled = False
        observer = AiShadowObserver(repository, _settings(), monotonic=lambda: 0.0)
        assert _observe(observer) is False
        assert any("开关" in m and "没开" in m for m in collector.messages())

    def test_throttling_is_logged_and_says_it_is_not_a_fault(self, collector: Collector) -> None:
        """⚠️ 限流跳过要说清「这是限流，不是故障」——否则排障的人会去查网络。

        ⚠️ 这里**不真的发起一轮**（那会起一个后台线程去够网络），而是直接把
        「上次发起时刻」摆在冷却窗口之内——要验的就是这一下被挡掉之后写了什么。
        """
        observer = AiShadowObserver(_FakeRepo(), _settings(), monotonic=lambda: 10.0)
        observer._last_request_at[2] = 0.0  # noqa: SLF001 - 10 秒前刚发过，还在冷却里
        assert _observe(observer) is False
        assert any("限流，不是故障" in m for m in collector.messages())

    def test_a_full_worker_pool_is_logged(self, collector: Collector) -> None:
        observer = AiShadowObserver(_FakeRepo(), _settings(), monotonic=lambda: 0.0)
        observer._active = 1  # noqa: SLF001 - 直接占满并发位
        assert _observe(observer) is False
        assert any("观测线程在跑" in m for m in collector.messages())

    def test_repeated_skips_inside_the_window_are_collapsed(self, collector: Collector) -> None:
        """★ 同一种跳过在 120 秒窗口内只写一条。

        这一段每一轮攻击都会走到；不压的话它自己就能把 `system_log` 淹掉——
        2026-08-18 那两条日志占了全表 44%，就是这么来的。
        """
        observer = AiShadowObserver(_FakeRepo(), _settings(), monotonic=lambda: 0.0)
        observer._active = 1  # noqa: SLF001
        for _ in range(20):
            _observe(observer)
        assert sum("观测线程在跑" in m for m in collector.messages()) == 1

    def test_a_skip_after_the_window_is_logged_again(self, collector: Collector) -> None:
        """窗口过去之后要再写一条——只写一次会让人以为它早就恢复了。"""
        ticks = iter([0.0, SKIP_LOG_THROTTLE_S + 1])
        observer = AiShadowObserver(_FakeRepo(), _settings(), monotonic=lambda: next(ticks))
        observer._active = 1  # noqa: SLF001
        _observe(observer)
        _observe(observer)
        assert sum("观测线程在跑" in m for m in collector.messages()) == 2


def test_nothing_at_all_is_logged_when_there_is_simply_no_work(
    collector: Collector,
) -> None:
    """「这一轮本来就没活干」（预算 0 / 池子空）**不记**。

    ⚠️ 那不是故障，调度器自己的日志已经把「为什么没活干」说清楚了；
    在这里再记一条只会让真正的失败淹没在噪声里。
    """
    observer = AiShadowObserver(_FakeRepo(), _settings(), monotonic=lambda: 0.0)
    assert (
        observer.observe(
            task_id=2,
            now=NOW,
            run_id=None,
            budget=0,
            candidates=_pool(),
            origins=[ORIGIN_A],
            configured_lines={ORIGIN_A: 0},
            budgets_by_origin={ORIGIN_A: 0},
            account_inflight=0,
            account_limit=None,
            hold=timedelta(minutes=90),
            presets=frozenset({"BBB"}),
            assignments=[],
        )
        is False
    )
    assert [m for m in collector.messages() if "跳过" in m] == []


def test_the_min_interval_constant_is_what_the_throttle_uses() -> None:
    """限流窗口与日志窗口是两个数，别不小心并成一个。"""
    assert AI_SHADOW_MIN_INTERVAL_S != SKIP_LOG_THROTTLE_S
