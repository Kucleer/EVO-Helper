"""军力那两条日志的限流：**判定变了立刻写，没变则一个窗口最多一条。**

## 这一条修的是什么

生产实测（2026-08-18 16:00 那一小时，`system_log` 全表 25,826 条）：

| 日志 | 那一小时的行数 | 其中内容不同的 |
|---|---|---|
| 「军力候选池：…」INFO | 6,078 | **38** |
| 「军力读数放宽窗口」WARNING | 6,077 | **37** |

两条合起来占了全表的 44%，而其中 12,080 行**一个新事实都没带来**。同一秒里同一句
话最多重复 4 次——成因是 `tick()` 里那个 `for _ in range(len(MissionKind))`：
`_step` 一个 tick 会转好几圈，每圈都要组一次命令行。

原先两个函数的 docstring 都写着「不限流，因为一轮出击一条」。**那句规格是错的**，
不是实现跑偏了。

## 四条判据

- (a) 状态持续不变时，一个窗口里只写一条；
- (b) 状态**跃迁**时立刻写，不被时间窗压掉；
- (c) 被压掉的那些在下一条里交代清楚，而且**不许撒谎**——跃迁那一条不能把旧状态
      的重复次数说成「这一判定已持续」；
- (d) 签名覆盖消息里的每一个数，少覆盖一个就会把「内容已经变了」压成沉默。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.mission_scheduler import (
    REPEATED_LOG_WINDOW,
    MilitaryPoolReading,
    MissionScheduler,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import add_bot_target, enable, only_gap_filler, task

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: 窗口 2 小时、军力截断要 2 个。窗口内够不够由用例自己摆。
BY_MILITARY = '{"by_military": true, "top_n": 2, "score_max_age_hours": 2}'


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


class RecordingLog:
    """把 `record_system_log` 的调用记下来。签名与真的那一个一致。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, level, source, message, *, payload=None, logged_at_utc=None):  # type: ignore[no-untyped-def]
        self.entries.append((level, message, dict(payload or {})))

    def of(self, prefix: str) -> list[tuple[str, str, dict[str, object]]]:
        return [item for item in self.entries if item[1].startswith(prefix)]

    @property
    def pool(self) -> list[tuple[str, str, dict[str, object]]]:
        return self.of("军力候选池")

    @property
    def widened(self) -> list[tuple[str, str, dict[str, object]]]:
        return self.of("军力读数放宽窗口")


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


# -- 夹具 ----------------------------------------------------------------------


def a_healthy_pool(repository: SqlAlchemyRepository, session_factory) -> orm.MissionTaskRow:  # type: ignore[no-untyped-def]
    """窗口内目标够多的军力任务：**不放宽**。放宽那条一个字都不该写。"""
    for index in range(4):
        add_bot_target(
            session_factory,
            Coordinate(2, 400 + index, 5),
            military_score=9_000.0 - index,
            scanned_at=NOW,
        )
    enable(repository, MissionKind.BOT, params_json=BY_MILITARY)
    only_gap_filler(repository)
    return task(repository, MissionKind.BOT)


def a_widened_pool(repository: SqlAlchemyRepository, session_factory) -> orm.MissionTaskRow:  # type: ignore[no-untyped-def]
    """窗口内只有 1 个、截断要 2 个：**放弃窗口**，那条 WARNING 该响。"""
    add_bot_target(
        session_factory,
        Coordinate(2, 400, 5),
        military_score=99_999.0,
        scanned_at=NOW - timedelta(days=3),
    )
    add_bot_target(
        session_factory,
        Coordinate(2, 401, 6),
        military_score=8_000.0,
        scanned_at=NOW - timedelta(days=3),
    )
    add_bot_target(session_factory, Coordinate(2, 402, 7), military_score=100.0, scanned_at=NOW)
    enable(repository, MissionKind.BOT, params_json=BY_MILITARY)
    only_gap_filler(repository)
    return task(repository, MissionKind.BOT)


