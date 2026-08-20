"""军力榜的**任务级扫描间隔**：两轮扫描之间至少隔 C 小时，从**上一轮开始**算起。

用户口径（2026-08-20）：「比如在周四，我会把 bot 攻击的军力范围选择为 6 小时。
但是我又不希望太多的扫描打断派出攻击。所以我会设定扫描间隔为 2 小时。**当新的扫描
发起时，检查上次开始扫描的时候是否大于 2 小时。** 当周一时，我会将军力范围选择为
2 小时，扫描冷却为 1 小时，这样尽快的轮转。」

判据本身（边界、安全阀、页面那句话）在 `tests/unit/domain/test_scan_cooldown.py`
里量；这个文件量的是**接线**——那几处只要接错就全绿、而且症状全是静默的：

1. **参数住在 `mission_tasks.params_json`，而且是任务级的。** 退化成全局
   （`military_attack_config`）之后，两条扫描任务会共用同一个间隔。
2. **起算点真的是「开始」。** 一轮扫描 40 分钟、结束在 25 分钟前，而它开始在
   65 分钟前——间隔配 1 小时，这一轮必须放行。改成从结束算不会有任何一处报错。
3. **安全阀真的读得到窗口内的候选数。** 领域层那一层量的是「给它一个饿着的池子
   会怎样」，这里量的是「池子饿了的时候，`_facts` 有没有把这件事告诉它」。
4. **已经开始的军力批次不许被卡在半途。** 批次交接问的是 bot 任务的 `has_work`，
   军力榜身上这道闸门碰不到它。
5. **挡掉与让路都要留痕，而且要限流。** `has_work` 每 tick 都走这条判据。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import evo_helper.web
from evo_helper.application.mission_scheduler import (
    REPEATED_LOG_WINDOW,
    MissionScheduler,
    task_snapshot,
)
from evo_helper.domain.missions import MissionParamError
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import GAP_FILLERS, MissionKind, TaskStatus, status_of
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.persistent_service import ranking_scan_summary

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import add_bot_target, disable, enable, task, task_id

#: 周二中午。**刻意不取周一**：选靶第 2 步有一条按「本周期起点（周一 00:00 UTC）」
#: 划的线，周一凌晨摆出来的读数会先被那条线整批丢掉，于是「窗口内有几个」这件事
#: 根本量不到（`domain.target_order` 模块头第 2 步）。
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
ORIGIN = Coordinate(2, 137, 18)

#: 周四那一套：扫描间隔 2 小时。
TWO_HOURS = '{"scan_cooldown_hours": 2}'

#: 军力优先、窗口门限 2 个、有效期 6 小时。窗口内够不够由每条用例自己摆。
BY_MILITARY = '{"by_military": true, "top_n": 2, "score_max_age_hours": 6}'


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
    def held(self) -> list[tuple[str, str, dict[str, object]]]:
        return self.of("扫描间隔生效")

    @property
    def released(self) -> list[tuple[str, str, dict[str, object]]]:
        return self.of("扫描间隔让路")

    @property
    def overran(self) -> list[tuple[str, str, dict[str, object]]]:
        return self.of("军力榜这一轮扫描耗时")


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


# -- 夹具 ----------------------------------------------------------------------


def only_ranking(repository: SqlAlchemyRepository, params_json: str = "{}") -> None:
    """只留军力榜这一条能跑，并给它配上这份参数。

    另一条填空隙的（扫描）必须关掉：不关的话它会顶上来把空隙填掉，
    于是「军力榜被挡住了」的断言看到的是扫描那一轮。
    """
    for kind in GAP_FILLERS:
        if kind is not MissionKind.RANKING:
            disable(repository, kind)
    enable(repository, MissionKind.RANKING, params_json=params_json)


def park_the_bot_task(repository: SqlAlchemyRepository) -> None:
    """让军力攻击任务**参与调度但没活干**。

    这是为了把「池子的账」和「谁去跑」这两件事分开量：`_facts` 只给参与调度的
    任务算军力候选池（`_participating`），可只要它有活干，它的优先级就在军力榜
    前面、这一轮就轮不到军力榜。定时窗口正好卡在中间——它挡的是 `has_work`，
    不挡 `_participating`，于是池子照算、活儿没有。
    """
    repository.update_mission_task(
        task_id(repository, MissionKind.BOT),
        enabled=True,
        params_json=BY_MILITARY,
        enabled_until_utc=NOW - timedelta(days=1),
    )


def a_full_window(session_factory) -> None:  # type: ignore[no-untyped-def]
    """窗口内 4 个、门限 2 个：**够用**，安全阀不该开。"""
    for index in range(4):
        add_bot_target(
            session_factory, Coordinate(2, 400 + index, 5), military_score=9_000.0, scanned_at=NOW
        )


def a_starving_window(session_factory) -> None:  # type: ignore[no-untyped-def]
    """窗口内 1 个、门限 2 个：**见底**，安全阀该开。

    ⚠️ 另外两个刻意只旧 8 小时（出了 6 小时窗口、仍在本周期内）。摆成 3 天前的话，
    它们会先被「本周期起点」那条线整批丢掉，`with_readings` 里就只剩 1 个，
    这一组要摆的「窗口内不够、窗口外还有」那个局面根本出不来。
    """
    add_bot_target(session_factory, Coordinate(2, 400, 5), military_score=9_000.0, scanned_at=NOW)
    for index in (1, 2):
        add_bot_target(
            session_factory,
            Coordinate(2, 400 + index, 5),
            military_score=8_000.0,
            scanned_at=NOW - timedelta(hours=8),
        )


def run_one_round(scheduler: MissionScheduler, launcher, clock: Clock, *, minutes: int) -> None:  # type: ignore[no-untyped-def]
    """跑完一轮军力榜：起进程 → 走 `minutes` 分钟 → 干净退出 → 收退出码。"""
    scheduler.start()
    scheduler.tick()
    assert launcher.kinds[-1] is MissionKind.RANKING
    launcher.latest.exit_code = 0
    clock.now = clock.now + timedelta(minutes=minutes)
    scheduler.tick()


# -- 留空 = 不限（与加这个旋钮之前逐字相同）--------------------------------------


def test_an_empty_cooldown_lets_the_scan_come_straight_back(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """没配间隔时，一轮跑完下一轮立刻就能起——这是加这个旋钮之前的行为。

    ⚠️ 军力榜是填空隙的那一种，正常跑完**不吃 `RESTART_COOLDOWN`**
    （`domain.scheduler.cooling_down` 只对它的崩溃计冷却）。所以这里量到的
    「立刻又起了一轮」是原有行为，不是这次改动带来的。
    """
    only_ranking(repository)

    run_one_round(scheduler, launcher, clock, minutes=1)

    assert launcher.kinds == [MissionKind.RANKING, MissionKind.RANKING]


def test_a_configured_cooldown_holds_the_next_round_back(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    only_ranking(repository, TWO_HOURS)

    run_one_round(scheduler, launcher, clock, minutes=1)

    assert launcher.kinds == [MissionKind.RANKING], "配了 2 小时间隔，一分钟后不该再起一轮"


def test_the_next_round_comes_back_once_the_cooldown_is_full(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    only_ranking(repository, TWO_HOURS)

    run_one_round(scheduler, launcher, clock, minutes=120)

    assert launcher.kinds == [MissionKind.RANKING, MissionKind.RANKING]


# -- ⚠️ 起算点是「上一轮开始」，不是「上一轮结束」 --------------------------------


def test_the_cooldown_is_measured_from_the_start_of_the_previous_round(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """一轮扫描 40 分钟；间隔配 1 小时。

    - 从**开始**算：已经过了 65 分钟 → 放行。（用户明确要的这一个。）
    - 从**结束**算：只过了 25 分钟 → 挡住。

    ⚠️ 改成从结束算不会有任何一处报错，它只会让实际节奏悄悄变成
    `C + 一轮时长`，而页面上那个数字还写着 C。用户要的正是「节奏稳定在每 C 小时
    一轮，不被单轮时长拖着漂」。
    """
    only_ranking(repository, '{"scan_cooldown_hours": 1}')
    scheduler.start()
    scheduler.tick()
    launcher.latest.exit_code = 0
    # 12:40 结束这一轮（开始于 12:00）。
    clock.now = NOW + timedelta(minutes=40)
    scheduler.tick()
    assert launcher.kinds == [MissionKind.RANKING], "40 分钟不足 1 小时，这时确实该挡住"

    # 13:05：距开始 65 分钟（够了），距结束 25 分钟（不够）。
    clock.now = NOW + timedelta(minutes=65)
    scheduler.tick()

    assert launcher.kinds == [MissionKind.RANKING, MissionKind.RANKING]


def test_a_round_that_outlives_its_own_cooldown_leaves_a_trace(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, recorded: RecordingLog
) -> None:
    """**边界留痕**：一轮扫描本身就比间隔还长，那这道闸门等于没生效。

    这不是缺陷，是「从开始算」必然的推论；但它必须查得出来——否则用户看着
    「间隔 30 分钟」却发现扫描一轮接一轮，只会以为旋钮坏了。
    实测一轮均值 19.3 分钟、最长 29 分钟，所以日常一条都不该出现。
    """
    only_ranking(repository, '{"scan_cooldown_hours": 0.5}')

    run_one_round(scheduler, launcher, clock, minutes=40)

    assert len(recorded.overran) == 1
    level, message, payload = recorded.overran[0]
    assert level == "WARNING"
    assert "40.0 分钟" in message
    assert payload["duration_minutes"] == 40.0
    assert payload["cooldown_hours"] == 0.5
    assert payload["task_id"] == task_id(repository, MissionKind.RANKING)


def test_a_short_round_says_nothing_about_the_boundary(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, recorded: RecordingLog
) -> None:
    """没超就一个字都不写。每轮都响的告警和不响的一样没用。"""
    only_ranking(repository, TWO_HOURS)

    run_one_round(scheduler, launcher, clock, minutes=19)

    assert recorded.overran == []


# -- ⚠️ 安全阀：冷却不许把自己饿死 ------------------------------------------------


def test_a_starving_window_pool_makes_the_cooldown_step_aside(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, recorded: RecordingLog
) -> None:
    """窗口内只剩 1 个、门限要 2 个 → 间隔立刻让路，该扫就扫。

    没有这一条，一轮失败或被打断的扫描会把池子饿空 → 选靶放弃窗口 → 回落到
    上一周期的陈旧读数，而**周一恰恰是最不能那么干的一天**（全服刚重置，
    上周期的数全部作废）。整段理由在 `domain.scheduler.scan_cooldown_verdict` 上。
    """
    a_starving_window(session_factory)
    only_ranking(repository, TWO_HOURS)
    park_the_bot_task(repository)

    run_one_round(scheduler, launcher, clock, minutes=1)

    assert launcher.kinds == [MissionKind.RANKING, MissionKind.RANKING]
    assert len(recorded.released) == 1, "让路这一刻是最需要事后查的一种，必须留痕"
    level, message, payload = recorded.released[0]
    assert level == "WARNING", "淹在 INFO 里等于没说"
    assert payload["in_window_count"] == 1
    assert payload["window_floor"] == 2
    assert payload["safety_valve_released"] is True
    assert "回落到上一周期的陈旧读数" in message


def test_a_healthy_window_pool_leaves_the_cooldown_in_force(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, recorded: RecordingLog
) -> None:
    """与上一条**只差窗口内的个数**：4 个 ≥ 门限 2 个，间隔照常生效。

    两条必须成对：只留放行那一条的话，把安全阀写成「恒真」也全绿，而那等于
    这个旋钮压根没做出来。
    """
    a_full_window(session_factory)
    only_ranking(repository, TWO_HOURS)
    park_the_bot_task(repository)

    run_one_round(scheduler, launcher, clock, minutes=1)

    assert launcher.kinds == [MissionKind.RANKING]
    assert recorded.released == []
    assert len(recorded.held) == 1
    assert recorded.held[0][2]["in_window_count"] == 4
    assert recorded.held[0][2]["safety_valve_released"] is False


def test_the_valve_reads_the_same_window_the_target_picker_reads(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory
) -> None:
    """安全阀读的那个数，就是选靶第 3 步筛出来的那一批的个数。

    ⚠️ 各算一份的话，安全阀会在「其实还够用」时乱放行（或者反过来），
    而两种走样在页面上都看不出来。
    """
    a_starving_window(session_factory)
    only_ranking(repository, TWO_HOURS)
    park_the_bot_task(repository)
    scheduler.start()
    scheduler.tick()

    pool = scheduler.snapshot().facts.military_window

    assert pool is not None
    assert (pool.in_window, pool.floor) == (1, 2)
    assert pool.below_floor


def test_no_military_task_means_no_valve(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """一个军力优先的任务都没有时，没人等这份读数——间隔照常生效。

    ⚠️ 把「没有任务」当成「窗口空了」的话，一个压根没开军力攻击的账号上，
    这个旋钮会永远被安全阀顶开，而页面上它看起来好好的。
    """
    only_ranking(repository, TWO_HOURS)
    disable(repository, MissionKind.BOT)

    run_one_round(scheduler, launcher, clock, minutes=1)

    assert scheduler.snapshot().facts.military_window is None
    assert launcher.kinds == [MissionKind.RANKING]


# -- ⚠️ 已经开始的军力批次不许被卡在半途 ------------------------------------------


def test_the_cooldown_never_interrupts_a_scan_that_is_already_running(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """间隔只挡「开新的一轮」。正在跑的那一轮一个字都不碰。

    这一层是纯判据、动不了子进程，所以这一条在结构上就成立；量它是为了钉住
    「将来有人想在这里加一句 `stop()`」这条路——半途掐掉的榜单是一批**采了一半**
    的目标，接着按它派攻击就是拿半截数据出击。
    """
    only_ranking(repository, TWO_HOURS)
    scheduler.start()
    scheduler.tick()
    assert scheduler.snapshot().running is not None

    clock.now = NOW + timedelta(minutes=30)
    scheduler.tick()

    running = scheduler.snapshot().running
    assert running is not None, "间隔生效期间正在跑的那一轮必须活着"
    assert running.started_at_utc == NOW
    assert not launcher.latest.terminated


def test_the_attack_batch_hands_over_even_while_the_scan_is_cooling(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory
) -> None:
    """⚠️ **批次交接不许被军力榜身上这道闸门卡住。**

    军力榜为某个 bot 任务采完一批之后，`_military_ranking_batch_task_id` 扣着那一
    批不让别人插队，下一步必须是**那个 bot 任务**真的打出去。而那一步问的是 bot
    的 `has_work`——军力榜的扫描间隔与它无关。

    接错的症状是最坏的一种：榜刚采完（正是数据最新的时刻），攻击却被一个属于
    另一条链路的冷却按住，等到解除时那批读数已经旧了。
    """
    only_ranking(repository, TWO_HOURS)
    enable(repository, MissionKind.BOT, params_json=BY_MILITARY)
    scheduler.start()
    scheduler.tick()
    # 库里一个本周期读数都没有 → bot 没活干 → 空隙归军力榜，批次就此扣下。
    assert launcher.kinds == [MissionKind.RANKING]

    # 这一轮榜单采到了目标（实机上是 runner 自己写的库）。
    a_full_window(session_factory)
    launcher.latest.exit_code = 0
    clock.now = NOW + timedelta(minutes=10)

    scheduler.tick()

    assert launcher.kinds[-1] is MissionKind.BOT, "榜刚采完，攻击必须立刻接上"
    snapshot = scheduler.snapshot()
    ranking_id = task_id(repository, MissionKind.RANKING)
    ranking = next(item for item in snapshot.snapshots if item.task_id == ranking_id)
    assert status_of(ranking, snapshot.facts, running=None) is TaskStatus.SCAN_COOLDOWN, (
        "这一刻军力榜确实还在间隔里——否则上面那条断言什么都没证明"
    )


# -- ⚠️ 旋钮是任务级的，不是全局的 ------------------------------------------------


def test_two_ranking_tasks_keep_their_own_cooldowns(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock
) -> None:
    """两条军力榜任务各有各的间隔。

    ⚠️ 退化成全局（比如挪到 `military_attack_config` 那张表上）之后，配了 2 小时
    的那条一跑完，另一条压根没配间隔的也跟着被按住两小时——而它那一行的框是空的。
    用户按周内相位来回调这个数，而扫描任务将来可能不止一个。
    """
    only_ranking(repository, TWO_HOURS)
    first = task_id(repository, MissionKind.RANKING)
    second = repository.create_mission_task(
        MissionKind.RANKING,
        name="军力榜（勤扫）",
        priority=0,
        params_json="{}",
        origin=None,
        fleet_lines=None,
        now_utc=NOW,
    )
    scheduler.start()
    scheduler.tick()
    launcher.latest.exit_code = 0
    clock.now = NOW + timedelta(minutes=1)
    scheduler.tick()

    snapshot = scheduler.snapshot()
    by_id = {item.task_id: item for item in snapshot.snapshots}
    held = by_id[first]
    free = by_id[second]

    assert held.scan_cooldown == timedelta(hours=2)
    assert free.scan_cooldown is None
    assert status_of(held, snapshot.facts, running=snapshot.running) is TaskStatus.SCAN_COOLDOWN
    assert status_of(free, snapshot.facts, running=snapshot.running) is not TaskStatus.SCAN_COOLDOWN


def test_the_knob_lives_in_the_task_params_not_in_the_global_attack_config(  # type: ignore[no-untyped-def]
    scheduler, repository
) -> None:
    """它落在 `mission_tasks.params_json` 上，`military_attack_config` 一个字都不动。"""
    enable(repository, MissionKind.RANKING, params_json=TWO_HOURS)
    row = task(repository, MissionKind.RANKING)

    assert "scan_cooldown_hours" in row.params_json
    # ⚠️ 那张全局表上**已经**有一个叫 `reconcile_cooldown_minutes` 的旋钮，所以
    # 这里量的不是「表上没有 cooldown 这个词」，而是「扫描间隔这一个不在上面」。
    config = repository.military_attack_config()
    assert not any(field.startswith("scan_cooldown") for field in vars(config))


# -- 日志：挡掉那一刻要留痕，而且要限流 ------------------------------------------


def test_being_held_back_is_written_down_with_the_numbers(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, recorded: RecordingLog
) -> None:
    """挡掉的那一刻要说清「为什么 + 当时看到了什么」。

    少任何一个数，读日志的人都得回去查库才知道该把间隔调成多少——而那正是
    「没人告诉你」的另一种写法。
    """
    a_full_window(session_factory)
    only_ranking(repository, TWO_HOURS)
    park_the_bot_task(repository)

    run_one_round(scheduler, launcher, clock, minutes=30)

    assert len(recorded.held) == 1
    level, message, payload = recorded.held[0]
    assert level == "INFO"
    assert payload["last_started_at_utc"] == NOW.isoformat()
    assert payload["cooldown_hours"] == 2.0
    assert payload["elapsed_minutes"] == 30.0
    assert payload["remaining_minutes"] == 90.0
    assert payload["in_window_count"] == 4
    assert payload["task_id"] == task_id(repository, MissionKind.RANKING)
    assert "还差 1.5 小时" in message


def test_the_held_back_line_is_throttled_instead_of_written_every_tick(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, recorded: RecordingLog
) -> None:
    """⚠️ **限流。** `has_work` 每 tick 都走这条判据，而 `_step` 一个 tick 里会转
    好几圈。不限流的话，一个配了 2 小时间隔的任务能在两小时里写出七千行同一句话
    ——2026-08-18 那一小时的 12,080 行废日志就是这么来的。

    ⚠️ 「还差几分钟」每 tick 都在变，所以它**不能进签名**：进了就等于限流整个失效，
    而库里那几千行里一个新事实都没有。
    """
    a_full_window(session_factory)
    only_ranking(repository, TWO_HOURS)
    park_the_bot_task(repository)
    run_one_round(scheduler, launcher, clock, minutes=1)
    assert len(recorded.held) == 1

    for step in range(1, 20):
        clock.now = NOW + timedelta(minutes=1, seconds=step * 5)
        scheduler.tick()

    assert len(recorded.held) == 1, "一个限流窗口之内只该写一条"

    clock.now = NOW + timedelta(minutes=1) + REPEATED_LOG_WINDOW + timedelta(seconds=1)
    scheduler.tick()

    assert len(recorded.held) == 2, "窗口到了要补一条，并交代被压掉了几次"
    assert recorded.held[1][2]["suppressed_since_last_log"] == 19


def test_the_valve_opening_is_written_the_moment_it_happens(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, recorded: RecordingLog
) -> None:
    """从「挡住」跃迁到「让路」时立刻写，**不被限流窗口压掉**。

    跃迁本身就是要看的那件事：它说明池子刚刚跌破门限，也就是上一轮扫描出了岔子。
    """
    a_full_window(session_factory)
    # 间隔取 12 小时，好让「窗口过期」这件事发生在间隔**之内**——两小时的间隔
    # 会先自己走完，那时量到的是 `ELAPSED`，跟安全阀无关。
    only_ranking(repository, '{"scan_cooldown_hours": 12}')
    park_the_bot_task(repository)
    run_one_round(scheduler, launcher, clock, minutes=1)
    assert len(recorded.held) == 1
    assert recorded.released == []

    # 窗口内那 4 个一起过期（有效期 6 小时），于是窗口内 0 个 < 门限 2 个。
    # 时钟只往前走 7 小时，**仍在本周期内**，所以它们只是出了窗口、没有作废。
    clock.now = NOW + timedelta(hours=7)
    scheduler.tick()

    assert len(recorded.released) == 1
    assert recorded.released[0][2]["in_window_count"] == 0


def test_nothing_is_written_when_the_cooldown_is_not_configured(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, recorded: RecordingLog
) -> None:
    """没配间隔时这两条一个字都不写。"""
    a_full_window(session_factory)
    only_ranking(repository)
    park_the_bot_task(repository)

    run_one_round(scheduler, launcher, clock, minutes=1)

    assert recorded.held == []
    assert recorded.released == []


# -- 拒掉不可能的取值 ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ['{"scan_cooldown_hours": 0}', '{"scan_cooldown_hours": -1}'],
    ids=["零", "负数"],
)
def test_zero_or_negative_is_refused_instead_of_silently_meaning_no_limit(
    scheduler: MissionScheduler, raw: str
) -> None:
    """`0` 不是「不限」。「不限」有一个明明白白的表达方式：把框留空。

    用一个看起来像时长的数字去表达它，只会让下一个读库的人分不清那是
    「用户想不限」还是「用户填错了」。同 `bot_limit` 那个 0。
    """
    with pytest.raises(MissionParamError):
        scheduler.command_for(MissionKind.RANKING, raw, origin=ORIGIN)


def test_a_non_numeric_cooldown_is_refused(scheduler: MissionScheduler) -> None:
    with pytest.raises(MissionParamError):
        scheduler.command_for(
            MissionKind.RANKING, '{"scan_cooldown_hours": "两小时"}', origin=ORIGIN
        )


def test_a_boolean_is_not_a_duration(scheduler: MissionScheduler) -> None:
    """`bool` 是 `int` 的子类：`True` 会被当成 1 小时，而用户敲进去的不是时长。"""
    with pytest.raises(MissionParamError):
        scheduler.command_for(MissionKind.RANKING, '{"scan_cooldown_hours": true}', origin=ORIGIN)


def test_the_console_validates_the_cooldown_with_the_same_ruler_as_the_launcher(
    scheduler: MissionScheduler,
) -> None:
    """页面保存之前那道校验走的就是 `command_for`（`web.persistent_service._validate`）。

    ⚠️ 间隔**不上命令行**，所以最容易漏的就是这里：不量的话，一个填错的值会静默
    落库，而它下一次现身是在**每个 tick** 的 `task_snapshot` 里抛出来——那时错的
    是整个调度循环，不是那一次保存。
    """
    assert "--bot-limit" not in scheduler.command_for(
        MissionKind.RANKING, '{"scan_cooldown_hours": 2}', origin=ORIGIN
    )
    with pytest.raises(MissionParamError):
        scheduler.command_for(MissionKind.RANKING, '{"scan_cooldown_hours": 0}', origin=ORIGIN)


@pytest.mark.parametrize(
    "raw", ["{}", '{"scan_cooldown_hours": ""}', '{"scan_cooldown_hours": null}']
)
def test_an_empty_box_parses_to_no_cooldown_at_all(  # type: ignore[no-untyped-def]
    scheduler, repository, raw: str
) -> None:
    """空框 / 空串 / 显式 null 都是「没配」，**不是某个默认值**。

    ⚠️ 给它写一个代码默认值就分不开「没配」和「恰好配成当前默认」，而这两件事在
    默认值将来被改动时的处置完全相反。理由照抄 `military_attack_config.blind_scrolls`。
    """
    enable(repository, MissionKind.RANKING, params_json=raw)
    row = task(repository, MissionKind.RANKING)

    assert task_snapshot(row, origin=ORIGIN, fleet_lines=6).scan_cooldown is None


def test_the_other_chains_never_pick_up_this_key(scheduler, repository) -> None:  # type: ignore[no-untyped-def]
    """只对 `RANKING` 解析：别的链路的 `params_json` 里不该长出一个不生效的键。"""
    enable(repository, MissionKind.BOT, params_json='{"scan_cooldown_hours": 2, "galaxy": 2}')
    row = task(repository, MissionKind.BOT)

    assert task_snapshot(row, origin=ORIGIN, fleet_lines=6).scan_cooldown is None


# -- 页面上说得出这件事 --------------------------------------------------------


def test_the_missions_page_gives_the_ranking_row_a_cooldown_box() -> None:
    """参数只在库里能存、页面上没地方填，等于这个功能不存在。"""
    page = (Path(evo_helper.web.__file__).parent / "templates" / "missions.html").read_text(
        encoding="utf-8"
    )

    assert "key: 'scan_cooldown_hours'" in page
    assert "留空 = 不限" in page
    # 小时数要填得下 0.5，而数字框默认按 1 取整。
    assert "step: '0.5'" in page


def test_the_console_row_says_that_an_empty_box_means_no_limit() -> None:
    """「留空 = 不限」必须**写在页面上**，不能只写在这份文档里。

    一个空的数字框自己说不出它是什么意思，而这一格比「扫描数量」那一格更需要
    这句话：这个功能上线之前所有任务的这一格都是空的，用户第一次看见它时最该
    确认的就是「空着和以前一样」。
    """
    assert "扫描间隔留空 = 不限" in ranking_scan_summary({})
    assert "扫描间隔留空 = 不限" in ranking_scan_summary({"scan_cooldown_hours": ""})
    summary = ranking_scan_summary({"bot_limit": 30, "scan_cooldown_hours": 2})
    assert "两轮扫描至少隔 2 小时（从上一轮开始算；留空 = 不限）" in summary
    assert "窗口内候选低于门限时会越过这个间隔" in summary
