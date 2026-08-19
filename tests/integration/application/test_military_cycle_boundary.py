"""周一 UTC+0 刷新之后，上周期的军力读数一律当作「没有读数」——接线那一半。

判据本身钉在 `tests/unit/domain/test_target_order.py`（「第 2 步的另一半」那一节）。
这里守的是它接出去的三条线，每一条错了都不会报错、只会静悄悄地说反话：

1. **页面**：`targets_remaining` 归零时不许显示成「已完成」——那是一句听起来顺利、
   实际一个都打不了的话。这一档要落到 `scores_are_missing` 上
   （`TaskStatus.MISSING_MILITARY_SCORES`），而**不是**落到「已放宽窗口」上。
2. **调度**：BOT 说「没活干」之后，填空隙的军力榜要自然拿到时间片，把这周的读数扫出来。
   「先扫再打」是现成的行为，不是另写的一条逻辑。
3. **日志**：判据把整池挡光的那一刻要留痕，说清「本周期起点是什么时候、被筛掉多少条、
   剩多少」；而且**必须限流**——这条路每 tick 都走，PR #188 修过一次同类问题，
   当时两条日志占了 `system_log` 全表的 44%。

用户口径（2026-08-19）：「周一刷新那一刻，全部 bot 的军力读数同时作废」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.mission_scheduler import (
    REPEATED_LOG_WINDOW,
    STALE_POOL_WARNING_AFTER,
    MissionScheduler,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind, TaskStatus, status_of

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import (
    BOT_BY_MILITARY_2H,
    add_bot_target,
    enable,
    only_gap_filler,
    task_id,
)

#: **周一 00:30 UTC**：刷新刚过去半小时，军力榜这周还一个字都没写。
NOW = datetime(2026, 8, 17, 0, 30, tzinfo=UTC)
#: 本周期的起点。写死而不是调 `cycle_start_utc(NOW)`——用例里再调一次被测的那个
#: 函数，等于让实现给自己判卷。
CYCLE_START = datetime(2026, 8, 17, tzinfo=UTC)
#: 上周日晚上读到的：**只有 31 分钟大，2 小时的有效期窗口一点都拦不住它**。
#: 拦住它的只能是周期边界，所以这一组的每一条都用这个时刻。
LAST_NIGHT = NOW - timedelta(minutes=31)

#: 日志里那条 WARNING / INFO 的开头。
CYCLE_LINE = "军力读数跨周期作废"


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

    def __call__(self, level, source, message, *, payload=None, logged_at_utc=None, **_):  # type: ignore[no-untyped-def]
        self.entries.append((level, message, dict(payload or {})))

    def of(self, prefix: str) -> list[tuple[str, str, dict[str, object]]]:
        return [item for item in self.entries if item[1].startswith(prefix)]

    @property
    def cycle(self) -> list[tuple[str, str, dict[str, object]]]:
        return self.of(CYCLE_LINE)

    @property
    def pool(self) -> list[tuple[str, str, dict[str, object]]]:
        return self.of("军力候选池")

    def warnings(self) -> list[tuple[str, str, dict[str, object]]]:
        return [item for item in self.entries if item[0] == "WARNING"]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


def a_pool_left_over_from_last_week(repository, session_factory) -> int:  # type: ignore[no-untyped-def]
    """两颗 bot，读数都是上周日晚上的——**本周期一条读数都没有**。返回任务 id。"""
    for index, coordinate in enumerate((Coordinate(2, 140, 5), Coordinate(2, 141, 6))):
        add_bot_target(
            session_factory, coordinate, military_score=9_000.0 - index, scanned_at=LAST_NIGHT
        )
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    return task_id(repository, MissionKind.BOT)


# -- 页面：不许说成「已完成」，也不许说成「已放宽窗口」 -------------------------


def test_last_weeks_readings_are_not_dressed_up_as_a_finished_round(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **周一凌晨读数全废时，页面要说「军力数据未采集」，不是「已完成」。**

    两者在 `targets_remaining` 上长得一模一样（都是 0），而「已完成」听起来是这一轮
    顺利跑完了——用户于是去点「重开一轮」，重开之后池子里还是那批上周的读数。

    ⚠️ **也不许说成「军力读数已放宽窗口」。** 那句话同样听起来正常（任务照跑），
    而实际一发都派不出去。它之所以不会出现，正是因为判据待在第 2 步：上周期的
    读数进不了 `with_readings`，放宽也就捞不回来（`domain.target_order` 模块头）。
    """
    bot = a_pool_left_over_from_last_week(repository, session_factory)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    snapshot = scheduler.snapshot()
    facts = snapshot.facts.per_task[bot]
    assert launcher.spawned == [], "拿上周的读数派出去了"
    assert facts.targets_remaining == 0
    assert facts.scores_are_missing, "只剩一个 0，页面就只能把它读成「已完成」"
    assert not facts.scores_window_widened, "上周期的读数被放宽窗口捞了回来"

    task = next(item for item in snapshot.snapshots if item.task_id == bot)
    assert status_of(task, snapshot.facts, running=None) is TaskStatus.MISSING_MILITARY_SCORES


