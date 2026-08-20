"""军力榜扫描的**任务级冷却**：两轮扫描之间至少隔 C 小时，从**上一轮开始**算起。

用户口径（2026-08-20）：「比如在周四，我会把 bot 攻击的军力范围选择为 6 小时。
但是我又不希望太多的扫描打断派出攻击。所以我会设定扫描间隔为 2 小时。**当新的扫描
发起时，检查上次开始扫描的时候是否大于 2 小时。** 当周一时，我会将军力范围选择为
2 小时，扫描冷却为 1 小时，这样尽快的轮转。」

这个文件守的是五件事，每一件都对应一种「改坏了也全绿」的实现：

1. **起算点是「上一轮开始」，不是「上一轮结束」。** 按结束算不会有任何一处报错，
   它只会让实际节奏悄悄变成 `C + 一轮时长`，而页面上那个数字还写着 C。
   这一条在纯判据这一层只量得到一半（这里只有「开始时刻」这一个事实），
   另一半——真的跑一轮长活再看它算的是哪一头——在
   `tests/integration/application/test_ranking_scan_cooldown.py` 里。
2. **边界是「满 C 就放行」**（`>=`），与 `after_schedule_window` 同向。
3. **安全阀：窗口内候选低于门限时，冷却立刻让路。** 这一条最要紧——没有它，
   一轮失败的扫描会把池子饿空，选靶只好放弃窗口、回落到上一周期的陈旧读数，
   而周一恰恰是最不能那么干的一天。
4. **没配 = 不施加任何冷却**，行为与加这个旋钮之前逐字相同（不许有代码默认值）。
5. **被挡住时页面要自己报家门**，不能掉到兜底那句「等航线」上——军力榜不派遣。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import (
    Action,
    MilitaryWindowPool,
    MissionKind,
    ScanCooldown,
    SchedulerFacts,
    TaskFacts,
    TaskSnapshot,
    TaskStatus,
    decide,
    has_work,
    scan_cooldown_verdict,
    status_of,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
HOME = Coordinate(2, 137, 18)
DWELL = timedelta(seconds=60)

#: 周四那一套：窗口 6 小时（这一层看不见）、扫描间隔 2 小时。
COOLDOWN = timedelta(hours=2)

#: 窗口内还有货：`in_window >= floor`，安全阀**不**该开。
HEALTHY = MilitaryWindowPool(in_window=120, floor=100)

#: 窗口内见底：`in_window < floor`，安全阀该开。
STARVED = MilitaryWindowPool(in_window=99, floor=100)


def task(
    kind: MissionKind = MissionKind.RANKING, *, task_id: int = 1, **overrides: Any
) -> TaskSnapshot:
    base = TaskSnapshot(
        task_id=task_id,
        kind=kind,
        name="",
        enabled=True,
        priority=0,
        origin=HOME,
        fleet_lines=6,
    )
    return replace(base, **overrides)


def facts(
    *,
    started_ago: timedelta | None = None,
    pool: MilitaryWindowPool | None = HEALTHY,
    now: datetime = NOW,
    task_id: int = 1,
) -> SchedulerFacts:
    """一份「除了扫描间隔之外什么都不挡」的事实。

    军力榜是填空隙的那一种：不派遣、没有完成态，`has_work` 里除了定时窗口、
    扫描间隔和崩溃冷却之外没有别的闸门。所以任何一处不动都只能是这一道造成的。
    """
    return SchedulerFacts(
        now_utc=now,
        military_window=pool,
        per_task={
            task_id: TaskFacts(
                last_started_at_utc=None if started_ago is None else now - started_ago
            )
        },
    )


# -- 没配 = 逐字保持原来的行为 --------------------------------------------------


def test_an_unconfigured_cooldown_never_blocks_anything() -> None:
    """留空 = 不限。**这一档是「加这个旋钮之前」的行为，必须一个字都不差。**

    ⚠️ 最容易做的错事是「顺手补一个默认值」（看起来像是漏了）。补上之后症状是
    静默的：扫描按一个凭空的间隔变稀，一夜少扫两轮，而页面上那个框还空着。
    """
    verdict = scan_cooldown_verdict(task(), facts(started_ago=timedelta(minutes=1)))

    assert verdict.state is ScanCooldown.OFF
    assert verdict.cooldown is None
    assert not verdict.blocks
    assert has_work(task(), facts(started_ago=timedelta(minutes=1)))


def test_a_task_that_never_ran_is_not_held_back() -> None:
    """从没跑过就没有「上一轮」可言，谈不上间隔。

    判成「挡住」的话，一个刚配好扫描间隔的新任务会永远起不来——它需要跑一轮
    才有起算点，而起算点需要它跑一轮。
    """
    verdict = scan_cooldown_verdict(task(scan_cooldown=COOLDOWN), facts(started_ago=None))

    assert verdict.state is ScanCooldown.OFF
    assert has_work(task(scan_cooldown=COOLDOWN), facts(started_ago=None))


# -- 判据本身：从「上一轮开始」算，满 C 放行 ------------------------------------


def test_a_scan_started_less_than_the_cooldown_ago_is_held_back() -> None:
    at = task(scan_cooldown=COOLDOWN)
    now = facts(started_ago=timedelta(minutes=90))

    verdict = scan_cooldown_verdict(at, now)

    assert verdict.state is ScanCooldown.BLOCKING
    assert verdict.blocks
    assert not has_work(at, now)


@pytest.mark.parametrize(
    "started_ago",
    [COOLDOWN, COOLDOWN + timedelta(seconds=1), timedelta(days=3)],
    ids=["正好满两小时", "多一秒", "早就过了"],
)
def test_the_cooldown_lets_go_the_moment_it_is_full(started_ago: timedelta) -> None:
    """⚠️ **边界取「满了就放行」**（`elapsed >= cooldown`），区间左闭右开。

    写成 `>` 的话，正好落在整点上的那一 tick 还要再等一秒；而 tick 每秒一次，
    差一秒本身无所谓——真正的问题是这个判据从此和 `after_schedule_window`
    那一条反向，而两者是同一层的两道闸门。反过来写成 `<=` 更糟：那等于把
    冷却整个提前一个 tick 失效，用户配的 2 小时永远兑现不了最后那一秒。
    """
    at = task(scan_cooldown=COOLDOWN)
    now = facts(started_ago=started_ago)

    assert scan_cooldown_verdict(at, now).state is ScanCooldown.ELAPSED
    assert has_work(at, now)


def test_the_verdict_carries_the_numbers_the_log_has_to_print() -> None:
    """判据与日志**同源**：「还差多久」由这里算，调用方不许自己再减一遍。

    各算一份的结果是日志理直气壮地说一个和判据不一样的数，而本仓的规矩是
    「日志说假话比不说更糟」。
    """
    verdict = scan_cooldown_verdict(
        task(scan_cooldown=COOLDOWN), facts(started_ago=timedelta(minutes=90))
    )

    assert verdict.last_started_at_utc == NOW - timedelta(minutes=90)
    assert verdict.elapsed == timedelta(minutes=90)
    assert verdict.remaining == timedelta(minutes=30)
    assert verdict.cooldown == COOLDOWN


# -- 安全阀：冷却不许把自己饿死 --------------------------------------------------


def test_the_cooldown_steps_aside_once_the_window_pool_dips_below_its_floor() -> None:
    """**这一条是整个改动最要紧的一句。**

    周一的配置是窗口 2 小时、间隔 1 小时。若某一轮扫描失败或中途被打断，下一轮又
    被间隔挡住，窗口内候选就会归零 → 选靶第 3 步触发「窗口内不足门限就放弃窗口」
    （`domain.target_order.WINDOW_POOL_FLOOR`）→ 回落到全部读数。而**周一恰恰是
    最不能用上周期读数的一天**：bot 军力每周一 UTC+0 刷新，那一刻全库读数同时作废。

    也就是说，一个本意是「少打断攻击」的旋钮，会在最坏的时刻把攻击喂上一批已经
    作废的数据，而页面上只显示一句听起来很正常的「军力读数已放宽窗口」。
    """
    at = task(scan_cooldown=COOLDOWN)
    now = facts(started_ago=timedelta(minutes=5), pool=STARVED)

    verdict = scan_cooldown_verdict(at, now)

    assert verdict.state is ScanCooldown.OVERRIDDEN
    assert not verdict.blocks
    assert has_work(at, now), "窗口内已经低于门限，冷却必须让路——再挡下去池子就空了"


def test_a_pool_exactly_at_its_floor_still_counts_as_having_stock() -> None:
    """⚠️ **安全阀的口径必须与选靶那一步逐字相同。**

    `choose_by_military` 那一行是 `len(in_window) >= window_floor` 才肯只用窗口内的，
    所以「正好等于门限」是**够用**，安全阀不开。松一格（写成 `<=`）会让冷却在池子
    还够用时白白失效，这个旋钮等于没配；紧一格则会让它在已经不够时继续挡。
    两种走样在页面上都看不出来。
    """
    at = task(scan_cooldown=COOLDOWN)
    now = facts(started_ago=timedelta(minutes=5), pool=MilitaryWindowPool(in_window=100, floor=100))

    assert scan_cooldown_verdict(at, now).state is ScanCooldown.BLOCKING
    assert not has_work(at, now)


def test_no_military_task_at_all_is_not_the_same_as_an_empty_window() -> None:
    """`military_window is None` = **没有军力优先的任务在等这份读数**，不是「窗口空了」。

    当成「空了」的话，一个压根没开军力攻击的账号上，扫描间隔会永远被安全阀顶开
    ——用户填的那个数一次都不生效，而页面上它看起来好好的。
    """
    at = task(scan_cooldown=COOLDOWN)
    now = facts(started_ago=timedelta(minutes=5), pool=None)

    assert scan_cooldown_verdict(at, now).state is ScanCooldown.BLOCKING
    assert not has_work(at, now)


def test_below_floor_is_read_the_same_way_everywhere() -> None:
    assert MilitaryWindowPool(in_window=99, floor=100).below_floor
    assert not MilitaryWindowPool(in_window=100, floor=100).below_floor
    assert not MilitaryWindowPool(in_window=101, floor=100).below_floor


# -- 页面要自己报家门 -----------------------------------------------------------


def test_a_held_back_scan_says_so_instead_of_claiming_to_wait_for_a_line() -> None:
    """被扫描间隔按住时页面必须说「扫描间隔未到」。

    ⚠️ 不报家门的话它会一路掉到 `status_of` 末尾那句兜底的「等航线」上——
    而军力榜**压根不派遣**，那是一句用户照着去调航线数、调完也不会有任何变化的
    假话。2026-08-16 晚上刚为「任务不动而界面不说原因」查过一个小时。
    """
    at = task(scan_cooldown=COOLDOWN)
    now = facts(started_ago=timedelta(minutes=90))

    assert status_of(at, now, running=None) is TaskStatus.SCAN_COOLDOWN


def test_the_scan_cooldown_status_is_not_the_restart_cooldown_status() -> None:
    """两句话听起来是一回事，用户能做的事却完全不同：一个是他自己填的小时数，
    另一个是代码定的几分钟。混成一句的代价是他改了没用的那一个。
    """
    assert TaskStatus.SCAN_COOLDOWN is not TaskStatus.COOLING_DOWN
    assert TaskStatus.SCAN_COOLDOWN.value != TaskStatus.COOLING_DOWN.value


def test_a_released_scan_goes_back_to_ready() -> None:
    at = task(scan_cooldown=COOLDOWN)

    assert status_of(at, facts(started_ago=COOLDOWN), running=None) is TaskStatus.READY
    assert (
        status_of(at, facts(started_ago=timedelta(minutes=5), pool=STARVED), running=None)
        is TaskStatus.READY
    )


# -- 它只挡「开新的一轮」 --------------------------------------------------------


def test_the_cooldown_keeps_the_scheduler_from_starting_a_new_round() -> None:
    at = task(scan_cooldown=COOLDOWN)

    decision = decide([at], facts(started_ago=timedelta(minutes=90)), running=None, min_dwell=DWELL)

    assert decision.action is Action.IDLE


def test_the_cooldown_is_per_task_not_per_kind() -> None:
    """⚠️ **旋钮是任务级的，不许退化成全局。**

    用户按周内相位来回调（周一 1 小时、周四 2 小时），而扫描任务将来可能不止一个。
    退化成全局之后，两条军力榜任务会共用同一个间隔——配了 2 小时的那条一跑完，
    另一条压根没配间隔的也跟着被按住两小时，而它那一行的框是空的。
    """
    held = task(task_id=1, scan_cooldown=COOLDOWN)
    free = task(task_id=2)
    now = SchedulerFacts(
        now_utc=NOW,
        military_window=HEALTHY,
        per_task={
            1: TaskFacts(last_started_at_utc=NOW - timedelta(minutes=5)),
            2: TaskFacts(last_started_at_utc=NOW - timedelta(minutes=5)),
        },
    )

    assert not has_work(held, now)
    assert has_work(free, now)


def test_the_other_chains_never_carry_a_scan_cooldown() -> None:
    """海盗与 bot 攻击身上这一格恒为 `None`——它们的节流是 `RESTART_COOLDOWN`。

    这里量的是判据本身对它们无害：真正保证「不会有人给它们配上」的是
    `application.mission_scheduler.task_snapshot`（只对 RANKING 解析）。
    """
    for kind in (MissionKind.PIRATE, MissionKind.BOT, MissionKind.SCAN):
        assert task(kind).scan_cooldown is None
