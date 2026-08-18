"""调度器**自己把任务关掉**的那一刻，要在 `system_log` 里留下痕迹。

## 为什么 `disabled_reason` 那一列不算留痕

它只留得住**当前**这一次。`resume_mission_task`（航线一空就自动恢复，PR #161）
与 `update_mission_task`（用户改一次配置）都会把它清成 NULL。于是「昨晚三点因为
范围里一个 bot 都没有被关掉、四点又被自动放回来」这段经过，事后在库里一个字都
不剩——而那正是要查的东西。日志是只增不改的，它才留得住。

恢复那一侧早就写了一条（`_resume_tasks_waiting_for_a_line`，理由是「任务突然
又开始跑而日志里一个字都没有，事后没人查得出是谁放的它」）。停用这一侧一直是
哑的，而它更要紧：**任务不动**比任务乱动难发现得多。

## 两条互相制衡的事

1. **跃迁那一下必须写**，而且要说清是哪一种停用（等航线自愈 / 要人工）。
2. **只在跃迁那一下写。** `_targets_remaining` 每 tick 都会走（页面轮询也会），
   无条件写就是每秒一条、一夜八万行；更糟的是事后按日志对时间会对出一个假的
   「停用时刻」——真正的那一刻埋在八万行的最前面。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.mission_scheduler import MissionScheduler, task_snapshot
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import DisabledRecovery, MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_line_shortage_recovery import (
    NOW,
    RecordingLog,
    disable_for_lack_of_lines,
    recorded,  # noqa: F401 - fixture，被下面的用例按名字取用
    row_of,
)
from .test_mission_scheduler import set_config, task_id

ORIGIN = Coordinate(2, 137, 18)

#: 首尾颠倒的系号区间：`bot_command` 会当场抛 `MissionParamError`，
#: 而那一类**不自愈**——改配置之前重试一万次都是同一个结果。
BROKEN_RANGE = '{"galaxy": 2, "first_system": 200, "last_system": 100}'


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


def only_bot_with_a_broken_range(  # type: ignore[no-untyped-def]
    repository: SqlAlchemyRepository, session_factory
) -> None:
    """只留 bot 一条链路，并把它的系号区间配成不合法的。

    航线管够（`fleet_line_limit=6`）：这样「为什么被停用」只可能是参数，
    不会和航线不足那一档混起来。
    """
    set_config(session_factory, fleet_line_limit=6, reserved_lines=0)
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == MissionKind.BOT.value)
    repository.update_mission_task(task_id(repository, MissionKind.BOT), params_json=BROKEN_RANGE)


def bot_row(repository: SqlAlchemyRepository) -> orm.MissionTaskRow:
    return row_of(repository, MissionKind.BOT)


# -- 跃迁那一下必须写 ----------------------------------------------------------


def test_a_config_error_that_disables_a_task_is_written_to_the_system_log(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    launcher,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ 这条钉的是**日志本身存在**。

    删掉 `_disable_task` 里那一句 `record_system_log`，行为用例仍然全绿——任务
    照样被停用了——但生产库里一个字都没有。而这个任务从此**一发都不派**，页面
    上只是一行「已停用」，没人知道是哪一秒、因为什么。
    """
    only_bot_with_a_broken_range(repository, session_factory)
    scheduler.start()
    scheduler.tick()

    assert bot_row(repository).disabled_reason is not None, "夹具没能把任务跑成「已停用」"
    assert len(recorded.messages) == 1
    assert "自动停用" in recorded.messages[0]
    assert launcher.spawned == []


