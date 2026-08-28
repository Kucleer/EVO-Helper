"""子进程台账：起停记录、孤儿标记、连续失败自停。

这些是调度循环唯一的记忆。记漏一条，重启后的控制台就说不清上一次到底停在哪。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.scheduler import MissionKind

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def task_id(repository, kind: MissionKind) -> int:  # type: ignore[no-untyped-def]
    """这条链路那一行的 id。

    任务的身份是 `id` 而不是 `kind`（同一 kind 可以有多行），所以写库的入口全部
    按 id 寻址；测试也就得先把 id 捞出来。
    """
    return next(row.id for row in repository.mission_tasks() if row.kind == kind.value)


def test_a_run_is_recorded_when_it_starts_and_closed_when_it_ends(repository) -> None:  # type: ignore[no-untyped-def]
    run_id = repository.begin_mission_run(
        MissionKind.SCAN,
        task_id=3,
        command=["python", "-m", "evo_helper.tools.scan_coordinates"],
        pid=4242,
        started_at_utc=NOW,
        log_path="var/logs/mission-scan.log",
    )

    repository.finish_mission_run(
        run_id, ended_at_utc=NOW + timedelta(minutes=3), exit_code=0, stopped_by="SELF"
    )

    row = repository.mission_runs(limit=10)[0]
    assert row.pid == 4242
    assert row.exit_code == 0
    assert row.stopped_by == "SELF"
    # 命令行原样存下来，事后翻账「那一轮到底打了谁」全靠它。
    assert "scan_coordinates" in row.command


def test_the_last_start_per_task_is_what_the_cooldown_reads(repository) -> None:  # type: ignore[no-untyped-def]
    """冷却按**启动**算，而且**按任务分**——取的是每个 `task_id` 最新的
    `started_at_utc`。

    按 kind 分的话，同一链路的两个任务会共用一份冷却：主星那个刚跑完，
    2 号星那个就得干等五分钟，而它俩占的根本不是同一份航线。
    """
    for minutes in (30, 5):
        repository.begin_mission_run(
            MissionKind.PIRATE,
            task_id=1,
            command=["python"],
            pid=None,
            started_at_utc=NOW - timedelta(minutes=minutes),
            log_path="var/logs/mission-pirate.log",
        )
    repository.begin_mission_run(
        MissionKind.SCAN,
        task_id=3,
        command=["python"],
        pid=None,
        started_at_utc=NOW - timedelta(hours=1),
        log_path="var/logs/mission-scan.log",
    )

    starts = repository.last_mission_starts()

    assert starts[1] == NOW - timedelta(minutes=5)
    assert starts[3] == NOW - timedelta(hours=1)
    assert 2 not in starts


def test_orphans_are_marked_unknown_rather_than_shot_by_pid(repository) -> None:  # type: ignore[no-untyped-def]
    """控制台重启时，`ended_at_utc` 为空的行说明上次没走正常的关闭路径。

    **不按 pid 自动杀**——pid 会被系统回收复用，照着一个可能已经换了主人的号码
    开枪比留个警告更糟。所以只标记，剩下的交给页面上的红条和「强制结束」。
    """
    repository.begin_mission_run(
        MissionKind.BOT,
        task_id=2,
        command=["python"],
        pid=9999,
        started_at_utc=NOW - timedelta(minutes=20),
        log_path="var/logs/mission-bot.log",
    )

    assert repository.mark_orphan_mission_runs(ended_at_utc=NOW) == 1

    row = repository.mission_runs(limit=1)[0]
    assert row.stopped_by == "UNKNOWN"
    # 结束时刻也补上，否则这一行永远显示成「运行中」。
    assert row.ended_at_utc == NOW
    assert row.pid == 9999


def test_a_closed_run_is_not_marked_as_an_orphan(repository) -> None:  # type: ignore[no-untyped-def]
    run_id = repository.begin_mission_run(
        MissionKind.SCAN,
        task_id=3,
        command=["python"],
        pid=None,
        started_at_utc=NOW - timedelta(minutes=20),
        log_path="var/logs/mission-scan.log",
    )
    repository.finish_mission_run(run_id, ended_at_utc=NOW, exit_code=0, stopped_by="USER")

    assert repository.mark_orphan_mission_runs(ended_at_utc=NOW) == 0


# -- 连续失败自停 --------------------------------------------------------------


def test_three_consecutive_failures_disable_the_task(repository) -> None:  # type: ignore[no-untyped-def]
    """没有这条，调度循环会在一个坏掉的任务上变成满速空转的重启循环。"""
    repository.ensure_mission_rows(now_utc=NOW)

    pirate = task_id(repository, MissionKind.PIRATE)
    for _ in range(3):
        repository.record_mission_failure(pirate, exit_code=1, limit=3, now_utc=NOW)

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.consecutive_failures == 3
    assert row.disabled_reason is not None


def test_two_failures_are_not_enough(repository) -> None:  # type: ignore[no-untyped-def]
    repository.ensure_mission_rows(now_utc=NOW)

    pirate = task_id(repository, MissionKind.PIRATE)
    for _ in range(2):
        repository.record_mission_failure(pirate, exit_code=1, limit=3, now_utc=NOW)

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.disabled_reason is None


def test_a_clean_exit_resets_the_streak(repository) -> None:  # type: ignore[no-untyped-def]
    """「连续」是连续。中间成功过一次，之前那两次就不该再算数。"""
    repository.ensure_mission_rows(now_utc=NOW)
    pirate = task_id(repository, MissionKind.PIRATE)
    for _ in range(2):
        repository.record_mission_failure(pirate, exit_code=1, limit=3, now_utc=NOW)

    repository.clear_mission_failures(pirate)
    repository.record_mission_failure(pirate, exit_code=1, limit=3, now_utc=NOW)

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.consecutive_failures == 1
    assert row.disabled_reason is None


def test_disabling_for_bad_parameters_says_why(repository) -> None:  # type: ignore[no-untyped-def]
    """参数不合格是配置问题，重试一万次也一样。写清原因，页面上标红给人看。"""
    repository.ensure_mission_rows(now_utc=NOW)

    repository.disable_mission_task(
        task_id(repository, MissionKind.BOT), reason="该范围内没有已记录的 bot；先跑扫描"
    )

    row = next(item for item in repository.mission_tasks() if item.kind == "BOT")
    assert row.disabled_reason == "该范围内没有已记录的 bot；先跑扫描"


# -- 退避自动恢复的写侧 --------------------------------------------------------
#
# 判据在 `domain.scheduler`（有它自己那份用例），端到端在
# `tests/integration/application/test_backoff_auto_recovery.py`。这里钉的是**写库
# 那几下**：哪一列被谁改、谁绝不许碰谁。


def test_the_auto_disable_marks_it_as_a_backoff_and_sets_the_alarm(repository) -> None:  # type: ignore[no-untyped-def]
    """连崩到上限之后，恢复方式是 `BACKOFF`，并且**当场定下重试时刻**。

    ⚠️ 2026-08-28 之前这里写的是 `MANUAL`，于是那一夜六个任务一直关到早上用户
    手动打开。防满速空转靠的是冷却，不是永不恢复。

    时刻按传进来的钟算，不是 `datetime.now()`：两个钟差一点，事后按日志排时间线
    就会把恢复排在停用之前，而时间相关的用例拿真实时钟一律恒绿。
    """
    repository.ensure_mission_rows(now_utc=NOW)
    pirate = task_id(repository, MissionKind.PIRATE)

    for _ in range(3):
        outcome = repository.record_mission_failure(pirate, exit_code=1, limit=3, now_utc=NOW)

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.disabled_recovery == "BACKOFF"
    assert row.backoff_rounds == 1
    assert row.retry_after_utc == NOW + timedelta(minutes=15)
    # 返回值说的是「**这一次调用**把它关掉了」，日志只该在跃迁那一下写。
    assert outcome.disabled_now is True
    assert outcome.backoff_round == 1
    assert outcome.retry_after_utc == row.retry_after_utc


def test_a_failure_on_an_already_disabled_task_is_not_a_new_transition(repository) -> None:  # type: ignore[no-untyped-def]
    """已经停用着的任务再记一次失败，不许当成新的一次跃迁。

    当成跃迁的话，日志里会出现一个**根本没发生过**的停用时刻，而且退避轮次会
    被多推一档——曲线凭空跳过 15 分钟那一格。
    """
    repository.ensure_mission_rows(now_utc=NOW)
    pirate = task_id(repository, MissionKind.PIRATE)
    for _ in range(3):
        repository.record_mission_failure(pirate, exit_code=1, limit=3, now_utc=NOW)

    again = repository.record_mission_failure(
        pirate, exit_code=1, limit=3, now_utc=NOW + timedelta(minutes=1)
    )

    assert again.disabled_now is False
    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.backoff_rounds == 1
    assert row.retry_after_utc == NOW + timedelta(minutes=15)


def test_resuming_clears_the_alarm_but_keeps_the_round_and_the_streak(repository) -> None:  # type: ignore[no-untyped-def]
    """放回来时清闹钟、**留轮次、留连续失败计数**。三样各有各的理由。

    - 闹钟：这一次停用结束了，留着就是给一个已经不存在的停用留着闹钟。
    - 轮次：留着才有「恢复之后又崩就落到下一档」。它的归零点只有一个——
      任何任务跑出退出码 0。
    - 连续失败计数：它此刻正等于上限，留着意味着放回来之后**再崩一次就重新
      停用**，也就是一轮只白试一次。清掉的话每轮要白试三次，「一天最多 24 次、
      每次约 1 秒」那笔账当场翻三倍。
    """
    repository.ensure_mission_rows(now_utc=NOW)
    pirate = task_id(repository, MissionKind.PIRATE)
    for _ in range(3):
        repository.record_mission_failure(pirate, exit_code=1, limit=3, now_utc=NOW)

    from evo_helper.domain.scheduler import DisabledRecovery

    assert repository.resume_mission_task(pirate, recovery=DisabledRecovery.BACKOFF) is True

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.disabled_reason is None
    assert row.disabled_recovery is None
    assert row.retry_after_utc is None
    assert row.backoff_rounds == 1
    assert row.consecutive_failures == 3


def test_a_user_edit_wipes_every_trace_of_the_backoff(repository) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **用户动手改一次任务，退避状态一个字都不许留下。**

    页面上勾掉复选框走的就是这里（`enabled=False`）。走完之后 `disabled_reason`
    是 NULL，而自愈判据只认「`disabled_reason IS NOT NULL`」——这是「用户自己
    关掉的任务永远不被自动打开」那条性质的第一道闸。

    闹钟同样要清掉：留着一个过了期的 `retry_after_utc`，等于给一个已经不存在的
    停用留着闹钟，判据哪天松一点就会照它把任务打开。
    """
    repository.ensure_mission_rows(now_utc=NOW)
    pirate = task_id(repository, MissionKind.PIRATE)
    for _ in range(3):
        repository.record_mission_failure(pirate, exit_code=1, limit=3, now_utc=NOW)

    repository.update_mission_task(pirate, enabled=False)

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.enabled is False
    assert row.disabled_reason is None
    assert row.disabled_recovery is None
    assert row.retry_after_utc is None
    assert row.backoff_rounds == 0
    assert row.consecutive_failures == 0


