"""★ 一期安全不变量：AI 选靶影子**绝不影响派遣**（需求文档第八节，验收第一项）。

## ⚠️ 这个文件为什么被重写过一次

第一版有一条叫「派遣逐字不变」的用例，但它**跑的时候开关是关的**——同文件另一条
用例断言 observer 一次都没被调用，正是证据。于是 baseline 和「被污染的那一路」
比的是同一条**没有 AI 的**路径，两边当然一样。2026-08-19 的审查做了变异实验
（在 `_military_assignments` 外面套一句「开关开着就把 assignments 反序」），
**AI 相关的 40 条用例全绿**。

这一版钉的是文档真正要的那条：**开关开着、假 LLM 返回一份合法但与算法完全不同的
选择，观测真的跑完并落了库，而 `_military_assignments` 的返回值与开关关着时逐字
相同。**

⚠️ **baseline 必须在开关打开之前取。** 开关是库里的全局配置，两个调度器共用；
开关打开之后再取 baseline 的话，「开关开着就反序」这种变异会把两边一起反掉，
用例照样全绿——这正是上一版的失败方式。
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.ai_targeting import AiShadowObserver
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    current_system_log_sink,
    install_system_log_sink,
    shutdown_system_log_sink,
)
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import add_bot_target, enable, set_score_window, task

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

BY_MILITARY = '{"by_military": true}'

ORIGIN_A = Coordinate(4, 277, 15)

#: 池子刻意给五个：预算 2，算法挑走两个之后还剩得下「完全不同」的另外两个。
#: 只给三个的话，AI 想不重合都做不到，用例就钉不住「合法但完全不同」这一档。
#: ⚠️ **池子要比「正选 + 备胎」再多留几个。**
#:
#: `_a_completely_different_but_legal_answer` 要从**没被算法挑中**的那些里凑出
#: 一份同样大小的 picks。2026-08-24 起分配阶段每条航线多配一个备胎
#: （`MILITARY_SPARE_FACTOR`），于是被挑中的数量翻倍、剩下的不够凑 —— 加两个。
POOL = (
    Coordinate(4, 269, 8),
    Coordinate(4, 393, 10),
    Coordinate(9, 245, 14),
    Coordinate(9, 244, 13),
    Coordinate(8, 80, 19),
    Coordinate(8, 214, 7),
    Coordinate(8, 311, 12),
)


class PoisonObserver:
    """无论收到什么参数都「成功发起」，但选择是垃圾（一期不执行它）。"""

    def __init__(self) -> None:
        self.calls = 0

    def observe(self, **kwargs: object) -> bool:  # noqa: ARG002
        self.calls += 1
        return True


class ExplodingObserver:
    """`observe()` 直接炸——观测侧的异常绝不能连锁到派遣。"""

    def __init__(self) -> None:
        self.calls = 0

    def observe(self, **kwargs: object) -> bool:  # noqa: ARG002
        self.calls += 1
        raise RuntimeError("LLM 彻底挂了")


class RecordingObserver:
    """记下有没有被调，开关关掉时必须一次都不调。"""

    def __init__(self) -> None:
        self.calls = 0

    def observe(self, **kwargs: object) -> bool:
        self.calls += 1
        return True


class CapturingObserver:
    """把 `observe()` 收到的入参原样留下来，供「喂的是不是全池」那条用例检查。"""

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def observe(self, **kwargs: Any) -> bool:
        self.seen.update(kwargs)
        return True


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

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(self._payload)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        ai_api_base="https://api.example.test/chat/completions",
        ai_api_key="sk-test",
        ai_model="test-model",
    )


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


def _make_scheduler(repository: SqlAlchemyRepository, clock: Clock, launcher, observer):  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(
        repository,
        make_supervisor(launcher, clock),
        clock=clock,
        origin=ORIGIN_A,
        ai_shadow=observer,
    )
    scheduler.prepare()
    return scheduler


@pytest.fixture(autouse=True)
def military_window(repository) -> None:  # type: ignore[no-untyped-def]
    """本模块的选靶窗口基线，摆在**全局**攻击配置里：有效期 2 小时、窗口门限 2 个。

    2026-08-23 起有效期与窗口门限是全局的（`military_attack_config`），不再是任务
    参数——从前它们就写在上面那串 JSON 里，一眼看得见。搬走之后若不摆，每条用例吃的
    都是代码默认值（2 小时 / **100 个**），而这个模块的候选池只有两三个目标：门限 100
    会让每一条用例都走「放弃窗口」那一支，于是本该量到的东西量不到，而用例照样是绿的。
    """
    set_score_window(repository, max_age_hours=2, window_floor=2)


def _a_working_pool(repository: SqlAlchemyRepository, session_factory) -> None:  # type: ignore[no-untyped-def]
    """窗口内目标够多、有启用出发星球的军力任务。"""
    for index, coordinate in enumerate(POOL):
        add_bot_target(
            session_factory,
            coordinate,
            military_score=20_000.0 - index * 1_000,
            scanned_at=NOW,
        )
    enable(repository, MissionKind.BOT, params_json=BY_MILITARY)


def _budget_of_two(session_factory: sessionmaker[Session]) -> None:
    """种子任务的 `fleet_lines` 为 NULL，回落 `scheduler_config.fleet_line_limit`。"""
    with session_factory() as session:
        config = session.get(orm.SchedulerConfigRow, 1)
        assert config is not None
        config.fleet_line_limit = 2
        session.commit()


def _turn_the_switch_on(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        row = session.get(orm.MilitaryAttackConfigRow, 1)
        assert row is not None
        row.ai_shadow_enabled = True
        session.commit()


def _a_completely_different_but_legal_answer(assignments: Any) -> dict[str, object]:
    """一份**合法**（过得了硬校验）但与算法**完全不重合**的 picks。

    「垃圾」不止「乱码」一种。最危险的那一档恰恰是这一档：格式完全正确、
    数量正好、坐标都在给过的集合里——如果哪天有人把 AI 的结果接进派遣，
    出错的就会是这种「看起来正常」的输入，而不是一眼能认的乱码。
    """
    chosen = {item.coordinate for item in assignments}
    others = [coordinate for coordinate in POOL if coordinate not in chosen]
    # ⚠️ **要凑的份数按正选算，不按 `assignments` 的长度算。**
    # 2026-08-24 起 `assignments` 里还含备胎（`MILITARY_SPARE_FACTOR`，每条航线
    # 多配一个），于是它的长度是正选的两倍 —— 拿它当「要凑几个」会让这个前置条件
    # 凭空翻倍，而这条用例要的只是「和算法挑的完全不重合」。
    wanted = sum(1 for item in assignments if not item.reserve)
    assert len(others) >= wanted, "池子不够大，凑不出「完全不同」的一份"
    preset = assignments[0].preset
    origin = assignments[0].origin
    content = {
        "picks": [
            {
                "target": f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}",
                "origin": f"{origin.galaxy}:{origin.system}:{origin.position}",
                "preset": preset,
                "rank": index + 1,
                "reason": "刻意与算法完全不同",
            }
            for index, coordinate in enumerate(others[:wanted])
        ],
        "pool_warnings": [],
        "confidence": "high",
        "notes": "影子模式，不会被执行",
    }
    return {
        "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _wait_for_a_recorded_round(repository: SqlAlchemyRepository, timeout_s: float = 10.0):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = repository.recent_ai_target_decisions(limit=5)
        if rows:
            return rows
        time.sleep(0.05)
    raise AssertionError("影子观测线程超时未落库——这一条用例失去意义，不许当成通过")


def test_the_dispatch_is_byte_for_byte_unchanged_with_a_live_poisoned_llm(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """★ 验收第一项：**开关开着、假 LLM 真的答了一份完全不同的选择**，派遣逐字不变。

    这一条是整个一期安全边界的唯一保障，所以它必须自证「真的跑到了那条路上」：
    - `fake.calls == 1`：LLM 那一路真的被走过（开关关着时它是 0）；
    - 落库那一行 `status == "ok"`、`overlap == 0`：那份「完全不同」的答案
      不是被当成垃圾丢掉的，而是**通过了硬校验、被完整记了下来**；
    - 然后才轮到 `poisoned == baseline`。

    ⚠️ **baseline 在开关打开之前取**，理由见模块头。
    """
    baseline_scheduler = _make_scheduler(repository, clock, launcher, None)
    _budget_of_two(session_factory)
    _a_working_pool(repository, session_factory)
    row = task(repository, MissionKind.BOT)

    # ① 开关还关着：这就是「没接 AI 时」的派遣结果。
    baseline = baseline_scheduler._military_assignments(row)  # noqa: SLF001
    assert len(baseline) >= 2, "至少两发才分得出「反序」这种变异"

    # ② 打开开关，接上一个会答出「合法但完全不同」的假 LLM。
    payload = _a_completely_different_but_legal_answer(baseline)
    fake = FakeHttpx(payload)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("evo_helper.application.ai_targeting.httpx", fake)
        observer = AiShadowObserver(repository, _settings(), sample_size=60, timeout_s=5.0)
        poisoned_scheduler = _make_scheduler(repository, clock, launcher, observer)
        _turn_the_switch_on(session_factory)
        poisoned = poisoned_scheduler._military_assignments(row)  # noqa: SLF001
        rows = _wait_for_a_recorded_round(repository)

    # ③ 先证明这一路真的走到底了，再谈「逐字不变」。
    assert len(fake.calls) == 1, "假 LLM 一次都没被调——开关没生效，这条用例是空的"
    assert rows[0].status == "ok", f"那份合法答案没通过硬校验：{rows[0].violations_json}"
    assert rows[0].overlap == 0, "AI 的选择该与算法完全不重合，用例的前提没成立"
    assert rows[0].ai_picks_json is not None

    # ④ ★ 逐字不变。
    assert poisoned == baseline


def test_the_dispatch_is_byte_for_byte_unchanged_when_the_llm_answers_garbage(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """同上，但假 LLM 答的是**乱码**：`invalid_json` 那一档也不许动派遣。"""
    baseline_scheduler = _make_scheduler(repository, clock, launcher, None)
    _budget_of_two(session_factory)
    _a_working_pool(repository, session_factory)
    row = task(repository, MissionKind.BOT)
    baseline = baseline_scheduler._military_assignments(row)  # noqa: SLF001

    fake = FakeHttpx({"choices": [{"message": {"content": "抱歉，我不明白你的意思"}}]})
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("evo_helper.application.ai_targeting.httpx", fake)
        observer = AiShadowObserver(repository, _settings(), sample_size=60, timeout_s=5.0)
        poisoned_scheduler = _make_scheduler(repository, clock, launcher, observer)
        _turn_the_switch_on(session_factory)
        poisoned = poisoned_scheduler._military_assignments(row)  # noqa: SLF001
        rows = _wait_for_a_recorded_round(repository)

    assert len(fake.calls) == 1
    assert rows[0].status == "invalid_json"
    assert poisoned == baseline


def test_an_exploding_observer_never_breaks_the_dispatch(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """LLM 调用炸掉（`observe` 抛异常）时调度继续：**不因 LLM 挂而停摆**。

    ⚠️ 开关必须**开着**，否则 `observe` 压根不会被调，这条用例又是空的——
    所以 `observer.calls == 1` 与「派遣逐字不变」一起断言。
    """
    baseline_scheduler = _make_scheduler(repository, clock, launcher, None)
    _budget_of_two(session_factory)
    _a_working_pool(repository, session_factory)
    row = task(repository, MissionKind.BOT)
    baseline = baseline_scheduler._military_assignments(row)  # noqa: SLF001

    observer = ExplodingObserver()
    scheduler = _make_scheduler(repository, clock, launcher, observer)
    _turn_the_switch_on(session_factory)
    assignments = scheduler._military_assignments(row)  # noqa: SLF001

    assert observer.calls == 1, "observe 一次都没被调——开关没生效，这条用例是空的"
    assert assignments == baseline


def test_a_poisoned_observer_that_claims_success_never_touches_the_dispatch(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """observer 声称「发起成功」也一样：返回值一个字不动。"""
    baseline_scheduler = _make_scheduler(repository, clock, launcher, None)
    _budget_of_two(session_factory)
    _a_working_pool(repository, session_factory)
    row = task(repository, MissionKind.BOT)
    baseline = baseline_scheduler._military_assignments(row)  # noqa: SLF001

    observer = PoisonObserver()
    poisoned_scheduler = _make_scheduler(repository, clock, launcher, observer)
    _turn_the_switch_on(session_factory)
    poisoned = poisoned_scheduler._military_assignments(row)  # noqa: SLF001

    assert observer.calls == 1, "observe 一次都没被调——开关没生效，这条用例是空的"
    assert poisoned == baseline


def test_the_switch_off_means_the_observer_is_never_called(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """开关默认关：`_observe_ai_shadow` 第一行就返回，observer 一次都不被调。"""
    scheduler = _make_scheduler(repository, clock, launcher, RecordingObserver())
    _budget_of_two(session_factory)
    _a_working_pool(repository, session_factory)
    row = task(repository, MissionKind.BOT)
    assignments = scheduler._military_assignments(row)  # noqa: SLF001
    assert assignments
    assert scheduler._ai_shadow.calls == 0  # type: ignore[union-attr]


def test_the_observer_gets_the_whole_pool_not_the_filtered_one(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """★ 喂进去的必须是 `candidates`（全池），不是 `eligible`（四步筛完的）。

    ⚠️ `eligible` 已经被 `score_max_age_hours`（读数窗口）、`top_n`（窗口门限）
    和 `max_score`（军力上限）裁过，而这三个旋钮的**值一个都没进 prompt**——
    喂它等于「旋钮的数值不给、筛选效果照给」，把答案先塞给 AI。

    这里放一颗**读数超出窗口**的目标：它在 `candidates` 里、不在 `eligible` 里，
    所以只要观测拿到的池子包含它，就证明喂的是全池。
    """
    observer = CapturingObserver()
    scheduler = _make_scheduler(repository, clock, launcher, observer)
    _budget_of_two(session_factory)
    _a_working_pool(repository, session_factory)
    stale = Coordinate(2, 111, 5)
    add_bot_target(
        session_factory,
        stale,
        military_score=99_000.0,
        # 窗口是 2 小时（`BY_MILITARY`），这一颗的读数是 9 小时前的。
        scanned_at=NOW.replace(hour=3),
    )

    _turn_the_switch_on(session_factory)
    row = task(repository, MissionKind.BOT)
    reading = scheduler._military_pool_reading(row)  # noqa: SLF001
    assert stale in {item.coordinate for item in reading.candidates}
    assert stale not in {item.coordinate for item in reading.eligible}, (
        "这颗该被窗口挡在 eligible 之外，用例的前提没成立"
    )

    scheduler._military_assignments(row)  # noqa: SLF001

    assert "candidates" in observer.seen, "observe 的入参还叫 eligible？全池那一改没落到位"
    handed_over = {item.coordinate for item in observer.seen["candidates"]}
    assert stale in handed_over, "喂给 AI 的还是筛完的 eligible，不是全池 candidates"


class BoomOnceObserver:
    """`observe()` 抛异常，用来验调度器一侧那句 `except` 有没有留痕。"""

    def observe(self, **kwargs: object) -> bool:  # noqa: ARG002
        raise RuntimeError("观测侧炸了")


def test_the_scheduler_records_why_it_skipped_the_shadow(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """★ `_observe_ai_shadow` 那两句 `except` **以前一个字都不记**。

    ⚠️ 用户把开关打开、库里什么都没多出来，排障时无从下手——判据不是
    「有没有打日志」，是**出事时能不能只靠库里的日志定位**，所以异常的类型与
    消息都要落到 `system_log` 上。
    """
    records: list[SystemLogRecord] = []
    install_system_log_sink(
        SystemLogSink(records.extend, flush_interval_s=0.01), context=SystemLogContext()
    )
    try:
        scheduler = _make_scheduler(repository, clock, launcher, BoomOnceObserver())
        _budget_of_two(session_factory)
        _a_working_pool(repository, session_factory)
        _turn_the_switch_on(session_factory)
        row = task(repository, MissionKind.BOT)
        assert scheduler._military_assignments(row)  # noqa: SLF001 - 派遣照常
        sink = current_system_log_sink()
        assert sink is not None
        assert sink.flush(timeout=5)
    finally:
        shutdown_system_log_sink()

    skipped = [item for item in records if "AI 选靶影子" in item.message and "跳过" in item.message]
    assert skipped, "观测被 except 挡掉却一个字都没记"
    assert "RuntimeError" in skipped[0].message, "日志没说清是什么异常，排障时定位不了"
    assert "观测侧炸了" in skipped[0].message
