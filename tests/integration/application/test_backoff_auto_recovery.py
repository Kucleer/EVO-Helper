"""连崩到上限被自动停用的任务，**冷却之后自己回来**。

## 在修什么（2026-08-28 生产实况）

00:01 游戏窗口没了，六个任务（1 个 RANKING + 5 个 BOT）每轮起来约 1 秒就死、
`exit=1`。调度器先按「多个任务一起倒」判为环境故障、连着免记 6 次（≈半小时），
01:02:49 豁免用尽，连续失败计满 `MAX_CONSECUTIVE_FAILURES=3`，六个任务全部自动
停用、恢复方式 `MANUAL`——**然后就没有然后了**。它们一直关到早上用户手动打开
（bot 约 07:2x、军力榜 09:32），而环境其实早就自己好了；军力榜多关了两个多小时
才被发现。用户口径：「我的预期是这些任务都应该自动重启才对」。

原先给 `MANUAL` 的理由（「自动恢复会让调度循环退回那个满速空转的重启循环」）
只对了一半：**防空转靠的是冷却，不是永不恢复。**

## 这份用例守的边界

判据是 `mission_tasks.disabled_recovery` 这个**结构化标记**加上 `retry_after_utc`
那个**落库的时刻**，不是 `disabled_reason` 里那句中文，也不是内存里的闹钟。
边界破了不会报错，只会长成两种样子之一：任务永远挂着「已停用」一发不派
（这次修的就是它），或者反过来——**用户自己关掉的任务被悄悄打开**。

⚠️⚠️ 后者是这次唯一不能出的错。生产上「侦查+攻击海盗」「扫描全星系 bot」
「5 系攻击」「9 系攻击」四个任务是用户手动关的。判据本身在
`domain.scheduler.due_for_a_backoff_retry`，那一层还有一份更贴身的用例
（`tests/unit/domain/test_backoff_recovery.py`）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.mission_scheduler import MAX_CONSECUTIVE_FAILURES, MissionScheduler
from evo_helper.domain.scheduler import RESTART_COOLDOWN, DisabledRecovery, MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_line_shortage_recovery import RecordingLog
from .test_mission_scheduler import disable, enable, only_gap_filler, task_id

NOW = datetime(2026, 8, 28, 0, 1, tzinfo=UTC)
"""窗口没掉的那一刻，逐字取自生产。"""

#: 夹具跑完之后自动停用发生的时刻：三轮崩溃，每轮间隔 `RESTART_COOLDOWN + 1 分钟`，
#: 收到第三个退出码那一 tick 就是第三轮的起点（见 `crash_until_disabled`）。
DISABLED_AT = NOW + (RESTART_COOLDOWN + timedelta(minutes=1)) * (MAX_CONSECUTIVE_FAILURES - 1)

#: 第一轮退避 15 分钟。
FIRST_RETRY_AT = DISABLED_AT + timedelta(minutes=15)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


def row_of(repository: SqlAlchemyRepository, kind: MissionKind) -> orm.MissionTaskRow:
    return next(row for row in repository.mission_tasks() if row.kind == kind.value)


def backoff_lines(recorded: RecordingLog) -> list[tuple[str, dict[str, object]]]:
    """只挑退避这条链路写的行。

    筛的是 payload 里那个**版本指纹键**（`backoff_scheme`），不是消息正文里的
    某个词：正文措辞随时会改，而指纹键的存在本身就是这次改动的留痕——顺带也把
    「每条退避日志都必须带指纹」钉住了。
    """
    return [
        (message, payload)
        for message, payload in zip(recorded.messages, recorded.payloads, strict=True)
        if "backoff_scheme" in payload
    ]


def crash_a_round(scheduler, launcher, clock, *, at: datetime) -> None:  # type: ignore[no-untyped-def]
    """在 `at` 起一轮，当场把它崩掉。

    ⚠️ **断言「这一轮真的起来了」**：重启冷却（`RESTART_COOLDOWN`）挡住的那种
    情形里 `launcher.latest` 拿到的是**上一轮那个已经结束的进程**，往它身上写
    `exit_code` 一点效果都没有——整段循环静默空转，而用例会以一个看不出原因的
    数字对不上收场。
    """
    before = len(launcher.spawned)
    clock.now = at
    scheduler.tick()
    assert len(launcher.spawned) == before + 1, f"{at} 这一轮没起来（多半是重启冷却挡着）"
    launcher.latest.exit_code = 1
    scheduler.tick()


def crash_until_disabled(scheduler, repository, launcher, clock) -> orm.MissionTaskRow:  # type: ignore[no-untyped-def]
    """把海盗链路真的**跑**成「连崩到上限而自动停用」，不是直接写库。

    直接写一行 `disabled_recovery='BACKOFF'` 的话，`record_mission_failure` 里
    「停用时定下重试时刻」那一段就没人守了：它一旦退化回 `MANUAL`，任务照样永远
    起不来，而下面每一条用例仍然全绿——正是 2026-08-28 那一夜的样子。

    只留海盗一条链路参与调度：填空隙的那几种永远有活干，留着会让「起了谁」
    这类断言先看到它们。也顺带保证**没有第二条链路在同一时间窗里倒**，否则
    「多个任务一起倒」那条豁免会把失败免记掉，三次根本攒不满。
    """
    enable(repository, MissionKind.PIRATE)
    only_gap_filler(repository)
    scheduler.start()

    for index in range(MAX_CONSECUTIVE_FAILURES):
        crash_a_round(
            scheduler,
            launcher,
            clock,
            at=NOW + (RESTART_COOLDOWN + timedelta(minutes=1)) * index,
        )

    row = row_of(repository, MissionKind.PIRATE)
    assert row.disabled_reason is not None, "夹具没能把任务跑成「连崩自停」"
    assert row.disabled_recovery == DisabledRecovery.BACKOFF.value
    assert row.retry_after_utc == FIRST_RETRY_AT
    return row


# -- 没到时间：不恢复 ----------------------------------------------------------


def test_it_stays_disabled_while_the_cooldown_is_still_running(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, recorded: RecordingLog
) -> None:
    """冷却没到期就一动不动，而且**一发都不许起**。

    ⚠️ **时钟一分钟一格往前挪，每格都 tick。** 一次跳到底的话，「每 tick 都放出来
    再立刻停用」那种实现可以蒙混过关——库里那两列看起来一直没变过。churn 只有
    在这里留得下痕迹：放一次写一条恢复日志，重新停用又写一条。

    挡住空转的就是这一段：它正是 `MAX_CONSECUTIVE_FAILURES` 当初要防的那个满速
    重启循环，只不过现在挡它的是冷却，不是「永不恢复」。
    """
    crash_until_disabled(scheduler, repository, launcher, clock)
    started = len(launcher.spawned)
    recorded.messages.clear()
    recorded.payloads.clear()

    minutes = int((FIRST_RETRY_AT - DISABLED_AT).total_seconds() // 60)
    for minute in range(minutes):
        clock.now = DISABLED_AT + timedelta(minutes=minute)
        scheduler.tick()

        row = row_of(repository, MissionKind.PIRATE)
        assert row.disabled_reason is not None, f"第 {minute} 分钟就被放出来了，冷却还没到期"
        assert row.disabled_recovery == DisabledRecovery.BACKOFF.value
        assert backoff_lines(recorded) == [], f"第 {minute} 分钟放出来又立刻停用了一次"
    assert len(launcher.spawned) == started


# -- 到时间了：自动恢复 --------------------------------------------------------


def test_it_comes_back_by_itself_once_the_cooldown_expires(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """这一条是整次改动的正面用例。

    2026-08-28 那一夜六个任务 01:02:49 被停用，按这条曲线它们会在 **01:17** 自己
    回来——而实际上它们一直关到早上用户手动打开。
    """
    crash_until_disabled(scheduler, repository, launcher, clock)

    clock.now = FIRST_RETRY_AT
    scheduler.tick()

    row = row_of(repository, MissionKind.PIRATE)
    assert row.disabled_reason is None
    assert row.disabled_recovery is None
    # 闹钟跟着这次停用一起结束：留着就是给一个已经不存在的停用留着闹钟。
    assert row.retry_after_utc is None


def test_the_recovered_task_actually_gets_scheduled_again(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """清掉那几列还不够，它得**真的重新开始派遣**。

    只清库不参与调度，页面上会从「已停用」变成「待命」然后一直待命——比一直
    显示「已停用」更难查。
    """
    crash_until_disabled(scheduler, repository, launcher, clock)
    started = len(launcher.spawned)

    clock.now = FIRST_RETRY_AT
    for _ in range(3):
        scheduler.tick()

    assert launcher.kinds[started:] == [MissionKind.PIRATE]


def test_the_backoff_round_survives_a_process_restart(  # type: ignore[no-untyped-def]
    repository, launcher, clock, session_factory
) -> None:
    """**新进程、新调度器对象，内存里一个字都没有——照样按时回来。**

    这一条钉的是「状态存库不存内存」。挂内存闹钟的实现在这里会**永远不恢复**：
    重启把闹钟丢了，也就再没人放它出来。而控制台重启是家常便饭（改配置、
    装新版本），2026-08-28 那一夜之后用户重启过好几次。

    ⚠️ 先用一个更早的时刻 tick 一遍：一个「重启之后从头开始数 15 分钟」的实现
    会在这里被放出来一次，从而在第一段断言上转红。
    """
    first_clock = Clock(NOW)
    first = MissionScheduler(repository, make_supervisor(launcher, first_clock), clock=first_clock)
    first.prepare()
    crash_until_disabled(first, repository, launcher, first_clock)

    # 新进程，起点就在停用之后不久。
    later = Clock(DISABLED_AT + timedelta(minutes=1))
    second = MissionScheduler(repository, make_supervisor(launcher, later), clock=later)
    second.prepare()
    second.start()
    for _ in range(3):
        second.tick()
    assert row_of(repository, MissionKind.PIRATE).disabled_reason is not None, (
        "重启之后就把冷却从头算了"
    )

    later.now = FIRST_RETRY_AT
    second.tick()

    assert row_of(repository, MissionKind.PIRATE).disabled_reason is None


# -- ⚠️⚠️ 用户自己关掉的任务永远不被碰 ------------------------------------------


def test_a_task_the_user_switched_off_is_never_switched_back_on(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, recorded: RecordingLog
) -> None:
    """**这条最重要，而且它守的是生产上正关着的四个任务。**

    「侦查+攻击海盗」「扫描全星系 bot」「5 系攻击」「9 系攻击」是用户手动关的，
    必须一直关着。库里的区别只有一处：手动关的 `disabled_reason` **为 NULL**
    （`update_mission_task` 会把停用标记连同退避两列一起清掉），自动停用的不为
    NULL。判据只认这一处，**一个字都不看 `enabled`**。

    摆法是把它先跑成自动停用（于是它带着一份**过期的**退避状态），再由用户手动
    勾掉复选框——这正是生产上会发生的次序，也是「看 `enabled` 就会放行」那种改法
    最容易踩中的现场。等三天都不许有任何变化。
    """
    crash_until_disabled(scheduler, repository, launcher, clock)
    disable(repository, MissionKind.PIRATE)
    started = len(launcher.spawned)
    recorded.messages.clear()
    recorded.payloads.clear()

    for hours in range(0, 72, 6):
        clock.now = FIRST_RETRY_AT + timedelta(hours=hours)
        for _ in range(3):
            scheduler.tick()

        row = row_of(repository, MissionKind.PIRATE)
        assert row.enabled is False, f"第 {hours} 小时把用户自己关掉的任务打开了"
        assert row.disabled_reason is None
    assert len(launcher.spawned) == started
    assert backoff_lines(recorded) == []


def test_a_switched_off_task_is_safe_even_with_stale_backoff_markers(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, recorded: RecordingLog
) -> None:
    """更狠的一版：**直接把库摆成最坏的样子**，判据仍然不许碰它。

    上一条走的是真实路径，于是有三道闸一起挡着（`update_mission_task` 清了标记、
    清了闹钟，判据又认标记）。三道闸的问题是**互相兜底**：拆掉其中任何一道，
    用例照样绿——而拆掉的那一道哪天就没人守了。

    这里绕过所有写入口，直接把行摆成「用户关着（`disabled_reason` 为 NULL）+
    带着一份过期的 `BACKOFF` 标记和早就到点的闹钟」，只留下**一条**判据挡着：
    `disabled_reason IS NOT NULL`。把它改成看 `enabled`、或者干脆去掉，这条当场
    转红——而后果是生产上那四个用户手动关掉的任务被自动打开。
    """
    enable(repository, MissionKind.PIRATE)
    only_gap_filler(repository)
    pirate = task_id(repository, MissionKind.PIRATE)
    with session_factory() as session:
        row = session.get(orm.MissionTaskRow, pirate)
        assert row is not None
        row.enabled = False
        row.disabled_reason = None
        row.disabled_recovery = DisabledRecovery.BACKOFF.value
        row.retry_after_utc = NOW - timedelta(hours=1)
        row.backoff_rounds = 3
        session.commit()
    scheduler.start()

    for hours in range(0, 72, 6):
        clock.now = NOW + timedelta(hours=hours)
        for _ in range(3):
            scheduler.tick()

        after = row_of(repository, MissionKind.PIRATE)
        assert after.enabled is False, f"第 {hours} 小时把用户自己关掉的任务打开了"
        assert after.disabled_recovery == DisabledRecovery.BACKOFF.value, (
            "谁动了这一行——判据本该一个字都不碰它"
        )
        assert after.retry_after_utc == NOW - timedelta(hours=1)
    assert launcher.spawned == []
    assert backoff_lines(recorded) == []


# -- 退避曲线：连着崩就等得更久 ------------------------------------------------


def test_a_second_disable_waits_twice_as_long(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """恢复之后又崩 → 落到下一档（30 分），**不是从头再来**。

    从头再来的实现每轮都等 15 分钟，一条真坏了的链路一天要白起 96 次；更要紧的是
    它永远退不出去，而这条曲线的全部意义就是「越像是真坏了，问得越少」。

    ⚠️ 恢复时**不清 `consecutive_failures`**，所以放回来之后**再崩一次**就重新
    停用——一轮只白试一次。这条用例顺带把那件事钉住了：如果哪天有人在恢复时
    顺手清零，这里就要崩三次才会重新停用，`retry_after_utc` 对不上。
    """
    crash_until_disabled(scheduler, repository, launcher, clock)

    clock.now = FIRST_RETRY_AT
    scheduler.tick()
    assert row_of(repository, MissionKind.PIRATE).disabled_reason is None
    scheduler.tick()
    launcher.latest.exit_code = 1
    crashed_at = FIRST_RETRY_AT + timedelta(minutes=1)
    clock.now = crashed_at
    scheduler.tick()

    row = row_of(repository, MissionKind.PIRATE)
    assert row.disabled_reason is not None, "放回来之后再崩一次就该重新停用"
    assert row.backoff_rounds == 2
    assert row.retry_after_utc == crashed_at + timedelta(minutes=30)


# -- 归零点：任何任务跑出退出码 0 ----------------------------------------------


def test_any_clean_exit_resets_the_backoff_rounds(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """**别的链路**跑通一轮，退避轮次就归零。

    那一刻环境被证明是好的：窗口在、会话在、鼠标是我们的。让下一次停用还从上一
    串的档位起数，等于拿昨夜的账去罚今天——而这个计数唯一的用途就是量「这一串
    坏了多久」。归零点与 `MAX_ENVIRONMENT_EXEMPTIONS` 共用 `_finish` 里同一处。

    ⚠️ 跑通的是**扫描**，被清的是**海盗**：这一条钉的正是「任何任务」这三个字。
    按任务清的实现在这里转红——海盗自己一轮都没跑通过。
    """
    crash_until_disabled(scheduler, repository, launcher, clock)
    assert row_of(repository, MissionKind.PIRATE).backoff_rounds == 1

    # 扫描本来被夹具关着（填空隙的那几种会抢空隙），这里专门放它跑一轮好的。
    enable(repository, MissionKind.SCAN)
    clock.now = DISABLED_AT + timedelta(minutes=1)
    scheduler.tick()
    assert launcher.latest.kind is MissionKind.SCAN, "扫描没起来，这条用例量不到东西"
    launcher.latest.exit_code = 0
    clock.now = DISABLED_AT + timedelta(minutes=2)
    scheduler.tick()

    assert row_of(repository, MissionKind.PIRATE).backoff_rounds == 0


def test_after_a_clean_exit_the_next_disable_starts_at_fifteen_minutes_again(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """归零之后，下一次停用又从第 1 轮（15 分钟）起数。

    上一条只钉了那个计数变成 0；这一条钉的是它**真的被用来算间隔**——把归零写成
    只改计数不影响曲线的实现（比如另存一份「历史最高档」）在这里转红。
    """
    crash_until_disabled(scheduler, repository, launcher, clock)

    # 恢复 → 跑通一轮（自己跑通同样是归零点）→ 再连崩三次。
    clock.now = FIRST_RETRY_AT
    scheduler.tick()
    scheduler.tick()
    launcher.latest.exit_code = 0
    clock.now = FIRST_RETRY_AT + timedelta(minutes=1)
    scheduler.tick()
    assert row_of(repository, MissionKind.PIRATE).backoff_rounds == 0

    # 起点让过一个重启冷却：上一轮刚在 `FIRST_RETRY_AT` 起过。
    base = FIRST_RETRY_AT + RESTART_COOLDOWN + timedelta(minutes=1)
    for index in range(MAX_CONSECUTIVE_FAILURES):
        crash_a_round(
            scheduler, launcher, clock, at=base + (RESTART_COOLDOWN + timedelta(minutes=1)) * index
        )

    row = row_of(repository, MissionKind.PIRATE)
    assert row.backoff_rounds == 1
    assert row.retry_after_utc == clock.now + timedelta(minutes=15)


# -- 日志：出事时只靠库里的 `system_log` 就要定位得了 --------------------------


def test_the_disable_says_when_it_will_retry_and_which_round_this_is(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, recorded: RecordingLog
) -> None:
    """⚠️ **这条链路原先整个是哑的。**

    自动停用写在 `repository.record_mission_failure` 里、不走 `_disable_task`，
    所以 2026-08-28 那一夜六个任务被关掉的那一刻，`system_log` 里一个字都没有——
    只能从 `mission_tasks.disabled_reason` 反推，而那一列会被下一次停用覆盖掉。
    实机在另一台机器上，日志是唯一查得到的东西。

    要能回答的三件事：什么时候被自动停用的、这是第几轮退避、下次什么时候重试。
    """
    row = crash_until_disabled(scheduler, repository, launcher, clock)

    lines = backoff_lines(recorded)
    assert len(lines) == 1, "连崩自停没有留下任何一条日志"
    message, payload = lines[0]
    assert payload["task_id"] == task_id(repository, MissionKind.PIRATE)
    assert payload["mission_kind"] == MissionKind.PIRATE.value
    assert payload["disabled_reason"] == row.disabled_reason
    assert payload["disabled_recovery"] == DisabledRecovery.BACKOFF.value
    assert payload["consecutive_failures"] == MAX_CONSECUTIVE_FAILURES
    assert payload["exit_code"] == 1
    assert payload["backoff_round"] == 1
    assert payload["retry_after_utc"] == FIRST_RETRY_AT.isoformat()
    assert "自动停用" in message
    assert "自动重试" in message, "没说清它会自己回来"


def test_the_recovery_says_who_let_it_out(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, recorded: RecordingLog
) -> None:
    """⚠️ 恢复那一下必须留痕，而且要说清**是谁放的它**。

    用户在页面上点「恢复」和调度器自己放它出来，在 `mission_tasks` 里长得一模
    一样（两列都被清成 NULL）。事后分不清这两者，「昨晚它到底是自己好的还是我
    去点了一下」就永远没有答案——而这正是排查「环境是不是真好了」的第一个问题。

    只留**一条**：tick 每秒一次，恢复那一下如果每 tick 都刷，真正要看的那一条
    会被淹掉。恢复之后标记就清了，所以这里连 tick 五次再数条数。
    """
    crash_until_disabled(scheduler, repository, launcher, clock)
    recorded.messages.clear()
    recorded.payloads.clear()

    clock.now = FIRST_RETRY_AT
    for _ in range(5):
        scheduler.tick()

    lines = backoff_lines(recorded)
    assert len(lines) == 1
    message, payload = lines[0]
    assert payload["resumed_by"] == "scheduler-backoff"
    assert payload["disabled_recovery"] == DisabledRecovery.BACKOFF.value
    assert payload["backoff_round"] == 1
    assert payload["retry_after_utc"] == FIRST_RETRY_AT.isoformat()
    assert payload["task_id"] == task_id(repository, MissionKind.PIRATE)
    assert "自动恢复" in message


def test_every_backoff_line_carries_the_version_fingerprint(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, recorded: RecordingLog
) -> None:
    """停用与恢复两条都要带版本指纹。

    ⚠️ **这一条是仓库刚吃过的亏。** 实机在另一台机器上，事后只能靠库里的
    `system_log` 判断「生产那会儿跑的到底是哪一版」。`disabled_recovery` 那一列
    是 `BACKOFF` 只说明列写对了，说不出退避曲线是哪一条——而曲线正是这次要
    观察的东西。指纹与曲线不许走散，那一条钉在
    `tests/unit/domain/test_backoff_recovery.py`。
    """
    from evo_helper.domain.scheduler import BACKOFF_SCHEME

    crash_until_disabled(scheduler, repository, launcher, clock)
    clock.now = FIRST_RETRY_AT
    scheduler.tick()

    lines = backoff_lines(recorded)
    assert len(lines) == 2, "停用与恢复应当各留一条"
    assert [payload["backoff_scheme"] for _, payload in lines] == [BACKOFF_SCHEME] * 2


# -- 两条恢复路径不许搅在一起 --------------------------------------------------


def test_the_backoff_pass_never_claims_a_line_shortage_disable(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, recorded: RecordingLog
) -> None:
    """`FREE_LINES` 那一档仍然由**航线那条路**处理，退避这条一个字都不许碰。

    这里摆一个 `FREE_LINES` 停用、把时钟推到很久以后。它**会**被放出来——那是
    航线那条路干的活，航线管够，判定成立（正面/反面用例全在
    `test_line_shortage_recovery.py`，那份**逐字未改**）。这条用例钉的是**谁**
    放的它：退避那一路只认 `BACKOFF` 标记，所以它既不该写退避日志，也不该在这
    行上留下任何退避状态。

    两条路搅在一起的后果是真的：退避按时间放行，而那一刻航线可能仍然满着，
    任务会一放出来就再停一次——2026-08-18 那种一小时 1368 行的日志正是这么来的。
    """
    enable(repository, MissionKind.PIRATE)
    only_gap_filler(repository)
    repository.disable_mission_task(
        task_id(repository, MissionKind.PIRATE),
        "空闲航线不足，暂不启动 bot 攻击",
        recovery=DisabledRecovery.FREE_LINES,
    )
    scheduler.start()

    clock.now = NOW + timedelta(days=2)
    for _ in range(5):
        scheduler.tick()

    row = row_of(repository, MissionKind.PIRATE)
    assert backoff_lines(recorded) == [], "退避那一路认领了一个航线不足的停用"
    assert row.retry_after_utc is None
    assert row.backoff_rounds == 0