def test_a_reading_taken_after_the_refresh_puts_the_round_back_to_work(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """反向那一半：军力榜扫出本周期的读数之后，这一轮立刻恢复正常。

    少了它，一个「军力优先永远不派」的实现也能让上面那条全绿——而那是把
    2026-08-17 那晚的停摆换了个成因原样搬回来。
    """
    bot = a_pool_left_over_from_last_week(repository, session_factory)
    add_bot_target(session_factory, Coordinate(2, 142, 7), military_score=7_000.0, scanned_at=NOW)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    facts = scheduler.snapshot().facts.per_task[bot]
    assert facts.targets_remaining == 1, "本周期读到的那一个要算数"
    assert not facts.scores_are_missing
    assert launcher.kinds == [MissionKind.BOT]
    assert "2:142:7" in " ".join(launcher.latest.command)
    assert "2:140:5" not in " ".join(launcher.latest.command), "上周的读数混进了这一轮"


# -- 调度：让位给军力榜是现成的行为，不是另写的一条 ------------------------------


def test_a_pool_of_last_weeks_readings_lets_the_ranking_scan_take_the_mouse(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **「先扫再打」不需要另写一条逻辑，它是现成链路的自然结果。**

    第 2 步把上周期的读数筛掉 → `MilitaryPoolReading.usable` 归零 →
    `bot_round_complete` 为真 → `has_work` 里 BOT 那支返回 False → 填空隙的军力榜
    （`has_work` 恒为真）拿到时间片。这条用例把那整条链路量了一遍，
    省得下一个人再照着「应该会让位吧」去加一条重复的判据。

    ⚠️ **刷新仍然只能由调度器发起**：攻击链路自己去起 RANKING 的话，两条链路会争
    同一只鼠标。
    """
    a_pool_left_over_from_last_week(repository, session_factory)
    only_gap_filler(repository, MissionKind.RANKING)
    scheduler.start()
    scheduler.tick()

    assert launcher.kinds == [MissionKind.RANKING]


# -- 日志：整池被挡光的那一刻要留痕，而且必须限流 --------------------------------


def test_the_moment_the_boundary_empties_the_pool_is_written_down(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **三个数一个都不能少：本周期起点、被筛掉多少条、还剩多少。**

    少任何一个，看见日志的人还是得回库里查——而那正是「没人告诉你」的另一种写法。

    ⚠️ **这条日志非有不可，上面那两条都盖不住它**：每一步余量那条
    （「军力候选池：…」）只在**组命令行**时才写，而整池被挡光时这条链路根本轮不到
    组命令行；「连续半小时筛不出目标」那条要憋满半小时才开口，而周一凌晨那半小时
    正是最该看清「刚才发生了什么」的半小时。
    """
    a_pool_left_over_from_last_week(repository, session_factory)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert len(recorded.cycle) == 1, "整池被挡光了，库里却一个字都没有"
    level, message, payload = recorded.cycle[0]
    assert level == "WARNING"
    assert "2026-08-17 00:00 UTC" in message, f"没说本周期起点是什么时候：{message}"
    assert payload["cycle_start_utc"] == CYCLE_START.isoformat()
    assert payload["dropped_last_cycle"] == 2, "被筛掉多少条没说出来"
    assert payload["with_readings"] == 0, "还剩多少没说出来"
    assert payload["attackable"] == 2


def test_the_boundary_line_is_throttled_like_every_other_per_tick_line(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, clock, recorded: RecordingLog
) -> None:
    """⚠️ **每 tick 都走的路上必须限流**：状态没变就一个窗口最多一条。

    这条判据住在 `_military_pool_reading` 里，而它每 tick 算一次（页面轮询也会）。
    不限流的话它就是下一个 PR #188——那次「军力候选池」与「放宽窗口」两条合起来
    占了 `system_log` 全表的 44%，其中 12,080 行一个新事实都没带来。

    时钟一格一格往前挪，走满整整一个窗口。**一次跳到底的话，任何「隔 N 秒才写」的
    实现都能蒙混过关**；分成多格走完，只写不压的实现必然在中途多落几条。
    """
    a_pool_left_over_from_last_week(repository, session_factory)
    only_gap_filler(repository)
    scheduler.start()

    for seconds in (0, 1, 1, 30, 80):
        clock.now += timedelta(seconds=seconds)
        scheduler.tick()

    assert len(recorded.cycle) == 1, f"一个窗口里只该落一条，落了 {len(recorded.cycle)} 条"

    # 越过窗口再走一轮：这一条要落库，「这个状态还在持续」本身就是要看的事实。
    clock.now = NOW + REPEATED_LOG_WINDOW + timedelta(seconds=1)
    scheduler.tick()

    assert len(recorded.cycle) == 2


def test_the_boundary_line_says_so_once_when_the_scan_catches_up(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, clock, recorded: RecordingLog
) -> None:
    """从「挡光」跌回「正常」时补一条 INFO 收口，**而且只补一条**。

    只报开头不报结尾的话，翻日志的人读不出这一段停了多久——而那正是判断「军力榜
    扫得够不够快」的那个数。收口只在跃迁那一下写，不吃窗口兜底：否则一个长期正常
    的任务会每 120 秒刷一句「已恢复」，那是把刷屏换了个句子。
    """
    a_pool_left_over_from_last_week(repository, session_factory)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()
    assert recorded.cycle[0][0] == "WARNING"

    clock.now = NOW + timedelta(minutes=5)
    add_bot_target(
        session_factory, Coordinate(2, 142, 7), military_score=7_000.0, scanned_at=clock.now
    )
    scheduler.tick()

    assert len(recorded.cycle) == 2
    level, message, payload = recorded.cycle[1]
    assert level == "INFO", "正常了还报 WARNING 就是狼来了"
    assert "已恢复" in message
    assert payload["stalled"] is False

    for minutes in (6, 10, 60):
        clock.now = NOW + timedelta(minutes=minutes)
        scheduler.tick()
    assert len(recorded.cycle) == 2, "「已恢复」变成了每窗口一条的刷屏"


def test_a_pool_that_never_made_the_board_is_not_blamed_on_the_boundary(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **成因必须说对：从没上过军力榜的那一档，不许说成「上周期的读数作废了」。**

    两者在 `usable == 0` 上长得一模一样，而善后完全不同——说错会把人引到
    「上周的数据怎么办」这条错路上，真相只是这些 bot 从来没被榜单扫到过。
    少了这条，一个「只要 `usable == 0` 就报跨周期作废」的实现会全绿。
    """
    add_bot_target(session_factory, Coordinate(2, 140, 5), military_score=None, scanned_at=None)
    enable(repository, MissionKind.BOT, params_json=BOT_BY_MILITARY_2H)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert recorded.cycle == [], "把「从没上过榜」说成了「上周期的读数作废」"


def test_the_pipeline_line_does_not_call_last_weeks_readings_never_rated(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **每一步余量那条日志里，两种「出局」必须各占一个数。**

    合成一个的话，周一凌晨这条会写着「N 个从未上榜」——**一句假话**，而且它会把人
    引到「军力榜为什么漏了这些 bot」这条错路上，真相只是该重扫一轮了。
    仓库的规矩是「日志说假话比不说更糟」。
    """
    a_pool_left_over_from_last_week(repository, session_factory)  # 2 个上周期的读数
    add_bot_target(session_factory, Coordinate(2, 143, 8), military_score=None, scanned_at=None)
    add_bot_target(session_factory, Coordinate(2, 142, 7), military_score=7_000.0, scanned_at=NOW)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    assert len(recorded.pool) == 1
    _, message, payload = recorded.pool[0]
    assert payload["dropped_unrated"] == 1, "上周期那两个被算成了「从未上榜」"
    assert payload["dropped_last_cycle"] == 2
    assert payload["with_readings"] == 1
    assert payload["cycle_start_utc"] == CYCLE_START.isoformat()
    assert "1 个从未上榜" in message, f"消息正文里那个数也得说对：{message}"


def test_the_half_hour_warning_names_the_boundary_as_the_cause(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, clock, recorded: RecordingLog
) -> None:
    """⚠️ 「连续半小时筛不出目标」那条也要说对成因。

    它原本只有一句「军力榜还没扫到它们」。周一凌晨那不是真话——榜单扫到过它们，
    只是那份读数上周就作废了。照着那句去查「军力榜为什么漏了这些 bot」，查一夜也
    查不出东西。
    """
    a_pool_left_over_from_last_week(repository, session_factory)
    only_gap_filler(repository)
    scheduler.start()
    scheduler.tick()

    clock.now = NOW + STALE_POOL_WARNING_AFTER
    scheduler.tick()

    stalled = [item for item in recorded.warnings() if item[1].startswith("「")]
    assert len(stalled) == 1
    _, message, payload = stalled[0]
    assert "军力榜还没扫到它们" not in message, f"成因说反了：{message}"
    assert "2026-08-17 00:00 UTC" in message
    assert payload["dropped_last_cycle"] == 2
