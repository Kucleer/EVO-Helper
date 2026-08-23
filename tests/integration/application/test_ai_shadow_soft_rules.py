"""软核对里的**规则遵守**那两条：撞在保护期里 / 距我方上次攻击不足 8 小时。

「规则遵守率」是一期最重要的评估口径（方案 4.3 第 1 条：「这一条不合格就没有
下文」）。所以这两条判据必须**真的能被触发**，而且要沿着生产那条链路触发——
事实从 `attack_dispatches` 和 `bot_targets.protection_seen_at_utc` 里查出来，
经 `_soft_reference` 变成 `SoftReference`，再由 `soft_check_picks` 判。

## ⚠️ 一条要说清楚的事实：这两条什么时候才够得着

选靶第 1 步（`_military_candidates`）本来就会排掉两批：

- `bot_revisit_hours`（默认 **24** 小时）之内打过的；
- `protection_exclusion_hours`（默认 **8** 小时）之内撞过保护期的。

`candidates` 与 `eligible` 都在这一步之后，所以**换池子并不会让这两条判据从
「够不着」变成「够得着」**——第 2--4 步筛的是读数窗口、窗口门限和军力上限，
与保护期、攻击间隔无关。

真正决定它们够不够得着的是那两个旋钮：

| 旋钮 | 默认 | 这两条能触发吗 |
|---|---|---|
| `bot_revisit_hours` = 24（默认） | ≥ 8 | **不能**：候选里不可能有 8 小时内打过的 |
| `bot_revisit_hours` = 1--7 | < 8 | **能**，正是下面第一条用例 |
| `protection_exclusion_hours` = 8（默认） | = 8 | **不能**：与游戏规则严丝合缝互补 |
| `protection_exclusion_hours` = 1--7 | < 8 | **能**，正是下面第二条用例 |

两个旋钮页面上都允许填到 1 小时（`_bot_revisit_hours` / `_protection_exclusion_hours`
的下界），所以这不是假想场景：想多榨几轮的人会把复访调小，而**游戏的 8 小时
保护期不会跟着变**——那一刻 AI 就可能推荐一个打不动的目标，而这两条正是用来
把它记下来的。
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.ai_targeting import AiShadowObserver
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_BOT
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import add_bot_target, dispatch, enable, set_score_window, task

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

BY_MILITARY = '{"by_military": true}'

ORIGIN_A = Coordinate(4, 277, 15)

#: 这一颗是「刚打过 / 刚撞过保护期」的那个。
HOT = Coordinate(4, 269, 8)
#: 陪跑的，把预算凑够、也让 AI 有第二个可选。
COLD = Coordinate(4, 393, 10)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class FakeHttpx:
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


def _answer_picking(target: Coordinate, preset: str = "BBB") -> dict[str, object]:
    """AI 只挑那一颗「打不动」的。预算是 1，所以这一份过得了硬校验。"""
    content = {
        "picks": [
            {
                "target": f"{target.galaxy}:{target.system}:{target.position}",
                "origin": f"{ORIGIN_A.galaxy}:{ORIGIN_A.system}:{ORIGIN_A.position}",
                "preset": preset,
                "rank": 1,
                "reason": "刻意挑一个规则上打不动的",
            }
        ],
        "pool_warnings": [],
        "confidence": "low",
        "notes": "用例构造",
    }
    return {
        "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _configure(
    session_factory: sessionmaker[Session],
    *,
    bot_revisit_hours: int | None = None,
    protection_exclusion_hours: int | None = None,
) -> None:
    """打开影子开关，并把那两个排除窗口调到游戏的 8 小时以下。

    ⚠️ 调小它们**不是为了让用例好写**，而是这两条软核对判据在生产上唯一够得着的
    配置——理由整段写在模块头。
    """
    with session_factory() as session:
        scheduler_config = session.get(orm.SchedulerConfigRow, 1)
        assert scheduler_config is not None
        scheduler_config.fleet_line_limit = 1
        row = session.get(orm.MilitaryAttackConfigRow, 1)
        assert row is not None
        row.ai_shadow_enabled = True
        if bot_revisit_hours is not None:
            row.bot_revisit_hours = bot_revisit_hours
        if protection_exclusion_hours is not None:
            row.protection_exclusion_hours = protection_exclusion_hours
        session.commit()


def _pool(repository: SqlAlchemyRepository, session_factory) -> None:  # type: ignore[no-untyped-def]
    add_bot_target(session_factory, HOT, military_score=30_000.0, scanned_at=NOW)
    add_bot_target(session_factory, COLD, military_score=10_000.0, scanned_at=NOW)
    enable(repository, MissionKind.BOT, params_json=BY_MILITARY)


def _round_started_at(session_factory: sessionmaker[Session], moment: datetime) -> None:
    """把本轮起点推到 `moment`。

    ⚠️ **这一步不能省。** 第 1 步还有一道 `phase_of` 判据：本轮里已经派过的目标
    一律不再进候选（战报没回来是「在等」、回来了是「走完」）。所以「刚打过却仍在
    候选池里」只可能发生在**上一轮打的、这一轮又轮到它**——正是这里构造的样子。
    """
    with session_factory() as session:
        row = next(
            item
            for item in session.query(orm.MissionTaskRow).all()
            if item.kind == MissionKind.BOT.value
        )
        row.round_started_at_utc = moment
        session.commit()


def _observe_one_round(  # type: ignore[no-untyped-def]
    repository: SqlAlchemyRepository,
    launcher,
    clock: Clock,
    fake: FakeHttpx,
    row: orm.MissionTaskRow,
):
    """接上假 httpx 跑一次 `_military_assignments`，等观测线程落库，返回那几行。"""
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
        # 选靶窗口那两格 2026-08-23 起是全局的（`military_attack_config`），
        # 不再是任务参数。这个模块的候选池只有两三个目标，门限若吃代码默认值
        # （100）就每一轮都走「放宽窗口」那一支，本该量到的东西量不到。
        set_score_window(repository, max_age_hours=2, window_floor=1)
        assert scheduler._military_assignments(row)  # noqa: SLF001
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            rows = repository.recent_ai_target_decisions(limit=5)
            if rows:
                return rows
            time.sleep(0.05)
    raise AssertionError("影子观测线程超时未落库——这条用例失去意义，不许当成通过")


def _codes(row: Any) -> set[str]:
    return {str(item.get("code", "")) for item in json.loads(row.violations_json or "[]")}


def test_a_pick_we_attacked_three_hours_ago_is_flagged(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock, run_id
) -> None:
    """★ `rule_attacked_too_recently`：复访窗口调成 2 小时，3 小时前打过的又进了池子。

    ⚠️ **判据必须是游戏规则的 8 小时，不是那个 24 小时旋钮**（需求 5.3 第 2 条）。
    这一条同时钉住「上次攻击时刻从 `attack_dispatches` 算」——
    `bot_targets.last_attack_at_utc` 那一列从来没被写过，读它恒得「从未打过」，
    这条用例会立刻转红。
    """
    observer_scheduler = MissionScheduler(
        repository, make_supervisor(launcher, clock), clock=clock, origin=ORIGIN_A
    )
    observer_scheduler.prepare()
    _configure(session_factory, bot_revisit_hours=2)
    _pool(repository, session_factory)
    # 上一轮打的（本轮起点之后就不会再进候选，见 `_round_started_at`）。
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=HOT,
        dispatched_at=NOW - timedelta(hours=3),
        origin=ORIGIN_A,
        flight=timedelta(minutes=20),
    )
    _round_started_at(session_factory, NOW - timedelta(hours=1))

    row = task(repository, MissionKind.BOT)
    # 前提：这一颗真的还在候选池里，否则这条用例什么都没验到。
    reading = observer_scheduler._military_pool_reading(row)  # noqa: SLF001
    assert HOT in {item.coordinate for item in reading.candidates}, (
        "前提没成立：3 小时前打过的那一颗没能回到候选池，复访窗口没调下来？"
    )

    fake = FakeHttpx(_answer_picking(HOT))
    rows = _observe_one_round(repository, launcher, clock, fake, row)

    assert len(fake.calls) == 1, "假 LLM 没被调——开关或前提没生效，用例是空的"
    assert rows[0].status == "ok", f"硬校验没过，软核对根本没跑：{rows[0].violations_json}"
    assert "rule_attacked_too_recently" in _codes(rows[0])


def test_a_pick_inside_the_game_protection_period_is_flagged(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """★ `rule_in_protection`：排除窗口调成 2 小时，3 小时前撞过保护期的又进了池子。

    ⚠️ 保护期到什么时候是**估**出来的：`protection_seen_at_utc + 游戏规则 8 小时`。
    我们只知道「在那一刻撞上了」，不知道保护期是什么时候开始的
    （`game.pirate_ui.DIALOG_NO_MISSION`）——所以这是上界，不是精确值。
    """
    observer_scheduler = MissionScheduler(
        repository, make_supervisor(launcher, clock), clock=clock, origin=ORIGIN_A
    )
    observer_scheduler.prepare()
    _configure(session_factory, protection_exclusion_hours=2)
    _pool(repository, session_factory)
    repository.note_protection_period(HOT, seen_at_utc=NOW - timedelta(hours=3))

    row = task(repository, MissionKind.BOT)
    reading = observer_scheduler._military_pool_reading(row)  # noqa: SLF001
    assert HOT in {item.coordinate for item in reading.candidates}, (
        "前提没成立：3 小时前撞过保护期的那一颗没能回到候选池"
    )

    fake = FakeHttpx(_answer_picking(HOT))
    rows = _observe_one_round(repository, launcher, clock, fake, row)

    assert len(fake.calls) == 1
    assert rows[0].status == "ok", f"硬校验没过，软核对根本没跑：{rows[0].violations_json}"
    assert "rule_in_protection" in _codes(rows[0])


def test_a_clean_pick_carries_no_rule_violation(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """反面：既没打过也没撞过保护期的那一颗，两条规则一条都不该记。

    ⚠️ 少了这一条，把 `soft_check_picks` 改成「无条件记一条」也能让上面两条全绿。
    """
    MissionScheduler(
        repository, make_supervisor(launcher, clock), clock=clock, origin=ORIGIN_A
    ).prepare()
    _configure(session_factory, bot_revisit_hours=2, protection_exclusion_hours=2)
    _pool(repository, session_factory)

    row = task(repository, MissionKind.BOT)
    fake = FakeHttpx(_answer_picking(HOT))
    rows = _observe_one_round(repository, launcher, clock, fake, row)

    assert rows[0].status == "ok"
    codes = _codes(rows[0])
    assert "rule_attacked_too_recently" not in codes
    assert "rule_in_protection" not in codes


def test_the_default_knobs_put_both_rules_out_of_reach(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock, run_id
) -> None:
    """⚠️ **把「默认配置下这两条够不着」这件事钉在用例里。**

    默认 `bot_revisit_hours=24`、`protection_exclusion_hours=8`，选靶第 1 步的
    排除与游戏的 8 小时保护期严丝合缝（甚至更保守），所以候选池里**不可能**出现
    「8 小时内打过」或「还在保护期里」的目标——两条规则恒不触发。

    这不是缺陷，是「代价不对称、宁可过度排除」那条策略的必然结果
    （`DEFAULT_PROTECTION_EXCLUSION` 的注释）。写成用例是因为**它决定了
    「规则遵守率」这个验收指标在默认配置下量到的是什么**：量到的是 100%，
    而那个 100% 来自第 1 步的排除，不是来自 AI 守规矩。看指标的人必须知道这件事。
    """
    scheduler = MissionScheduler(
        repository, make_supervisor(launcher, clock), clock=clock, origin=ORIGIN_A
    )
    scheduler.prepare()
    _configure(session_factory)  # 两个窗口都不动 = 走默认
    _pool(repository, session_factory)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=HOT,
        dispatched_at=NOW - timedelta(hours=3),
        origin=ORIGIN_A,
        flight=timedelta(minutes=20),
    )
    repository.note_protection_period(COLD, seen_at_utc=NOW - timedelta(hours=3))
    _round_started_at(session_factory, NOW - timedelta(hours=1))

    row = task(repository, MissionKind.BOT)
    candidates = {item.coordinate for item in scheduler._military_pool_reading(row).candidates}  # noqa: SLF001
    assert HOT not in candidates, "默认 24 小时复访窗口该把 3 小时前打过的挡在候选池外"
    assert COLD not in candidates, "默认 8 小时排除该把 3 小时前撞过保护期的挡在候选池外"
