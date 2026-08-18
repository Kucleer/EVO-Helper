"""「因空闲航线不足停用」的任务，等航线空出来就自己回来。

判据本身简单，这里守的是**语义边界**——哪一类停用会自愈、哪一类必须要用户
动手。边界破了不会报错：要么任务永远挂着「已停用」一发不派（这次修的就是它），
要么反过来，一条连崩三次的链路被悄悄放出来，退回那个满速空转的重启循环。

四条硬约束，每条各有一段：

1. **航线仍然满着 → 不恢复。** 恢复的条件是「此刻真的有空闲航线」，不是
   「过了一会儿再试试」。
2. **航线空出来了 → 自动恢复，并写一条 `system_log`。** 任务突然又开始跑而
   日志里一个字都没有，事后没人查得出是谁放的它。
3. **连续失败停用的 → 航线空着也不恢复。** 这条最重要。
4. **用户手动停用（复选框没勾）的 → 不被自动恢复。**

分类判据是 `mission_tasks.disabled_recovery` 这个**结构化标记**，不是
`disabled_reason` 里那句中文——所以这里的断言一律钉标记与行为，不钉措辞。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.mission_scheduler import MAX_CONSECUTIVE_FAILURES, MissionScheduler
from evo_helper.domain.missions import NoFreeLineError, bot_command
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import MISSION_KIND_SCOUT, TARGET_KIND_BOT, TARGET_KIND_PIRATE
from evo_helper.domain.scheduler import GAP_FILLERS, DisabledRecovery, MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import (
    BOT_RANGE,
    add_bot_target,
    dispatch,
    set_config,
    task_id,
)

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
#: 种子任务与测试里的派遣共用的出发星球（`domain.missions.ORIGIN`）。
ORIGIN = Coordinate(2, 137, 18)
#: 打谁不重要，只要是个已记录的 bot，好让 bot 任务有活干。
#: ⚠️ 位次必须避开 1–4：那四个位次是游戏固定生成的海盗，`is_bot_coordinate`
#: 会把它们整个剔掉，于是这个任务一个目标都没有、被判成「已完成」而不是「等航线」。
TARGET = Coordinate(2, 150, 8)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


def row_of(repository: SqlAlchemyRepository, kind: MissionKind) -> orm.MissionTaskRow:
    return next(row for row in repository.mission_tasks() if row.kind == kind.value)


def only_bot(repository: SqlAlchemyRepository) -> None:
    """只留 bot 攻击一条链路参与调度。

    填空隙的那几种（扫描 / 军力榜）尤其要关：它们永远有活干，留着的话
    「起了谁」这类断言会先看到它们，而它们与航线一点关系都没有。
    """
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == MissionKind.BOT.value)
    repository.update_mission_task(task_id(repository, MissionKind.BOT), params_json=BOT_RANGE)


def occupy_the_only_line(repository, run_id, *, flight: timedelta):  # type: ignore[no-untyped-def]
    """派一发还没回来的侦察，把那颗星球上唯一一条航线占住。

    **用侦察而不是攻击**：侦察不产生战报，也就不会顺带改变 bot 那条链路的
    完成判据。它一样占航线（这正是这里要的），只是不进配额、不进战报。
    """
    return dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        target=Coordinate(2, 137, 1),
        dispatched_at=NOW - timedelta(minutes=10),
        preset_name="侦察",
        mission_kind=MISSION_KIND_SCOUT,
        flight=flight,
    )


class OneShotLineShortage:
    """第一次组 bot 命令行时抛 `NoFreeLineError`，之后一切照旧。

    ⚠️ **为什么要有它。** 2026-08-19 之前这一档是「跑」出来的：航线满着、
    但还有战报要收，`has_work` 的右半边（`or reports_due`）把任务放行，
    `bot_command` 再因 `max_dispatches < 1` 抛 `NoFreeLineError`。那条路已经
    堵上了（见 `domain.scheduler.has_work`）——bot 链路航线满时压根不再起轮，
    因为它那两条命令行**都**兑现不了「只收战报不派遣」。

    **不改成直接写一行 `disabled_recovery='FREE_LINES'`**：那样 `_launch` 里
    「按异常类型认类别」那一段就没人守了，它一旦退化成 `MANUAL`，任务照样
    永远起不来，而所有用例仍然全绿。所以这里换的只是**触发方式**——异常从哪
    抛出来的——`_launch` 那一段仍然逐字被执行到。

    只抛一次：恢复之后那几条用例要看着它真的重新开始派遣。
    """

    def __init__(self) -> None:
        self.fired = False

    def __call__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not self.fired:
            self.fired = True
            raise NoFreeLineError("空闲航线不足，暂不启动 bot 攻击")
        return bot_command(*args, **kwargs)


def disable_for_lack_of_lines(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, run_id, session_factory, clock, monkeypatch
) -> orm.MissionTaskRow:
    """把 bot 任务真的**跑**成「因空闲航线不足停用」，不是直接写库。

    两步，次序不能颠倒：

    1. **航线空着**的那一 tick 把任务放行，`bot_command` 抛 `NoFreeLineError`
       （`OneShotLineShortage`）→ `_launch` 把它停用成 `FREE_LINES`。
    2. **停用之后**再派一发在飞的侦察，把那颗星球上唯一一条航线占住。下面那
       几条用例要的「航线还满着 39 分钟、第 40 分钟空出来」由它来摆。

    ⚠️ 顺序颠倒（先占航线再 tick）就回到 2026-08-19 之前那条已经堵上的路：
    `has_work` 现在会说「没活干」，任务根本不会被起，夹具那句断言当场转红。

    **这一整套为什么还留着**（触发路径已经没了）：`mission_tasks` 那两列是
    持久化的，生产库里可能还有旧版本留下的 `FREE_LINES` 行；用户重启 bat 之后
    跑的是新代码，认不得它就永远挂着「已停用」。而「哪一类停用会自愈」是语义
    边界，边界破了不报错（见模块头）。
    """
    set_config(session_factory, fleet_line_limit=1, reserved_lines=0)
    only_bot(repository)
    add_bot_target(session_factory, TARGET)
    # 两小时前打出去、飞行时间没读到、战报还没到：`ReportWaitPlanner` 判「该去
    # 收」（两小时仍在 `MAX_REPORT_AGE` 以内），而这一发早已过了
    # `UNKNOWN_LINE_HOLD`（90 分钟），所以它自己不再占航线——占航线的只有下面
    # 那发侦察，好让「空闲航线」这个变量只由它一个人决定。
    #
    # 留着它是为了把生产那个现场原样摆出来：**航线满着，而且还欠着战报**。
    # 2026-08-19 起这一档就是「什么都不做」，而不是「起一轮去收」。
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=TARGET,
        dispatched_at=NOW - timedelta(hours=2),
    )
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.bot_command",
        OneShotLineShortage(),
        raising=True,
    )
    scheduler.start()
    scheduler.tick()
    occupy_the_only_line(repository, run_id, flight=timedelta(minutes=25))

    row = row_of(repository, MissionKind.BOT)
    assert row.disabled_reason is not None, "夹具没能把任务跑成「因航线不足停用」"
    assert row.disabled_recovery == DisabledRecovery.FREE_LINES.value
    assert launcher.spawned == []
    return row


class RecordingLog:
    """把 `record_system_log` 的调用记下来。签名与真的那一个一致。"""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.payloads: list[dict[str, object]] = []

    def __call__(self, level, source, message, *, payload=None, logged_at_utc=None, **_):  # type: ignore[no-untyped-def]
        self.messages.append(message)
        self.payloads.append(dict(payload or {}))


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


# -- 航线仍然满着：不恢复 ------------------------------------------------------


def test_a_task_disabled_for_lack_of_lines_stays_disabled_while_the_lines_are_full(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    launcher,
    clock,
    run_id,
    session_factory,
    recorded: RecordingLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """条件还成立着就别动它。

    那发侦察要 50 分钟才回来，所以整整 39 分钟里航线一直是满的。

    ⚠️ **时钟是一格一格往前挪的，每格都 tick。** 一次跳到底的话，任何一种
    「停用之后等 N 分钟再试试」的实现都可以因为「第一次看见它就是最后一刻」
    而蒙混过关——分五分钟一格走完 39 分钟，那种实现必然在中途放它出来。

    ⚠️ **日志那条断言才是真正有牙的那半。** 一个「等 N 分钟就放出来」的实现放它
    出来之后，同一个 tick 里 `_step` 会立刻拿同样的事实把它再停用一次，于是
    库里那两列看起来一直没变过——churn 只有在日志里留得下痕迹。恢复一次写一条、
    停用一次也写一条（`_disable_task`），所以 churn 会在这里留下**两条**，
    而正确的实现一条都不留。

    夹具那一下停用本身是一次真的跃迁，它写的那一条要先清掉：这里数的是
    「停用之后又发生了什么」，不是「一共写过几条」。
    """
    disable_for_lack_of_lines(
        scheduler, repository, launcher, run_id, session_factory, clock, monkeypatch
    )
    recorded.messages.clear()
    recorded.payloads.clear()

    for minutes in range(0, 40, 5):
        clock.now = NOW + timedelta(minutes=minutes)
        scheduler.tick()

        row = row_of(repository, MissionKind.BOT)
        assert row.disabled_reason is not None, f"第 {minutes} 分钟就被放出来了，而航线还满着"
        assert row.disabled_recovery == DisabledRecovery.FREE_LINES.value
        assert recorded.messages == [], f"第 {minutes} 分钟放出来又立刻停用了一次"
    assert launcher.spawned == []


# -- 航线空出来了：自动恢复 ----------------------------------------------------


def test_the_task_comes_back_by_itself_once_a_line_frees_up(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    launcher,
    clock,
    run_id,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """舰队一回来就该自己回来，不必用户点「恢复」。

    这一条是整次改动的正面用例。判定每 tick **现算**：时钟一越过那条航线空出来
    的时刻，下一个 tick 就恢复——中间没有任何人被通知过，也没有谁在等一个闹钟，
    所以调度器进程重启之后照样成立。
    """
    disable_for_lack_of_lines(
        scheduler, repository, launcher, run_id, session_factory, clock, monkeypatch
    )

    # 出发前 10 分钟派出，飞 25 分钟、往返 50 分钟 → 航线在 NOW + 40 分钟空出来。
    clock.now = NOW + timedelta(minutes=41)
    scheduler.tick()

    row = row_of(repository, MissionKind.BOT)
    assert row.disabled_reason is None
    assert row.disabled_recovery is None


def test_the_recovered_task_actually_gets_scheduled_again(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    launcher,
    clock,
    run_id,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清掉两列还不够，它得真的重新开始派遣。

    只清库不参与调度，页面上会从「已停用」变成「待命」然后一直待命——比一直
    显示「已停用」更难查。
    """
    disable_for_lack_of_lines(
        scheduler, repository, launcher, run_id, session_factory, clock, monkeypatch
    )

    clock.now = NOW + timedelta(minutes=41)
    for _ in range(3):
        scheduler.tick()

    assert launcher.kinds == [MissionKind.BOT]