def log_once(  # type: ignore[no-untyped-def]
    scheduler: MissionScheduler, row: orm.MissionTaskRow
) -> MilitaryPoolReading:
    """走一遍生产那条路：**现算一份账目，再把它写进日志**。"""
    reading = scheduler._military_pool_reading(row)  # noqa: SLF001 - 钉的就是这一层
    scheduler._log_the_military_pipeline(row, reading)  # noqa: SLF001
    return reading


# -- (a) 状态不变：一个窗口一条 ------------------------------------------------


def test_an_unchanged_pool_writes_once_per_window(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **用例 (a)：账目一个数都没变时，一个窗口里只落一条。**

    时钟一格一格往前挪、每格都写一次，走满整整一个窗口。**一次跳到底的话，任何
    「隔 N 秒才写」的实现都能因为「第一次看见它就是最后一刻」蒙混过关**；分成
    多格走完，只限流不去重的实现必然在中途多落几条。

    生产那一小时是 6,078 行、38 条内容不同——这里量的正是那 6,040 行被压到哪去了。
    """
    row = a_healthy_pool(repository, session_factory)

    # 累计 0 / 1 / 2 / 32 / 112 秒——全部落在 120 秒的窗口里。
    for seconds in (0, 1, 1, 30, 80):
        clock.now += timedelta(seconds=seconds)
        log_once(scheduler, row)

    assert len(recorded.pool) == 1, f"一个窗口里只该落一条，落了 {len(recorded.pool)} 条"

    # 越过窗口再写一次：这一条要落库，因为「这个状态还在持续」本身就是要看的事实。
    clock.now = NOW + REPEATED_LOG_WINDOW + timedelta(seconds=1)
    log_once(scheduler, row)

    assert len(recorded.pool) == 2


# -- (b) 状态跃迁：立刻写 ------------------------------------------------------


def test_a_changed_pool_is_written_at_once_even_inside_the_window(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **用例 (b)：账目变了就立刻写，时间窗压不住它。**

    压住的代价是「候选池 16:13 从 519 掉到 442」这种时刻整个读不出来——而那正是
    排障时唯一有用的东西。窗口是 120 秒，这里只过了 1 秒。

    第二次写之前往库里多放一颗目标：`attackable` 与 `with_readings` 各 +1，
    这就是一次真的跃迁。
    """
    row = a_healthy_pool(repository, session_factory)
    log_once(scheduler, row)
    assert len(recorded.pool) == 1

    clock.now += timedelta(seconds=1)
    add_bot_target(session_factory, Coordinate(2, 410, 9), military_score=7_000.0, scanned_at=NOW)
    reading = log_once(scheduler, row)

    assert len(recorded.pool) == 2, "跃迁被时间窗压掉了，那一刻在库里就没有了"
    assert recorded.pool[1][2]["attackable"] == reading.attackable
    assert recorded.pool[1][2]["signature_changed"] is True


# -- (c) 被压掉的信息不许丢，也不许说岔 ----------------------------------------


def test_the_suppressed_repeats_are_carried_into_the_next_line(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **用例 (c)：被压掉的次数与横跨的时长都要在下一条里说出来。**

    只限流不计数的话，「同一句话刷了 6,040 遍」和「老老实实写了一条」在库里长得
    一模一样，而那两件事的善后完全相反：前者说明 `_step` 在空转，后者说明一切正常。

    这里连写 5 次（第 1 次落库、后 4 次被压），越过窗口再写第 6 次。
    """
    row = a_healthy_pool(repository, session_factory)
    for index in range(5):
        clock.now = NOW + timedelta(seconds=index)
        log_once(scheduler, row)
    assert len(recorded.pool) == 1

    clock.now = NOW + REPEATED_LOG_WINDOW + timedelta(seconds=10)
    log_once(scheduler, row)

    assert len(recorded.pool) == 2
    _, message, payload = recorded.pool[1]
    assert payload["suppressed_since_last_log"] == 4
    assert payload["suppressed_span_seconds"] == 130
    assert "4 次" in message, f"被压掉几次没写进消息里：{message}"
    assert "2.2 分钟" in message, f"被压掉的那一段横跨多久没写进消息里：{message}"


def test_a_transition_does_not_claim_the_old_repeats_as_its_own(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **用例 (c) 的另一半：跃迁那一条不许把旧状态的重复次数说成自己的。**

    被压掉的那些按构造与**上一条落库的**一字不差。所以「已持续 N 分钟」这种说法
    只在「这一条和上一条内容相同」时才成立；跃迁那一条要是照抄这句措辞，读日志的
    人会以为**新**的判定已经稳定了那么久——而它其实才刚出现。
    仓库的规矩是「日志说假话比不说更糟」。

    这一条同时守住 payload 里的 `signature_changed`：机器要能分清这两种。
    """
    row = a_healthy_pool(repository, session_factory)
    for index in range(4):
        clock.now = NOW + timedelta(seconds=index)
        log_once(scheduler, row)
    assert len(recorded.pool) == 1

    # 窗口还没走完就让账目变一变：这一条走的是「跃迁」那一支。
    clock.now = NOW + timedelta(seconds=20)
    add_bot_target(session_factory, Coordinate(2, 410, 9), military_score=7_000.0, scanned_at=NOW)
    log_once(scheduler, row)

    assert len(recorded.pool) == 2
    _, message, payload = recorded.pool[1]
    assert payload["signature_changed"] is True
    assert payload["suppressed_since_last_log"] == 3
    assert "上一条" in message, f"没交代被压掉的是上一条的账：{message}"
    assert "已持续" not in message, f"把旧状态的重复算到新状态头上了：{message}"


# -- (d) 签名必须覆盖消息里的每一个数 ------------------------------------------


def test_every_number_in_the_message_is_part_of_the_signature(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **用例 (d)：消息不同的两条，不许被限流当成「一样」压掉一条。**

    这是这道闸最危险的失效方式：签名少覆盖一个数 → 内容已经变了的日志被压成沉默
    → 库里留着的上一条**还在假装现状没变**。那比不打日志糟得多。

    所以签名由 `_line_signature(message, payload)` 结构性地取全文，而不是手写一份
    「哪几个数算数」的清单。这里逐个字段改一遍，每改一次都必须多落一条。
    """
    row = a_healthy_pool(repository, session_factory)
    base = scheduler._military_pool_reading(row)  # noqa: SLF001
    scheduler._log_the_military_pipeline(row, base)  # noqa: SLF001
    assert len(recorded.pool) == 1

    written = 1
    for changed in (
        # 每一个都只动一处，而且都在窗口之内（都是 NOW），压不住才算过。
        base.__class__(**{**vars(base), "take": base.take + 1}),
        base.__class__(**{**vars(base), "max_age": base.max_age + timedelta(hours=1)}),
        base.__class__(**{**vars(base), "candidates": base.candidates[:-1]}),
    ):
        scheduler._log_the_military_pipeline(row, changed)  # noqa: SLF001
        written += 1
        assert len(recorded.pool) == written, (
            f"改了一处却被当成没变过压掉了：{recorded.pool[-1][1]}"
        )


# -- 放宽窗口那条 WARNING ------------------------------------------------------


def test_the_widened_warning_is_throttled_the_same_way(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """放宽窗口那条走同一道闸：生产那一小时它自己就写了 6,077 行。

    ⚠️ **级别仍然必须是 WARNING。** 限流是把重复压掉，不是把它降级——降成 INFO
    就等于把「用了旧数据要有人告诉你」那一半退回去了。
    """
    row = a_widened_pool(repository, session_factory)
    for index in range(5):
        clock.now = NOW + timedelta(seconds=index)
        log_once(scheduler, row)

    assert len(recorded.widened) == 1
    level, _, payload = recorded.widened[0]
    assert level == "WARNING", "限流不许顺手把告警降级"
    assert payload["in_window"] == 1
    assert payload["take"] == 2


def test_a_window_that_recovered_says_so_once(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """从「放宽」跌回「正常」时补一条收口，**而且只补一条**。

    只报开头不报结尾的话，翻日志的人读不出这一段有多长——而「放宽持续了多久」正是
    判断该不该调有效期的那个数。

    ⚠️ **收口那条只在跃迁那一下写。** 让它也吃窗口兜底的话，一个长期正常的任务会
    每 120 秒刷一句「已恢复」——那是把刷屏换了个句子而已。所以恢复之后再怎么写都
    不许多出第二条。
    """
    row = a_widened_pool(repository, session_factory)
    log_once(scheduler, row)
    assert len(recorded.widened) == 1
    assert recorded.widened[0][0] == "WARNING"

    # 军力榜扫到了新的一批：窗口内够 2 个了，不再放宽。
    clock.now = NOW + timedelta(minutes=5)
    add_bot_target(
        session_factory, Coordinate(2, 403, 8), military_score=200.0, scanned_at=clock.now
    )
    log_once(scheduler, row)

    assert len(recorded.widened) == 2
    level, message, payload = recorded.widened[1]
    assert level == "INFO", "收口那条不该是 WARNING，正常了还响就是狼来了"
    assert "已恢复" in message
    assert payload["widened"] is False

    # 之后一直正常：不许再写第三条，哪怕窗口早就过去了。
    for minutes in (6, 10, 60):
        clock.now = NOW + timedelta(minutes=minutes)
        log_once(scheduler, row)
    assert len(recorded.widened) == 2, "「已恢复」变成了每窗口一条的刷屏"


def test_a_pool_that_never_widened_says_nothing(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """从头到尾都正常的任务，放宽那条一个字都不写——连「已恢复」都不写。

    没响过的告警去「恢复」它，读日志的人只会以为刚才出过事。
    """
    row = a_healthy_pool(repository, session_factory)
    for minutes in (0, 5, 60):
        clock.now = NOW + timedelta(minutes=minutes)
        log_once(scheduler, row)

    assert recorded.widened == []
    assert len(recorded.pool) >= 1, "候选池那条该照写"


# -- 走真正那条路 --------------------------------------------------------------


def test_the_real_dispatch_path_is_throttled_too(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """限流长在 `_military_assignments` 那条真路上，不是只长在测试直接调的那一层。

    生产那一小时的成因就是这条路一个 tick 走好几遍（`tick()` 里的
    `for _ in range(len(MissionKind))`）。这里同一秒内连走四遍——正是实测到的
    「同一秒最多重复 4 次」——只该落一条。
    """
    row = a_widened_pool(repository, session_factory)

    for _ in range(4):
        scheduler._military_assignments(row)  # noqa: SLF001 - 钉的就是这条真路

    assert len(recorded.pool) == 1
    assert len(recorded.widened) == 1


# -- 旋钮 ----------------------------------------------------------------------


def test_the_window_is_the_same_configurable_knob(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """窗口复用 `military_attack_config.auto_toggle_log_seconds` 这一个旋钮。

    不新开一个：两边的取舍**完全同向**——想把排障看密的人两边都想密，嫌库吵的人
    两边都嫌吵。旋钮多一个就多一个要解释、要配、要配错的地方。

    填 0 = 不限流，也就是加这道闸之前的行为（排障时想看清真实频率就填 0）。
    这一条同时守住「0 不是假值」：写成 `seconds or DEFAULT` 的实现会让它转红。
    """
    row = a_healthy_pool(repository, session_factory)
    repository.replace_military_attack_tiers(
        '[{"min_score": 0, "preset": "AAA"}]', auto_toggle_log_seconds=0
    )

    for index in range(4):
        clock.now = NOW + timedelta(seconds=index)
        log_once(scheduler, row)

    assert len(recorded.pool) == 4, "填 0 就该每次都记，这是排障时唯一看得清频率的挡位"
