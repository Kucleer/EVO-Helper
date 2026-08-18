"""航线满 + 欠战报的 bot 任务：**一句话都不该说，一个进程都不该起。**

## 这一条修的是什么

生产实测（`system_log`，2026-08-18 16:13 → 08-19 00:04，7.8 小时）：

```
2号球 这一轮没活干：本轮没有可派遣的军力攻击目标
{"logger": "evo_helper.application.mission_scheduler", "func": "_launch"}
```

**6,661 行**，占 `system_log` 全表的 22%，`task_id` 列**全部为 NULL**，节奏基本是
每个 tick 一条（23:49 那一分钟 50 条）。链条是这样的——每一步都用生产库复核过：

1. 出发点 `9:250:8` 配了 4 条航线，4 条全在飞 → `_origin_budgets` 算出预算 0 →
   `TaskFacts.free_lines == 0` → `domain.scheduler` 的 `can_dispatch` 为假；
2. 那颗星球上另有 8 发攻击的战报**永远认领不上**（OCR 把 `attacker_origin` 的
   `9` 读成了 `3`：全库 `3:250:8` 有 7 份战报、7 份全部未认领，`9:250:8` 一份没有）
   → `reports_due` 恒为真 → `has_work` 的右半边说「有活干」；
3. `_launch` 走 `_military_command`，而**那个函数只会派遣**：预算 0 的出发点拿不到
   任何目标 → `assignments` 为空 → `raise MissionIdle("本轮没有可派遣的军力攻击目标")`；
4. `LaunchOutcome.IDLE` 本 tick 不再重算（PR #190），但下一个 tick 从头再来。

也就是**「有没有活干」和「能不能干」用的不是同一把尺子**——和
`_origin_budgets` 注释里记的 2026-08-18 01:00 那一小时 447 次自动停用/恢复同源。

## ⚠️ 为什么修的是 `has_work`，不是「那就去收战报」

`_military_command` 若改成「没航线就进信箱收战报」，后果**比现在更糟**：认领那个
缺陷还在，收回来照样认领不上，`reports_due` 还是真，于是 runner 一趟趟地进信箱
——烧的是**真实鼠标时间**，而不是像现在只多写几行日志。`_reports_due` 的
docstring 逐字预言过这个形状：「调度器每个 tick 都去收一封永远不会到的战报，
扫描永远抢不到空隙」。所以这里的判据是**有界性**：航线满着连 tick 多少次，
起的子进程数都必须是 0。

## 三条判据

1. **航线满 + 欠战报 → 不起轮、不写日志、不停用**（`has_work` 那一半）；
2. **航线有、池子挑不出人 → 仍然会抛 `MissionIdle`，那一档必须限流**
   （对齐挡不掉这一类，见 `_log_an_idle_round`）；
3. **限流之后那条日志的 `task_id` 要落到列上**，不是只躺在消息正文里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.mission_scheduler import REPEATED_LOG_WINDOW, MissionScheduler
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_BOT
from evo_helper.domain.scheduler import MissionKind, TaskStatus, status_of
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import (
    add_bot_target,
    dispatch,
    enable,
    only_gap_filler,
    set_config,
    task,
    task_id,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: 种子任务的出发星球，也是下面所有派遣记在的那一颗。
ORIGIN = Coordinate(2, 137, 18)
#: 打谁不重要，只要位次避开 1–4（那四个位次是游戏固定生成的海盗，`is_bot_coordinate`
#: 会整个剔掉，于是任务一个目标都没有、被判成「已完成」而不是「等航线」）。
TARGET = Coordinate(2, 150, 8)

#: 军力优先。**上限 100**——库里那批候选全都强过它，于是：
#: `_facts` 把 `targets_remaining` 填成「有军力读数的候选数」（第 2 步之后），
#: 而 `max_score` 要到第 4 步才生效，于是 `has_work()` 看得见「还有目标」、
#: `_military_command()` 却挑不出一个来。这正是**对齐之后仍然可达**的那一档
#: `MissionIdle`，也就是限流真正要挡的东西。
ALL_TOO_STRONG = '{"by_military": true, "top_n": 2, "score_max_age_hours": 24, "max_score": 100}'

#: 同上，但不设上限：池子挑得出人，只看航线够不够。
BY_MILITARY = '{"by_military": true, "top_n": 2, "score_max_age_hours": 24}'


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
        self.entries: list[tuple[str, str, dict[str, object], int | None]] = []

    def __call__(  # type: ignore[no-untyped-def]
        self, level, source, message, *, payload=None, logged_at_utc=None, task_id=None, **_
    ):
        self.entries.append((level, message, dict(payload or {}), task_id))

    @property
    def idle(self) -> list[tuple[str, str, dict[str, object], int | None]]:
        return [item for item in self.entries if "这一轮没活干" in item[1]]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


# -- 夹具 ----------------------------------------------------------------------


def lines_full_and_a_report_owed(  # type: ignore[no-untyped-def]
    repository: SqlAlchemyRepository, session_factory, run_id, *, params_json: str = BY_MILITARY
) -> None:
    """把生产那个现场原样摆出来：**航线全满，而且欠着一份永远认领不上的战报。**

    - 唯一那条航线被一发还在飞的攻击占住（还要 25 分钟才回来）→ `free_lines == 0`；
    - 另有一发两小时前打出去、飞行时间没读到、战报还没到 → `ReportWaitPlanner`
      判「该去收」（两小时仍在 `MAX_REPORT_AGE` 以内），而它自己早已过了
      `UNKNOWN_LINE_HOLD`（90 分钟），所以不再占航线——占航线的只有前一发。

    池子里放两颗**读数很新、而且没被这两发打过**的目标：这样「不起轮」只可能是
    因为航线，不会和「池子挑不出人」「本轮已完成」那两档混起来——选靶第 1 步会把
    重复攻击间隔内打过的排除掉，拿被打过的坐标去凑池子，`targets_remaining` 会
    归零、状态变成「已完成」，那是另一件事。
    """
    set_config(session_factory, fleet_line_limit=1, reserved_lines=0)
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == MissionKind.BOT.value)
    enable(repository, MissionKind.BOT, params_json=params_json)
    only_gap_filler(repository)
    for index in range(2):
        add_bot_target(
            session_factory,
            Coordinate(2, 160 + index, 8),
            military_score=9_000.0 - index,
            scanned_at=NOW,
        )
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=TARGET,
        dispatched_at=NOW - timedelta(hours=2),
        origin=ORIGIN,
    )
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(2, 151, 8),
        dispatched_at=NOW - timedelta(minutes=10),
        flight=timedelta(minutes=25),
        origin=ORIGIN,
    )


def a_pool_with_nothing_dispatchable(  # type: ignore[no-untyped-def]
    repository: SqlAlchemyRepository, session_factory
) -> None:
    """航线管够，但池子里一个都挑不出来（全都强过 `max_score`）。

    ⚠️ **这一档是 `has_work` 对齐之后仍然可达的 `MissionIdle`**，也就是「限流不是
    多余的」那句话的证据。生产里对应的是「候选全在保护期里 / 全在重复攻击间隔里 /
    全被军力上限挡在外面」。
    """
    set_config(session_factory, fleet_line_limit=6, reserved_lines=0)
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == MissionKind.BOT.value)
    for index in range(3):
        add_bot_target(
            session_factory,
            Coordinate(2, 400 + index, 5),
            military_score=9_000.0 - index,
            scanned_at=NOW,
        )
    enable(repository, MissionKind.BOT, params_json=ALL_TOO_STRONG)
    only_gap_filler(repository)


# -- (1) 航线满 + 欠战报：一句话都不说 -----------------------------------------


def test_a_bot_task_with_full_lines_and_a_due_report_says_nothing(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id, clock, recorded: RecordingLog
) -> None:
    """⚠️ **这条是本次改动的正面用例：6,661 行噪声在这里归零。**

    连 tick 30 次、时钟往前走 15 分钟（那条航线要 25 分钟才空），期间：

    - 一条「这一轮没活干」都不写；
    - 一个子进程都不起。

    改之前实测是**每 tick 一条**，30 次就是 30 条。把
    `or facts.of(task).reports_due` 加回 `domain.scheduler.has_work` 的 BOT 那一支，
    这条当场转红。
    """
    lines_full_and_a_report_owed(repository, session_factory, run_id)
    scheduler.start()

    for minute in range(30):
        clock.now = NOW + timedelta(seconds=30 * minute)
        scheduler.tick()

    assert recorded.idle == [], f"航线满着却写了 {len(recorded.idle)} 行「这一轮没活干」"


def test_a_bot_task_with_full_lines_never_opens_the_mailbox(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, run_id, clock
) -> None:
    """⚠️ **有界性：航线满着连 tick 多少次，起的子进程数都是 0。**

    这一条挡的是那个**看起来更热心**的改法——「没航线就去收战报」。战报认领那个
    缺陷还在（`attacker_origin` 的 `9` 被读成 `3`），收回来照样认领不上，
    `reports_due` 还是真，于是 runner 会一趟趟真的进信箱翻邮件，烧的是真实鼠标
    时间。任何一种「让 `_military_command` / `_bot_command` 在预算为 0 时也组得出
    一条命令行」的实现，都会让这里的 `spawned` 变成非空。

    ⚠️ **别把断言放宽成「起的次数有限」。** 上界只能是 0：这条链路此刻**没有任何
    一件它做得成的事**，起一轮就是纯粹占着鼠标。
    """
    lines_full_and_a_report_owed(repository, session_factory, run_id)
    scheduler.start()

    for minute in range(30):
        clock.now = NOW + timedelta(seconds=30 * minute)
        scheduler.tick()

    assert launcher.spawned == [], "航线满着还是起了 runner——那一趟是去信箱扑空的"


def test_a_bot_task_with_full_lines_is_never_disabled(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id, clock
) -> None:
    """⚠️ **「不吵」不许靠「把它停用掉」换来。**

    自动停用会挂上 `disabled_reason`，用户不去页面点一次「恢复」就永远不跑
    （2026-08-18 01:00 那一小时因此自动停用 447 次）。而航线满是一档**会自己好
    起来**的间歇：舰队飞回来就完了。
    """
    lines_full_and_a_report_owed(repository, session_factory, run_id)
    scheduler.start()

    for minute in range(30):
        clock.now = NOW + timedelta(seconds=30 * minute)
        scheduler.tick()

    row = task(repository, MissionKind.BOT)
    assert row.disabled_reason is None, f"任务被自动停用了：{row.disabled_reason}"
    assert row.disabled_recovery is None
    assert row.enabled is True


def test_the_page_says_it_is_waiting_for_a_line(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id
) -> None:
    """不吵不等于装死：页面上要说得出**为什么不动**。

    `status_of` 复用 `has_work`，所以「没活干」之后它会往下走那串原因，落在
    「等航线」上——那正是实话，而且是用户等一等就会自己好的那一档。
    """
    lines_full_and_a_report_owed(repository, session_factory, run_id)
    scheduler.start()
    scheduler.tick()

    view = scheduler.snapshot()
    bot = next(item for item in view.snapshots if item.kind is MissionKind.BOT)
    assert status_of(bot, view.facts, running=view.running) is TaskStatus.WAITING_LINES


# -- (2) 航线有、池子挑不出人：仍然会 idle，那一档必须限流 ---------------------


def test_an_unavoidable_idle_verdict_is_written_once_per_window(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, clock, recorded: RecordingLog
) -> None:
    """⚠️ **对齐挡不掉这一类，所以限流不是多余的。**

    这里航线管够（`can_dispatch` 为真、`has_work` 说有活干），可池子里那三个全都
    强过 `max_score` → `_military_command` 照样空手而归。它每个 tick 都会重来一次，
    而判定一个字都没变。

    连 tick 60 次、时钟只往前走 60 秒（不到 `REPEATED_LOG_WINDOW` 的 120 秒）：
    **只该写一条**。删掉 `_log_an_idle_round` 里的限流（换回 `_LOGGER.info` 或
    直接 `record_system_log`），这里会看到 60 条。
    """
    a_pool_with_nothing_dispatchable(repository, session_factory)
    scheduler.start()

    for second in range(60):
        clock.now = NOW + timedelta(seconds=second)
        scheduler.tick()

    assert len(recorded.idle) == 1, f"限流没生效，写了 {len(recorded.idle)} 条"


def test_an_unavoidable_idle_verdict_never_disables_the_task(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, clock
) -> None:
    """⚠️ **限流不许顺手改成「吵一次就把它关掉」。**

    `MissionIdle` 走的是「不停用、不记失败、下一 tick 重算」，理由写在
    `_military_command` 的 docstring 里：判成 `MissionParamError` 会调
    `disable_mission_task`，用户不去页面点「恢复」它就永远不跑——2026-08-18
    01:00 那一小时因此自动停用 447 次。而这里的空手而归全都是**会自己好起来**
    的一档：军力榜扫到弱一点的目标就成立了。
    """
    a_pool_with_nothing_dispatchable(repository, session_factory)
    scheduler.start()

    for second in range(30):
        clock.now = NOW + timedelta(seconds=second)
        scheduler.tick()

    row = task(repository, MissionKind.BOT)
    assert row.disabled_reason is None, f"一次正常的间歇被当成配置错误了：{row.disabled_reason}"
    assert row.disabled_recovery is None
    assert launcher.spawned == [], "池子一个都挑不出来，却起了 runner"


def test_the_idle_line_comes_back_once_the_window_has_passed(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, clock, recorded: RecordingLog
) -> None:
    """限流是「一个窗口最多一条」，不是「一辈子只报一次」。

    一整夜的停摆该在日志里留下持续的痕迹——只报一次的话，翻日志的人会以为它早
    就恢复了。而且被压掉的次数要如实交代出去，不许假装没发生过。
    """
    a_pool_with_nothing_dispatchable(repository, session_factory)
    scheduler.start()

    scheduler.tick()
    for second in range(1, 60):
        clock.now = NOW + timedelta(seconds=second)
        scheduler.tick()
    clock.now = NOW + REPEATED_LOG_WINDOW + timedelta(seconds=1)
    scheduler.tick()

    assert len(recorded.idle) == 2, "过了一个窗口之后应该再写一条"
    second_line = recorded.idle[1]
    assert second_line[2]["suppressed_since_last_log"] == 59, "被压掉的次数没如实交代"
    assert "59" in second_line[1], "消息正文里没说被压掉了几次"


def test_a_changed_idle_verdict_is_written_immediately(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, clock, recorded: RecordingLog
) -> None:
    """⚠️ **判定变了立刻写，不被时间窗压掉。**

    只有窗口、没有「变了就写」的实现，会把「航线余量从 6 掉到 0」这种跃迁压掉
    整整两分钟——而那一刻正是排障要找的东西。这里把航线数从 6 改成 3，签名里的
    `free_lines` 跟着变，同一个窗口之内必须立刻再写一条。
    """
    a_pool_with_nothing_dispatchable(repository, session_factory)
    scheduler.start()
    scheduler.tick()
    assert len(recorded.idle) == 1

    set_config(session_factory, fleet_line_limit=3)
    clock.now = NOW + timedelta(seconds=1)
    scheduler.tick()

    assert len(recorded.idle) == 2, "判定变了却被时间窗压掉了"
    assert recorded.idle[1][2]["signature_changed"] is True


# -- (3) 结构化字段里认得出是哪个任务 -----------------------------------------


def test_the_idle_line_carries_the_task_id_in_a_structured_field(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **`task_id` 要落到 `system_log.task_id` 那一列上，不是只在消息正文里。**

    生产那 6,661 行的 `task_id` 全是 NULL——只能从正文里的「2号球」三个字认出是
    哪个任务，按任务过滤日志根本做不到。成因是那句 `_LOGGER.info` 经
    `SystemLogHandler` 落库，而那座桥取的是**进程**身份（`current_context()`），
    控制台进程不属于任何一轮。

    payload 里那份是给人读的，列上这份是给 `WHERE` 用的，**两份都要**。
    """
    a_pool_with_nothing_dispatchable(repository, session_factory)
    scheduler.start()
    scheduler.tick()

    level, message, payload, column_task_id = recorded.idle[0]
    expected = task_id(repository, MissionKind.BOT)
    assert column_task_id == expected, "`system_log.task_id` 那一列还是空的"
    assert payload["task_id"] == expected
    assert payload["mission_kind"] == MissionKind.BOT.value
    assert level == "INFO"
    assert "本轮没有可派遣的军力攻击目标" in message, "原因没带上，只剩一句「没活干」"
    # 说清「为什么这不是去收战报」：这两个数是那一刻的现场。
    assert payload["free_lines"] == 6
    assert "reports_due" in payload