def test_the_recovery_is_written_into_the_system_log(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    launcher,
    clock,
    run_id,
    session_factory,
    recorded: RecordingLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复必须留下一条，而且要说清「当前空闲航线几条」。

    ⚠️ 这条钉的是**日志本身存在**。删掉那一句 `record_system_log`，上面几条
    行为用例仍然全绿——任务照样自己回来了——但日志里一个字都没有，而
    「任务不动/突然又动而日志不说原因」今晚（2026-08-17）已经栽过一次。

    只留**一条**：tick 每秒一次，恢复那一下如果每 tick 都刷，真正要看的那一条
    会被淹掉。恢复之后标记就清了，所以这里连 tick 五次再数条数。
    """
    disable_for_lack_of_lines(
        scheduler, repository, launcher, run_id, session_factory, clock, monkeypatch
    )
    recorded.messages.clear()
    recorded.payloads.clear()

    clock.now = NOW + timedelta(minutes=41)
    for _ in range(5):
        scheduler.tick()

    assert len(recorded.messages) == 1
    assert "空闲航线" in recorded.messages[0]
    assert "自动恢复" in recorded.messages[0]
    assert recorded.payloads[0]["task_id"] == task_id(repository, MissionKind.BOT)
    assert recorded.payloads[0]["disabled_recovery"] == DisabledRecovery.FREE_LINES.value
    free_lines = recorded.payloads[0]["free_lines"]
    assert isinstance(free_lines, int) and free_lines >= 1


# -- 别的停用原因：一律不动 ----------------------------------------------------


def test_a_chain_disabled_by_consecutive_failures_is_never_resumed_automatically(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory
) -> None:
    """**这条最重要。** 连崩到上限说的是「这不是暂时的」。

    航线一条都没被占（空闲航线管够），所以「有空闲航线」这个条件成立得不能再
    成立——任何把恢复判据放宽到「所有自动停用都自愈」的改法，都会让这条转红。
    自动放它出来的后果是调度循环退回那个满速空转的重启循环。
    """
    set_config(session_factory, fleet_line_limit=6, reserved_lines=0)
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == MissionKind.PIRATE.value)
    scheduler.start()

    for index in range(MAX_CONSECUTIVE_FAILURES):
        scheduler.tick()
        launcher.latest.exit_code = 1
        clock.now = NOW + timedelta(minutes=6 * (index + 1))
        scheduler.tick()

    disabled = row_of(repository, MissionKind.PIRATE)
    assert disabled.disabled_reason is not None
    assert disabled.disabled_recovery == DisabledRecovery.MANUAL.value

    started = len(launcher.spawned)
    clock.now = NOW + timedelta(hours=4)
    for _ in range(5):
        scheduler.tick()

    still = row_of(repository, MissionKind.PIRATE)
    assert still.disabled_reason is not None
    assert still.disabled_recovery == DisabledRecovery.MANUAL.value
    assert len(launcher.spawned) == started


def test_a_config_error_keeps_needing_a_human_even_with_lines_to_spare(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory
) -> None:
    """「恒星系区间首尾颠倒」这类参数不合格同样不自愈。

    航线空着也没用：改配置之前重试一万次都是同一个结果。这一条守的是
    `_launch` 里那个 `isinstance` 分岔——把它去掉、让所有 `MissionParamError`
    都记成会自愈的话，这个任务会每 tick 被放出来再停用一次。
    """
    set_config(session_factory, fleet_line_limit=6, reserved_lines=0)
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == MissionKind.BOT.value)
    repository.update_mission_task(
        task_id(repository, MissionKind.BOT),
        params_json='{"galaxy": 2, "first_system": 200, "last_system": 100}',
    )
    scheduler.start()
    scheduler.tick()

    disabled = row_of(repository, MissionKind.BOT)
    assert disabled.disabled_reason is not None
    assert disabled.disabled_recovery == DisabledRecovery.MANUAL.value

    clock.now = NOW + timedelta(hours=1)
    for _ in range(5):
        scheduler.tick()

    still = row_of(repository, MissionKind.BOT)
    assert still.disabled_reason is not None
    assert still.disabled_recovery == DisabledRecovery.MANUAL.value
    assert launcher.spawned == []


def test_a_task_the_user_turned_off_is_not_switched_back_on(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    launcher,
    clock,
    run_id,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户手动停用的语义一个字都不变。

    `enabled` 是用户的意志。恢复那一路只清 `disabled_reason`/`disabled_recovery`
    这两列（调度器自己的状态），碰 `enabled` 就等于「我自己勾掉的被悄悄打开了」。
    """
    disable_for_lack_of_lines(
        scheduler, repository, launcher, run_id, session_factory, clock, monkeypatch
    )
    # 用户在停用状态上又手动把复选框勾掉了。⚠️ 这一下走 `update_mission_task`，
    # 它会顺带清掉停用标记（改配置 = 给它一次重新开始的机会），所以下面钉的是
    # `enabled` 这一列本身。
    repository.update_mission_task(task_id(repository, MissionKind.BOT), enabled=False)

    clock.now = NOW + timedelta(minutes=41)
    for _ in range(5):
        scheduler.tick()

    assert row_of(repository, MissionKind.BOT).enabled is False
    assert launcher.spawned == []


def test_gap_fillers_are_never_touched_by_the_recovery_pass(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory
) -> None:
    """填空隙的那几种从来不因航线停用，恢复那一路也就一个字都不该改它们。

    它们不派遣、不受航线约束；被这一路碰到只可能是判据认错了人。
    """
    set_config(session_factory, fleet_line_limit=6, reserved_lines=0)
    for kind in sorted(GAP_FILLERS, key=lambda item: item.value):
        repository.disable_mission_task(
            task_id(repository, kind), "手动停用", recovery=DisabledRecovery.MANUAL
        )
    scheduler.start()

    clock.now = NOW + timedelta(hours=1)
    for _ in range(5):
        scheduler.tick()

    for kind in GAP_FILLERS:
        assert row_of(repository, kind).disabled_reason == "手动停用"
