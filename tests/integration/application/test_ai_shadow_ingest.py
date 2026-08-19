"""真 observer + 假 httpx 的端到端落库：LLM 的四种结局都记进 `ai_target_decisions`。

验收标准里「LLM 挂掉时调度继续」由 `test_ai_shadow_safety.py` 钉住；这里钉的是
**落库与状态分类**——超时 / HTTP 错误 / 坏 JSON / 合法选择各落一行，绝不抛出。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.ai_targeting import AiShadowObserver
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import add_bot_target, enable, task

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

BY_MILITARY = '{"by_military": true, "top_n": 2, "score_max_age_hours": 2}'

ORIGIN_A = Coordinate(4, 277, 15)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class FakeHttpx:
    """假的 `application.ai_targeting.httpx`。`TimeoutException` 用真的那个。"""

    TimeoutException = httpx.TimeoutException

    def __init__(
        self,
        *,
        payload: dict[str, object] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._payload = payload
        self._exc = exc
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        assert self._payload is not None
        return FakeResponse(self._payload)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        ai_api_base="https://api.example.test/chat/completions",
        ai_api_key="sk-test",
        ai_model="test-model",
    )


def _enable_switch(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        # 预算凑成 2（响应里给 2 个 picks）；种子任务 fleet_lines 为 NULL，
        # 回落 `scheduler_config.fleet_line_limit`。
        config = session.get(orm.SchedulerConfigRow, 1)
        assert config is not None
        config.fleet_line_limit = 2
        row = session.get(orm.MilitaryAttackConfigRow, 1)
        assert row is not None
        row.ai_shadow_enabled = True
        session.commit()


def _working_pool(repository: SqlAlchemyRepository, session_factory: sessionmaker[Session]) -> None:
    for index, coordinate in enumerate(
        (Coordinate(4, 269, 8), Coordinate(4, 393, 10), Coordinate(9, 245, 14))
    ):
        add_bot_target(
            session_factory,
            coordinate,
            military_score=20_000.0 - index * 1_000,
            scanned_at=NOW,
        )
    enable(repository, MissionKind.BOT, params_json=BY_MILITARY)


def _picks_response() -> dict[str, object]:
    """合法且能通过硬校验的响应：2 个目标、都来自池子、预设 BBB、出发星球匹配。

    这是**模型返回**的形状——`content` 里才是那份 JSON（`_call_llm` 会先剥一层
    `choices[0].message.content`）。
    """
    content = {
        "picks": [
            {
                "target": "4:269:8",
                "origin": "4:277:15",
                "preset": "BBB",
                "military": 20000,
                "reading_age_hours": 0.0,
                "round_trip_minutes": 40,
                "reason": "示例",
            },
            {
                "target": "4:393:10",
                "origin": "4:277:15",
                "preset": "BBB",
                "military": 19000,
                "reading_age_hours": 0.5,
                "round_trip_minutes": 62,
                "reason": "示例",
            },
        ],
        "pool_warnings": [],
        "confidence": "high",
        "notes": "示例",
    }
    import json

    return {
        "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


def _run_and_wait(  # type: ignore[no-untyped-def]
    repository: SqlAlchemyRepository,
    session_factory: sessionmaker[Session],
    launcher,
    clock: Clock,
    fake: FakeHttpx,
    timeout_s: float = 5.0,
):
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("evo_helper.application.ai_targeting.httpx", fake)
        observer = AiShadowObserver(repository, _settings(), sample_size=60, timeout_s=5.0)
        scheduler = MissionScheduler(
            repository,
            make_supervisor(launcher, clock),
            clock=clock,
            origin=ORIGIN_A,
            ai_shadow=observer,
        )
        scheduler.prepare()
        _working_pool(repository, session_factory)
        _enable_switch(session_factory)
        row = task(repository, MissionKind.BOT)
        # 直接调真路，不等 tick；observer 另起线程落库。
        assignments = scheduler._military_assignments(row)  # noqa: SLF001
        assert assignments
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rows = repository.recent_ai_target_decisions(limit=5)
            if rows:
                return rows, fake
            time.sleep(0.05)
    raise AssertionError("worker 超时未落库")


def test_a_clean_response_is_recorded_as_ok(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    rows, fake = _run_and_wait(
        repository,
        session_factory,
        launcher,
        clock,
        FakeHttpx(payload=_picks_response()),
    )
    assert len(fake.calls) == 1
    # 请求体不该把 API key 带进消息正文。
    assert "sk-test" not in rows[0].prompt_text
    assert rows[0].status == "ok"
    assert rows[0].ai_picks_json is not None
    assert rows[0].model == "test-model"
    assert rows[0].latency_ms is not None
    assert rows[0].overlap is not None


def test_a_timeout_is_recorded_and_the_dispatch_is_untouched(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    fake = FakeHttpx(exc=httpx.TimeoutException("读超时"))
    rows, _ = _run_and_wait(repository, session_factory, launcher, clock, fake)
    assert rows[0].status == "timeout"
    assert rows[0].ai_picks_json is None


def test_a_http_error_is_recorded_and_the_dispatch_is_untouched(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    fake = FakeHttpx(exc=httpx.HTTPStatusError("401", request=None, response=None))  # type: ignore[arg-type]
    rows, _ = _run_and_wait(repository, session_factory, launcher, clock, fake)
    assert rows[0].status == "http_error"
    assert rows[0].ai_picks_json is None


def test_bad_json_is_recorded_as_invalid_json(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    # 模型返回一个带 JSON 包装但内容是乱码的字段。
    payload = {"choices": [{"message": {"content": "抱歉，我不明白你的意思"}}]}
    fake = FakeHttpx(payload=payload)
    rows, _ = _run_and_wait(repository, session_factory, launcher, clock, fake)
    assert rows[0].status == "invalid_json"
    assert rows[0].ai_picks_json is None
    assert rows[0].response_text == "抱歉，我不明白你的意思"