def test_clearing_the_rounds_is_global_and_leaves_the_alarm_alone(repository) -> None:  # type: ignore[no-untyped-def]
    """归零按**全库**清，而且只清轮次。

    按全库清：证明环境好了的那一轮可能是另一条链路跑的，而它们共用同一个游戏
    窗口、同一只鼠标（同 `MAX_ENVIRONMENT_EXEMPTIONS` 的归零）。

    只清轮次：把闹钟也清掉，就等于「另一条链路跑通 → 立刻把还在冷却里的任务全
    放出来」，那是一条**没人要求过**的旁路——它绕开整条退避判据，一条真坏了的
    链路会跟着每一次别人的成功被反复起起来。到点自己回来就够。
    """
    repository.ensure_mission_rows(now_utc=NOW)
    pirate = task_id(repository, MissionKind.PIRATE)
    for _ in range(3):
        repository.record_mission_failure(pirate, exit_code=1, limit=3, now_utc=NOW)

    assert repository.clear_backoff_rounds() == 1

    row = next(item for item in repository.mission_tasks() if item.kind == "PIRATE")
    assert row.backoff_rounds == 0
    assert row.retry_after_utc == NOW + timedelta(minutes=15)
    assert row.disabled_reason is not None


def test_two_tasks_of_one_kind_have_their_own_last_start(repository) -> None:  # type: ignore[no-untyped-def]
    """**同一链路的两个任务各记各的启动时刻。**

    冷却按启动算，而 `last_mission_starts()` 是它唯一的事实来源。按 `kind` 分组
    的话，两个 bot 任务会共用一个时刻：主星那个刚跑完，2 号星那个就得干等五分钟
    ——而它俩占的根本不是同一份航线。

    ⚠️ 两个时刻**故意相差很远**（5 分钟 vs 3 小时）：只差几秒的话，分组键改错了
    也可能碰巧取到同一个值。
    """
    from evo_helper.domain.models import Coordinate

    repository.ensure_mission_rows(now_utc=NOW)
    main = task_id(repository, MissionKind.BOT)
    second = repository.create_mission_task(
        MissionKind.BOT,
        name="2 号星",
        priority=5,
        params_json="{}",
        origin=Coordinate(9, 250, 8),
        fleet_lines=2,
        now_utc=NOW,
    )
    for wanted, minutes in ((main, 5), (second, 180)):
        repository.begin_mission_run(
            MissionKind.BOT,
            task_id=wanted,
            command=["python"],
            pid=None,
            started_at_utc=NOW - timedelta(minutes=minutes),
            log_path="var/logs/mission-bot.log",
        )

    starts = repository.last_mission_starts()

    assert starts[main] == NOW - timedelta(minutes=5)
    assert starts[second] == NOW - timedelta(hours=3)