def test_the_message_says_why_and_what_it_takes_to_come_back(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """光说「停用了」不够，还要说**为什么**和**接下来怎么办**。

    这两样对应的动作完全相反：参数填错要用户去改配置，等航线只需要等。日志把
    两者说成同一句，用户就只能去页面上翻——而页面只留当前那一次。
    """
    only_bot_with_a_broken_range(repository, session_factory)
    scheduler.start()
    scheduler.tick()

    message = recorded.messages[0]
    payload = recorded.payloads[0]
    # 原因原样带上：它是 `MissionParamError` 那句话本身，措辞改了这里跟着变，
    # 所以只钉「库里那一列和日志说的是同一句」。
    assert payload["disabled_reason"] == bot_row(repository).disabled_reason
    assert str(payload["disabled_reason"]) in message
    assert payload["disabled_recovery"] == DisabledRecovery.MANUAL.value
    assert payload["task_id"] == task_id(repository, MissionKind.BOT)
    assert payload["mission_kind"] == MissionKind.BOT.value
    assert "恢复" in message, "没说清要人工才能放它出来"


def test_the_line_shortage_flavour_says_it_will_come_back_by_itself(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    launcher,
    run_id,
    session_factory,
    clock,
    monkeypatch: pytest.MonkeyPatch,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """航线不足那一档要说「会自动恢复」，而且标记必须是 `FREE_LINES`。

    ⚠️ **措辞和标记必须同时对。** 只钉措辞的话，一个把标记写成 `MANUAL` 的实现
    照样绿——而那个任务会永远挂着，正是 PR #161 修的那个毛病；只钉标记的话，
    日志会对着一个自愈型停用说「要用户动手」，也就是**说了一句假话**。
    """
    disable_for_lack_of_lines(
        scheduler, repository, launcher, run_id, session_factory, clock, monkeypatch
    )

    assert len(recorded.messages) == 1
    assert "自动恢复" in recorded.messages[0]
    assert recorded.payloads[0]["disabled_recovery"] == DisabledRecovery.FREE_LINES.value
    assert bot_row(repository).disabled_recovery == DisabledRecovery.FREE_LINES.value


# -- 只在跃迁那一下写 ----------------------------------------------------------


def test_disabling_twice_with_the_same_verdict_only_says_it_once(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ **这一条直接钉 `_disable_task` 的限流，因为整条 tick 路径钉不住它。**

    今天没有哪一条路会拿同一个判词把同一个任务连停两次：停用之后
    `_participating` 就是 False，`_facts` / `decide` 都不再碰它。也就是说下面那两
    条端到端用例现在是**结构上**成立的，删掉限流它们照样绿——所以限流本身必须
    在这里单独钉一次，否则它就是一段没人守的代码。

    而它守的东西是真的：`_targets_remaining` 位于**每 tick 都走**的那条路上
    （页面轮询也会走），只要哪天 `_participating` 那道闸口松一点，「无条件写」
    就变成每秒一条、一夜八万行——真正要看的那一条被埋在最前面，事后按日志对
    时间还会对出一个假的停用时刻。

    判据取的是**库里此刻那两列**而不是内存记忆，所以这里第二次调用走的是和
    「控制台重启后又看到同一个已停用任务」完全相同的那条判断。
    """
    only_bot_with_a_broken_range(repository, session_factory)
    scheduler.start()
    scheduler.tick()
    assert len(recorded.messages) == 1

    row = bot_row(repository)
    snapshot = task_snapshot(row, origin=ORIGIN, fleet_lines=6)
    for _ in range(3):
        scheduler._disable_task(  # noqa: SLF001 - 这条用例的对象就是它自己
            bot_row(repository),
            snapshot,
            str(row.disabled_reason),
            recovery=DisabledRecovery.MANUAL,
        )

    assert len(recorded.messages) == 1, "同一个判词重复停用，却不止写了一条"


def test_a_task_that_stays_disabled_does_not_write_a_line_every_tick(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    clock,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """一直停用着的任务，整段时间里日志只有最初那一条。

    这是上面那条限流的**端到端**说法。今天它靠的是 `_participating`（停用的任务
    不再参与 `_facts` 与 `decide`），而不是限流本身——所以它钉的是那道闸口：
    哪天有人让 `_facts` 也去处理已停用的任务，这条就会红。

    连 tick 二十次、时钟往前走一小时：任何一种「隔一会儿再报一次」的实现都会
    在这段里露头。
    """
    only_bot_with_a_broken_range(repository, session_factory)
    scheduler.start()
    scheduler.tick()
    assert len(recorded.messages) == 1

    for minute in range(1, 21):
        clock.now = NOW + timedelta(minutes=3 * minute)
        scheduler.tick()

    assert len(recorded.messages) == 1, "任务一直停用着，却不止写了一条"


def test_a_second_disable_after_the_user_reset_it_is_written_again(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    clock,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """限流不许压掉**真的第二次**跃迁。

    用户改一次配置就等于给这条链路一次重新开始的机会（`update_mission_task` 会
    把停用标记清掉）；它接着又被同样的理由关掉，那是一件新事，日志里必须有。
    压掉它的话，「我明明改过配置了」和「改完还是不行」在库里长得一模一样。

    ⚠️ 判据取的是**库里此刻那两列**而不是内存里的记忆，正是为了这一条：内存记忆
    在这里仍然记着「已经报过了」。
    """
    only_bot_with_a_broken_range(repository, session_factory)
    scheduler.start()
    scheduler.tick()
    assert len(recorded.messages) == 1

    # 用户在页面上改了一次配置（改回来的还是那个坏区间）——停用标记被清掉。
    repository.update_mission_task(task_id(repository, MissionKind.BOT), params_json=BROKEN_RANGE)
    assert bot_row(repository).disabled_reason is None
    clock.now = NOW + timedelta(minutes=10)
    scheduler.tick()

    assert len(recorded.messages) == 2


def test_restarting_the_console_does_not_re_announce_an_old_disable(  # type: ignore[no-untyped-def]
    repository,
    launcher,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """进程重启之后再看到同一个已停用的任务，那不是新的跃迁。

    再记一条的话，库里会出现一个**根本没发生过**的停用时刻——而控制台重启是
    家常便饭（改配置、装新版本），假时刻会一次次攒下去。

    今天挡住它的是 `_participating`（已停用的任务不再参与调度），限流那道判据
    是第二重保险，单独钉在上面那条用例里。
    """
    first_clock = Clock(NOW)
    first = MissionScheduler(repository, make_supervisor(launcher, first_clock), clock=first_clock)
    first.prepare()
    only_bot_with_a_broken_range(repository, session_factory)
    first.start()
    first.tick()
    assert len(recorded.messages) == 1

    # 新进程、新调度器对象：内存里那份记忆一个字都没有。
    later = Clock(NOW + timedelta(hours=2))
    second = MissionScheduler(repository, make_supervisor(launcher, later), clock=later)
    second.prepare()
    second.start()
    for _ in range(5):
        second.tick()

    assert len(recorded.messages) == 1
    assert bot_row(repository).disabled_reason is not None


# -- 措辞不许说自己没做过的事 --------------------------------------------------


def test_the_log_and_the_row_never_disagree(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """写下这一条的时候，库里那一行**已经**是停用状态了。

    ⚠️ 顺序反过来（先记日志再写库）就会出现「日志说停用了、库里还开着」的一秒，
    而写库那一步万一抛异常，那一秒会变成永久——日志说了一件没发生的事，而这个
    仓库今天已经为「日志说假话」付过两天的代价。
    """
    seen: list[tuple[str | None, str | None]] = []

    def spy(level, source, message, *, payload=None, logged_at_utc=None, **_):  # type: ignore[no-untyped-def]
        row = bot_row(repository)
        seen.append((row.disabled_reason, row.disabled_recovery))
        recorded(level, source, message, payload=payload, logged_at_utc=logged_at_utc)

    only_bot_with_a_broken_range(repository, session_factory)
    scheduler.start()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("evo_helper.application.mission_scheduler.record_system_log", spy)
        scheduler.tick()

    assert len(seen) == 1
    assert seen[0] == (
        recorded.payloads[0]["disabled_reason"],
        recorded.payloads[0]["disabled_recovery"],
    )


def test_the_logged_moment_comes_from_the_scheduler_clock(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    clock,
    session_factory,  # noqa: F811
) -> None:
    """时刻走调度器自己的钟，不是 `datetime.now()`。

    判据与写库同源是这个仓库的一条成例：两个钟差一点，事后按日志排时间线就会
    把停用排在触发它的那件事之前。
    """
    moments: list[datetime | None] = []

    def spy(level, source, message, *, payload=None, logged_at_utc=None, **_):  # type: ignore[no-untyped-def]
        moments.append(logged_at_utc)

    only_bot_with_a_broken_range(repository, session_factory)
    clock.now = datetime(2026, 8, 17, 3, 21, tzinfo=UTC)
    scheduler.start()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("evo_helper.application.mission_scheduler.record_system_log", spy)
        scheduler.tick()

    assert moments == [clock.now]