def test_failures_are_counted_per_task_not_per_kind(repository) -> None:  # type: ignore[no-untyped-def]
    """**多个 BOT 任务各自独立记账。**

    按 kind 记的话，主星那个任务崩三次会把 2 号星那个一起停用——它俩跑的是不同
    的范围、不同的出发星球，没有任何理由共担一份失败计数。
    """
    from evo_helper.domain.models import Coordinate

    repository.ensure_mission_rows(now_utc=NOW)
    main = task_id(repository, MissionKind.BOT)
    second = repository.create_mission_task(
        MissionKind.BOT,
        name="2 号星",
        priority=5,
        params_json="{}",
        origin=Coordinate(9, 250, 8),
        fleet_lines=2,
        now_utc=NOW,
    )

    for _ in range(3):
        repository.record_mission_failure(main, exit_code=1, limit=3, now_utc=NOW)

    rows = {row.id: row for row in repository.mission_tasks()}
    assert rows[main].disabled_reason is not None
    assert rows[second].consecutive_failures == 0
    assert rows[second].disabled_reason is None


def test_clearing_failures_on_a_deleted_task_is_not_an_error(repository) -> None:  # type: ignore[no-untyped-def]
    """「多个一起倒」那条豁免会回头清同一阵里每个任务的计数。

    其中一个可能已经被用户删掉了——为它抛异常会让整个调度循环停摆，而它想做的
    事（把记错的账清掉）在那一行上本来就没有意义。
    """
    from evo_helper.domain.models import Coordinate

    repository.ensure_mission_rows(now_utc=NOW)
    gone = repository.create_mission_task(
        MissionKind.BOT,
        name="待删",
        priority=6,
        params_json="{}",
        origin=Coordinate(9, 250, 8),
        fleet_lines=2,
        now_utc=NOW,
    )
    repository.delete_mission_task(gone)

    repository.clear_mission_failures(gone)


def test_a_deleted_task_keeps_its_run_history(repository) -> None:  # type: ignore[no-untyped-def]
    """删任务只删配置，**不删账**。

    `mission_runs` 里那些行回答的是「昨晚那几轮是谁跑的」，跟着任务一起删掉，
    事后就再也说不清了。
    """
    from evo_helper.domain.models import Coordinate

    repository.ensure_mission_rows(now_utc=NOW)
    doomed = repository.create_mission_task(
        MissionKind.BOT,
        name="待删",
        priority=7,
        params_json="{}",
        origin=Coordinate(9, 250, 8),
        fleet_lines=2,
        now_utc=NOW,
    )
    repository.begin_mission_run(
        MissionKind.BOT,
        task_id=doomed,
        command=["python"],
        pid=None,
        started_at_utc=NOW,
        log_path="var/logs/mission-bot.log",
    )

    repository.delete_mission_task(doomed)

    assert [row.task_id for row in repository.mission_runs(limit=10)] == [doomed]
