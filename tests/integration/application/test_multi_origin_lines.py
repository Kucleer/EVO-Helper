"""多出发点的航线账：两道闸、轮换、以及「派不出去」不算错。

这一支从前有三个互相纠缠的缺陷，它们共用同一套账，所以钉在同一个文件里：

1. **全账号那道闸不存在。** `_free_lines_from` 在多出发点那一路把各星球预算
   **相加**，从不拿账号总数校验一次。用户口径（2026-08-18）：「我的总航线数是
   所有星球共享的，在启动加成道具情况下最高是到 9 条」「两者均需要约束」。
2. **第二颗星结构性不可达。** 取的是 `assignments[0].origin`，而分配结果末尾按
   `(origin, distance)` 排、`Coordinate` 是 `order=True` 的 dataclass，
   `4:277:15 < 9:250:8` 恒成立——1 号星只要拿到一个目标就永远排第一。
3. **`has_work` 与实际闸门量的不是同一把尺。** 前者看所有出发点之和，后者只看
   真正要跑的那一颗。2026-08-18 01:00 实机：自动停用 447 次、自动恢复 447 次、
   1368 行日志、bot 链路空转一小时。

判据的核心断言只有一条：**`has_work` 说「能跑」的时候，`_launch` 那道闸一定过得
去**；过不去的那一刻抛的必须是 `MissionIdle`（正常间歇），不是 `NoFreeLineError`
（配置错误 → 自动停用）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.missions import MissionIdle
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_BOT
from evo_helper.domain.scheduler import DisabledRecovery, MissionKind, has_work
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import add_bot_target, dispatch, set_config, task, task_id

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: 两颗出发星球，取的是生产库里那一对（用户 2026-08-18 配的就是这两颗）。
#: ⚠️ **`FIRST < SECOND` 这个大小关系是判据的一部分**：`Coordinate` 是
#: `order=True` 的 dataclass，旧代码按 `(origin, distance)` 排序之后 `FIRST` 恒排
#: 第一。挑一对反过来的坐标，轮换那几条用例就全都白测了。
FIRST = Coordinate(4, 277, 15)
SECOND = Coordinate(9, 250, 8)

#: 军力优先的任务参数。`top_n` 现在只是**窗口门限**（第 3 步的尺子），给得足够大
#: 只有一个后果：窗口更容易被放弃，于是全部有读数的目标都进池——这一整个文件量的
#: 是航线，池子越大越不会有别的东西悄悄限制发数。
BY_MILITARY = '{"by_military": true, "top_n": 50}'


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


# -- 夹具 ----------------------------------------------------------------------


def only_military_bot(repository: SqlAlchemyRepository) -> int:
    """只留军力优先的 bot 攻击一条链路，返回它的 id。

    填空隙的那几种（扫描 / 军力榜）必须关掉：它们永远有活干，留着的话
    「起了谁」「起了几次」这类断言先看到的是它们，而它们与航线毫无关系。
    """
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == MissionKind.BOT.value)
    bot = task_id(repository, MissionKind.BOT)
    repository.update_mission_task(bot, params_json=BY_MILITARY)
    return bot


def configure_origins(
    repository: SqlAlchemyRepository, bot: int, *pairs: tuple[Coordinate, int]
) -> None:
    """给这个任务配上多出发点。**走 `attack_planets` 那张表**，同页面。"""
    resolved = []
    for coordinate, lines in pairs:
        planet = repository.create_attack_planet(coordinate)
        resolved.append((planet.id, lines, True))
    repository.replace_mission_task_origins(bot, tuple(resolved))


def with_lines(  # type: ignore[no-untyped-def]
    repository, session_factory, *pairs: tuple[Coordinate, int], account_limit: int | None = None
) -> int:
    """一条军力 bot 链路 + 各出发点的航线预算 + 全账号上限。返回任务 id。

    `account_limit` 留空 = 走代码里的默认值（9），也就是「用户还没在攻击配置页上
    填过」。全局档位一并配好——没有档位时选靶会走 `BOT_ATTACK_PRESET` 回落，
    那与这里要量的东西无关，但配上更接近实机。
    """
    set_config(session_factory, fleet_line_limit=6, reserved_lines=0)
    bot = only_military_bot(repository)
    configure_origins(repository, bot, *pairs)
    repository.replace_military_attack_tiers(
        '[{"min_score": 0, "preset": "AAA"}]', account_line_limit=account_limit
    )
    return bot


def target_near(  # type: ignore[no-untyped-def]
    session_factory, origin: Coordinate, *, offset: int, score: float
) -> Coordinate:
    """在这颗出发星球边上放一个已记录、且有军力读数的 bot。

    ⚠️ **位次必须避开 1–4**：那四个是游戏固定生成的海盗，`is_bot_coordinate`
    会把它们整个剔掉，于是目标池是空的、任务被判成「没活干」而不是「等航线」。
    ⚠️ **军力读数不能缺**：2026-08-18 起从没上过榜的目标根本不参与攻击。
    """
    coordinate = Coordinate(origin.galaxy, origin.system, 5 + offset)
    add_bot_target(session_factory, coordinate, military_score=score, scanned_at=NOW)
    return coordinate


def occupy(  # type: ignore[no-untyped-def]
    repository, run_id, origin: Coordinate, count: int, *, ago: timedelta | None = None
) -> None:
    """从这颗星球派 `count` 发还没回来的舰队，把它的航线占住。

    默认 10 分钟前派出、飞行 25 分钟（往返 50 分钟），所以在 `NOW + 40 分钟`
    之前它们一直占着。目标位次从 10 起、逐发递增：同一坐标同一周期只记得下一发
    （唯一约束）。

    `ago` 传了就**不给飞行时间**：那一档按 `UNKNOWN_LINE_HOLD`（90 分钟）算仍然
    占着航线。这是「这颗星球满着，而且它上次出兵是很久以前」唯一的构造方式——
    给了飞行时间的话，占位时长和「上次出兵多久以前」就绑死在一起了。
    """
    for index in range(count):
        dispatch(
            repository,
            run_id,
            TARGET_KIND_BOT,
            target=Coordinate(origin.galaxy, origin.system, 10 + index),
            dispatched_at=NOW - (ago if ago is not None else timedelta(minutes=10)),
            flight=None if ago is not None else timedelta(minutes=25),
            origin=origin,
        )


def free_lines(scheduler: MissionScheduler, bot: int) -> int:
    """调度器此刻算出来的空闲航线数（`_facts` 那一份，也就是 `has_work` 看的那个）。"""
    snapshot = scheduler.snapshot()
    return next(
        snapshot.facts.of(item).free_lines for item in snapshot.snapshots if item.task_id == bot
    )


def row_of(repository: SqlAlchemyRepository, bot: int) -> orm.MissionTaskRow:
    return next(row for row in repository.mission_tasks() if row.id == bot)


# -- (a) 两道闸都要生效 ---------------------------------------------------------


def test_a_planet_with_budget_to_spare_is_still_capped_by_the_account_limit(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id
) -> None:
    """**单星预算够，但全账号已满 → 可用为 0。**

    这一条钉的是那道 2026-08-18 之前根本不存在的闸。1 号星配了 6 条、一发都没派；
    2 号星配了 2 条、也没派——按旧口径（各星球各算各的）可用是 6。但全账号上限是
    3，而别处（用户手动派的、海盗链路派的，这里用另一颗星球代表）已经占满 3 条，
    账号那一侧一条不剩。

    ⚠️ 把 `_free_lines_from` 里那个 `min(..., account_free)` 去掉，这条立刻转红。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), (SECOND, 2), account_limit=3)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    # 三发在飞，全记在第三颗星球上——所以两个 bot 出发点各自的预算一条没动。
    occupy(repository, run_id, Coordinate(3, 100, 7), 3)
    scheduler.start()

    assert free_lines(scheduler, bot) == 0, "全账号已经满了，任何一颗星球都不该还有余量"


def test_a_full_planet_has_no_budget_even_when_the_account_has_room(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id
) -> None:
    """**全账号够，但这颗星球满了 → 这颗星球可用为 0。**

    反过来的那一半：账号上限 9、只占了 2 条，宽得很；而 1 号星配的就是 2 条，
    两条全在飞。它此刻一发都派不出去，尽管账号那边还剩 7 条。

    只配这一颗星球，好让 `free_lines`（各出发点里最能派的那一个）只由它决定。
    """
    bot = with_lines(repository, session_factory, (FIRST, 2), account_limit=9)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    occupy(repository, run_id, FIRST, 2)
    scheduler.start()

    assert free_lines(scheduler, bot) == 0, "这颗星球自己的预算已经用光了"


def test_the_two_gates_take_the_smaller_one(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id
) -> None:
    """两道闸取小：账号剩 1 条，而这颗星球自己还剩 5 条 → 可用 1 条。

    只钉「取小」这个算式本身。取大或者取和的话这里会读到 5。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), account_limit=4)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    # 一发记在这颗星球上（它自己剩 5），另外两发记在别处（账号一共占了 3，剩 1）。
    occupy(repository, run_id, FIRST, 1)
    occupy(repository, run_id, Coordinate(3, 100, 7), 2)
    scheduler.start()

    assert free_lines(scheduler, bot) == 1


def test_reserved_lines_come_off_the_account_total(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """`reserved_lines` 是给用户自己留的缓冲，账号那一侧也要扣。

    上限 4、留 3 条给用户、一发未派 → 账号只剩 1 条，尽管星球配了 6 条。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), account_limit=4)
    set_config(session_factory, fleet_line_limit=6, reserved_lines=3)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    scheduler.start()

    assert free_lines(scheduler, bot) == 1


def test_leaving_the_account_limit_blank_applies_no_account_gate_at_all(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """⚠️ **留空 = 不施加账号那道闸，绝不是「回落到某个写死的数」。**

    用户口径（2026-08-18）：「账号的默认权限不应在代码中进行配置，直接用航线限制
    就可以了，因为实际通过科技升级，使用道具，人为占用，都会影响到留给你的航线
    数量」。真实可用航线是浮动的，程序里写死 9 是错的，写死 6 也是错的。

    这里把星球配成 **20 条**——比任何一个「像默认值」的数字（6、9、`fleet_line_limit`
    的 6）都大。留空时可用必须原样是 20：任何一个回落实现都会把它压到那个数上，
    这条随即转红。这正是「下一个人顺手补一个默认值」唯一的守卫。
    """
    bot = with_lines(repository, session_factory, (FIRST, 20), account_limit=None)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    scheduler.start()

    assert free_lines(scheduler, bot) == 20, "留空不该被任何写死的数压住"


def test_a_blank_account_limit_never_holds_a_dispatch_back(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """行为版：留空时两颗星加起来配了 12 条，照样派得出去、不被停用。

    只钉 `free_lines` 那个数的话，一个「留空时把账号余量算成 0」的实现会在别处
    露馅而这里看不出来——那种实现会让整台助手一发不派，而页面上一切正常。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), (SECOND, 6), account_limit=None)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    target_near(session_factory, SECOND, offset=1, score=8_000.0)
    scheduler.start()
    scheduler.tick()

    assert launcher.spawned, "留空 = 不额外限制，这一轮当然派得出去"
    assert row_of(repository, bot).disabled_reason is None


def test_reserved_lines_stay_per_planet_when_the_account_limit_is_blank(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """留空时账号那一侧整个不参与，但**每星那一侧的保留航线照旧生效**。

    这一条防的是「把账号闸做成可选」时顺手把 `reserved_lines` 一起绕过去：
    单出发星球那条路上它一直是按星球扣的（`domain.scheduler.free_lines_for`），
    那个语义一个字都没变。
    """
    set_config(session_factory, fleet_line_limit=6, reserved_lines=2)
    repository.replace_military_attack_tiers("[]", account_line_limit=None)
    for row in repository.mission_tasks():
        repository.update_mission_task(row.id, enabled=row.kind == MissionKind.PIRATE.value)
        if row.kind == MissionKind.PIRATE.value:
            repository.update_mission_task(row.id, params_json='{"radius": 3}', fleet_lines=6)
    pirate = task_id(repository, MissionKind.PIRATE)
    scheduler.start()

    assert free_lines(scheduler, pirate) == 4, "6 条配额减 2 条保留 = 4"


# -- (b) 轮换 ------------------------------------------------------------------


def launched_origin(launcher) -> str:  # type: ignore[no-untyped-def]
    """上一次真的起进程时，命令行上那个 `--origin`。"""
    command = launcher.latest.command
    return command[command.index("--origin") + 1]


def test_the_turn_goes_to_whichever_planet_dispatched_longest_ago(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, run_id
) -> None:
    """**两颗星都能派时，挑上次出兵最久远的那颗。**

    1 号星两小时前刚出过兵，2 号星是四小时前——所以这一轮该轮到 2 号星，
    尽管 `FIRST < SECOND`（旧代码按坐标排，1 号星恒赢）。

    那两发用的是**已经回港**的飞行时间（30 分钟往返，两小时前派出），所以它们只
    留下「上次出兵时刻」这一个事实，不占任何航线——否则这条用例量的就变成航线了。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), (SECOND, 6), account_limit=9)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    target_near(session_factory, SECOND, offset=1, score=8_000.0)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(4, 277, 20),
        dispatched_at=NOW - timedelta(hours=2),
        flight=timedelta(minutes=15),
        origin=FIRST,
    )
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(9, 250, 20),
        dispatched_at=NOW - timedelta(hours=4),
        flight=timedelta(minutes=15),
        origin=SECOND,
    )
    scheduler.start()
    scheduler.tick()

    assert launched_origin(launcher) == str(SECOND), "轮到的应该是等得最久的那颗"
    assert row_of(repository, bot).disabled_reason is None


def test_a_richer_neighbourhood_never_wins_the_turn_by_itself(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, run_id
) -> None:
    """⚠️ **这一条是整次改动的核心：判据绝不能是军力 / 价值。**

    实测（生产库 2026-08-18）：1 号星邻域最高 47,170，2 号星 38,330。这里把这两个
    数原样摆上去——1 号星边上那个目标军力高得多，而且它是**刚刚**出过兵的那颗
    （2 号星四小时没动了）。

    按价值排的话 1 号星恒赢，第二颗星照样饿死，只是换了个判据复发；而且这一次
    连「排序恒定」这条线索都没有了，看起来像是「它就是更该打」。所以这一轮必须
    仍然轮到 2 号星。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), (SECOND, 6), account_limit=9)
    target_near(session_factory, FIRST, offset=0, score=47_170.0)
    target_near(session_factory, SECOND, offset=1, score=38_330.0)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(4, 277, 20),
        dispatched_at=NOW - timedelta(minutes=30),
        flight=timedelta(minutes=5),
        origin=FIRST,
    )
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(9, 250, 20),
        dispatched_at=NOW - timedelta(hours=4),
        flight=timedelta(minutes=5),
        origin=SECOND,
    )
    scheduler.start()
    scheduler.tick()

    assert launched_origin(launcher) == str(SECOND), "军力更高的那颗不该因此抢到这一轮"
    assert row_of(repository, bot).disabled_reason is None


def test_a_planet_that_never_dispatched_goes_first(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, run_id
) -> None:
    """从没出过兵的那颗排最前：它等得比任何人都久。

    这一条同时钉住「`None` 不许被当成最近」——把 `_NEVER` 换成 `datetime.max`
    之类的写法，第二颗星就永远排在最后，而那正是要修的病。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), (SECOND, 6), account_limit=9)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    target_near(session_factory, SECOND, offset=1, score=8_000.0)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(4, 277, 20),
        dispatched_at=NOW - timedelta(hours=6),
        flight=timedelta(minutes=15),
        origin=FIRST,
    )
    scheduler.start()
    scheduler.tick()

    assert launched_origin(launcher) == str(SECOND)
    assert row_of(repository, bot).disabled_reason is None


def test_one_round_runs_exactly_one_planet(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **一轮只跑一颗星球。** runner 一个游戏窗口、一只鼠标，中途换星球会留下
    半组状态（`ensure_origin_planet`）。所以命令行上只能有一个 `--origin`，
    而且下发的目标必须全是那颗星球邻域的。
    """
    with_lines(repository, session_factory, (FIRST, 6), (SECOND, 6), account_limit=9)
    near_first = target_near(session_factory, FIRST, offset=0, score=9_000.0)
    near_second = target_near(session_factory, SECOND, offset=1, score=8_000.0)
    scheduler.start()
    scheduler.tick()

    command = launcher.latest.command
    assert command.count("--origin") == 1
    chosen, other = (
        (near_first, near_second)
        if launched_origin(launcher) == str(FIRST)
        else (near_second, near_first)
    )
    assert any(str(chosen) in part for part in command)
    assert not any(str(other) in part for part in command)


# -- (c) 分不到目标的出发点不参与轮换 --------------------------------------------


def test_a_planet_with_no_targets_this_round_neither_takes_nor_blocks_the_turn(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory, run_id
) -> None:
    """这一轮分不到目标的出发点：不占这一轮，也不把别人卡住。

    2 号星等得最久（从没出过兵），但它的航线**全在飞**，于是它的预算是 0、
    拿不到任何目标。这一轮必须照常轮到 1 号星，而不是「等最久的那颗没目标，
    于是谁都不跑」。

    ⚠️ 这是「预算为 0 的星球拿不到目标」那条结构性保证的正面用例：预算不喂进
    分配那一步的话，2 号星会分到目标、赢得轮换，然后 `max_dispatches` 算出 0。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), (SECOND, 2), account_limit=9)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    target_near(session_factory, SECOND, offset=1, score=8_000.0)
    occupy(repository, run_id, SECOND, 2)
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(4, 277, 20),
        dispatched_at=NOW - timedelta(hours=6),
        flight=timedelta(minutes=15),
        origin=FIRST,
    )
    scheduler.start()
    scheduler.tick()

    assert launched_origin(launcher) == str(FIRST), "2 号星没有航线，不该赢下这一轮"
    assert row_of(repository, bot).disabled_reason is None


# -- (d) 一颗都派不出去 = MissionIdle ------------------------------------------


def test_no_planet_can_dispatch_raises_idle_not_a_config_error(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id
) -> None:
    """两颗星都满了 → `MissionIdle`，**绝不是** `NoFreeLineError`。

    ⚠️ 类型是判据本身，不是修辞：`NoFreeLineError` 继承 `MissionParamError`，
    而 `_launch` 接住后者的动作是 `disable_mission_task`——把一次正常的间歇判成
    配置错误。2026-08-18 01:00 那一小时的 447 次自动停用全是这么来的。
    """
    bot = with_lines(repository, session_factory, (FIRST, 2), (SECOND, 2), account_limit=9)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    target_near(session_factory, SECOND, offset=1, score=8_000.0)
    occupy(repository, run_id, FIRST, 2)
    occupy(repository, run_id, SECOND, 2)

    with pytest.raises(MissionIdle):
        scheduler._military_command(row_of(repository, bot))


def test_an_idle_round_neither_disables_the_task_nor_counts_as_a_failure(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, run_id
) -> None:
    """空手而归的一轮**真的走一遍 `_launch`**：不停用、不记失败、不起进程。

    ⚠️ **这一条必须让判据真的走到 `_launch` 那道闸上。** 航线全满的构造走不到
    ——`has_work` 早在 `free_lines == 0` 那里就把它拦下了，于是「抛的是哪个异常」
    根本没被执行到，把 `MissionIdle` 换回 `NoFreeLineError` 也照样全绿。

    所以这里用的是**航线有、目标却凑不出来**那一档：候选有军力读数（
    `targets_remaining > 0`，`has_work` 放行），但军力上限把它整批挡在池外，
    于是 `_military_assignments` 空手而归。这正是 `MissionIdle` 存在的理由。

    ⚠️ **不只钉 `disabled_reason`。** 只钉那一列的话，「抛 `NoFreeLineError` 但
    恰好又被自动恢复了」也能蒙混过关——而那正是 447 次抖动的样子。所以这里连着
    tick 二十次（跨过一个 `RESTART_COOLDOWN`），要求这中间**一个进程都没起、
    一次失败都没记**。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), (SECOND, 6), account_limit=9)
    # `max_score` 是军力**上限**：军力高于它的一律不进池（`within_max_score`）。
    # 这两颗目标都远高于 100，于是候选池有货、选中的却是空集。
    repository.update_mission_task(
        bot, params_json='{"by_military": true, "top_n": 50, "max_score": 100}'
    )
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    target_near(session_factory, SECOND, offset=1, score=8_000.0)
    scheduler.start()

    for minutes in range(20):
        clock.now = NOW + timedelta(minutes=minutes)
        scheduler.tick()

    row = row_of(repository, bot)
    assert row.disabled_reason is None, "凑不出目标是正常间歇，不该把任务停用"
    assert row.disabled_recovery is None
    assert row.consecutive_failures == 0, "连进程都没起，不该记失败"
    assert launcher.spawned == []


def test_lines_full_on_every_planet_never_starts_a_round(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, run_id
) -> None:
    """两颗星都满着的那一档：`has_work` 就该把它拦下，一轮都不起、也不停用。

    和上一条互补——那条量的是「走到了 `_launch` 之后」，这条量的是「压根没走到」。
    """
    bot = with_lines(repository, session_factory, (FIRST, 2), (SECOND, 2), account_limit=9)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    target_near(session_factory, SECOND, offset=1, score=8_000.0)
    occupy(repository, run_id, FIRST, 2)
    occupy(repository, run_id, SECOND, 2)
    scheduler.start()

    for minutes in range(20):
        clock.now = NOW + timedelta(minutes=minutes)
        scheduler.tick()

    row = row_of(repository, bot)
    assert row.disabled_reason is None, "航线暂时占满是正常间歇，不该把任务停用"
    assert row.disabled_recovery is None
    assert row.consecutive_failures == 0
    assert launcher.spawned == []


# -- (e) has_work 与 _launch 那道闸必须过得去 -----------------------------------


def test_when_has_work_says_yes_the_launch_gate_always_lets_it_through(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id
) -> None:
    """**447 次抖动的直接守卫。**

    构造的正是那一小时的现场：1 号星 2 条全在飞、2 号星还剩 2 条。旧口径下
    `has_work` 看的是两者之和（0 + 2 = 2 > 0）放行，而 `_military_command` 取的是
    第一组（1 号星），`max_dispatches = min(2, 0) = 0` → `NoFreeLineError` → 停用
    → 下一 tick 合计仍是 2 → 恢复 → 再撞。一小时 447 个来回。

    ⚠️ **1 号星必须同时是「满着的」和「上次出兵最久远的」那颗**，否则轮换会顺手
    把它绕过去，这条用例就测不到闸门本身了。所以它那两发占位派遣给的是
    80 分钟前、且**没有飞行时间**的那一档：按 `UNKNOWN_LINE_HOLD`（90 分钟）算
    仍然占着航线，而「上次出兵」是 80 分钟前——2 号星才 10 分钟。

    断言是**两句一起**：`has_work` 说能跑，那 `_military_command` 就必须真的组得
    出一条命令行、而且是从**派得出去**的那颗星球。少了后半句，把
    `_free_lines_from` 改回 `sum`、或者把原样航线数喂给分配那一步，都能全绿。
    """
    bot = with_lines(repository, session_factory, (FIRST, 2), (SECOND, 2), account_limit=9)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    target_near(session_factory, SECOND, offset=1, score=8_000.0)
    occupy(repository, run_id, FIRST, 2, ago=timedelta(minutes=80))
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(9, 250, 30),
        dispatched_at=NOW - timedelta(minutes=10),
        flight=timedelta(minutes=2),
        origin=SECOND,
    )
    scheduler.start()

    snapshot = scheduler.snapshot()
    snap = next(item for item in snapshot.snapshots if item.task_id == bot)
    assert has_work(snap, snapshot.facts), "2 号星还有航线，这一轮当然有活干"

    command = scheduler._military_command(
        row_of(repository, bot), max_dispatches=free_lines(scheduler, bot)
    )
    assert command[command.index("--origin") + 1] == str(SECOND), "只有 2 号星派得出去"


def test_the_task_never_flaps_between_disabled_and_resumed(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, session_factory, run_id
) -> None:
    """整段跑下来一次自动停用都不该发生——这是上一条的行为版。

    同一个现场（1 号星满着且等得最久、2 号星有余量），时钟一格一格往前挪。
    旧实现会在每个 tick 上停用一次、恢复一次；正确的实现只是安静地从 2 号星派兵。
    """
    bot = with_lines(repository, session_factory, (FIRST, 2), (SECOND, 2), account_limit=9)
    target_near(session_factory, FIRST, offset=0, score=9_000.0)
    target_near(session_factory, SECOND, offset=1, score=8_000.0)
    occupy(repository, run_id, FIRST, 2, ago=timedelta(minutes=80))
    dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=Coordinate(9, 250, 30),
        dispatched_at=NOW - timedelta(minutes=10),
        flight=timedelta(minutes=2),
        origin=SECOND,
    )
    scheduler.start()

    for minutes in range(20):
        clock.now = NOW + timedelta(minutes=minutes)
        scheduler.tick()
        assert row_of(repository, bot).disabled_reason is None, f"第 {minutes} 分钟被停用了"
    assert launcher.spawned, "2 号星有航线也有目标，这一段里至少该派出去一轮"
    assert all(str(FIRST) not in " ".join(item.command) for item in launcher.spawned)


# -- 固化记录与页面显示 --------------------------------------------------------


def test_the_freeze_records_every_configured_origin(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """点「开始」抄下来的那份配置必须含**全部**出发点。

    ⚠️ 从前整条固化路径一次都没读过 `mission_task_origins`：它抄的是
    `mission_tasks.origin_*` 与 `mission_tasks.fleet_lines`，而那两样是加多出发点
    之前留下的残值、永远不会被更新。生产实证（2026-08-18）：用户配的是
    `4:277:15=6 线` + `9:250:8=2 线`，记录里却写「出发 4:277:15 · 航线 7」。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), (SECOND, 2), account_limit=9)
    scheduler.start()

    frozen = scheduler.snapshot().frozen_config
    assert frozen is not None
    recorded = next(item for item in frozen.tasks if item.task_id == bot)
    assert recorded.origins is not None
    assert [(item.origin, item.fleet_lines, item.enabled) for item in recorded.origins] == [
        (str(FIRST), 6, True),
        (str(SECOND), 2, True),
    ]


def test_the_freeze_keeps_a_disabled_origin_and_says_so(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """停用的出发点照样入账，并且标着「没勾上」。

    只记启用的那几颗的话，「用户把 2 号星停掉了」和「用户把它删了」在账里长得
    一模一样，而那两件事的善后完全不同。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), (SECOND, 2), account_limit=9)
    planets = {
        (row.galaxy, row.system, row.position): row.id for row in repository.attack_planets()
    }
    repository.replace_mission_task_origins(
        bot,
        (
            (planets[(FIRST.galaxy, FIRST.system, FIRST.position)], 6, True),
            (planets[(SECOND.galaxy, SECOND.system, SECOND.position)], 2, False),
        ),
    )
    scheduler.start()

    frozen = scheduler.snapshot().frozen_config
    assert frozen is not None
    recorded = next(item for item in frozen.tasks if item.task_id == bot)
    assert recorded.origins is not None
    assert [(item.origin, item.enabled) for item in recorded.origins] == [
        (str(FIRST), True),
        (str(SECOND), False),
    ]


def test_a_task_without_multiple_origins_records_an_empty_tuple(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """其余链路记 `()`（「确实没有多出发点」），不是 `None`（「没得比」）。

    两者混为一谈的话，逐条对比要么整段失灵、要么把每一颗星球报成「新增」。
    """
    set_config(session_factory, fleet_line_limit=6, reserved_lines=0)
    scheduler.start()

    frozen = scheduler.snapshot().frozen_config
    assert frozen is not None
    pirate = next(item for item in frozen.tasks if item.kind is MissionKind.PIRATE)
    assert pirate.origins == ()


# -- (g) 停用 / 恢复日志的限流 --------------------------------------------------


class RecordingLog:
    """把 `record_system_log` 的调用记下来。签名与真的那一个一致。"""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.payloads: list[dict[str, object]] = []

    def __call__(self, level, source, message, *, payload=None, logged_at_utc=None):  # type: ignore[no-untyped-def]
        self.messages.append(message)
        self.payloads.append(dict(payload or {}))


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


def test_repeated_auto_disables_are_throttled_into_one_line_per_window(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """同一个任务反复被自动停用时，一个窗口里只落一条，被压掉的次数记在 payload 里。

    ⚠️ **去重挡不住这一档，所以非要限流不可。** 每一下都是真跃迁（库里那两列每次
    都在变），`_disable_task` 里那个「和上一次一样就不写」一条都拦不下来。
    2026-08-18 01:00 实机：447 次停用 + 447 次恢复 = 1368 行。

    ⚠️ **被压掉的次数必须留下来。** 只限流不计数的话，「抖了 447 次」和「老老实实
    停用了一次」在库里长得一模一样，而那两件事的善后完全相反。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), account_limit=9)
    task_row = row_of(repository, bot)
    snapshot = next(item for item in scheduler.snapshot().snapshots if item.task_id == bot)
    recorded.messages.clear()

    # 窗口 120 秒；每 10 秒抖一次，反复停用 / 恢复共 6 轮 = 一分钟。
    for index in range(6):
        clock.now = NOW + timedelta(seconds=10 * index)
        scheduler._disable_task(
            row_of(repository, bot), snapshot, "航线不足", recovery=DisabledRecovery.FREE_LINES
        )
        repository.resume_mission_task(task_row.id, recovery=DisabledRecovery.FREE_LINES)

    assert len(recorded.messages) == 1, f"一个窗口里只该落一条，落了 {recorded.messages}"

    # 越过窗口再抖一次：这一条要落库，而且要说清刚才被压掉了几次。
    clock.now = NOW + timedelta(seconds=200)
    scheduler._disable_task(
        row_of(repository, bot), snapshot, "航线不足", recovery=DisabledRecovery.FREE_LINES
    )

    assert len(recorded.messages) == 2
    assert recorded.payloads[1]["suppressed_since_last_log"] == 5
    assert "5" in recorded.messages[1]


def test_the_throttle_window_is_configurable(  # type: ignore[no-untyped-def]
    scheduler, repository, clock, session_factory, recorded: RecordingLog
) -> None:
    """窗口是**运维旋钮**：填 0 = 每一次跃迁都记，也就是加这道闸之前的行为。

    排障时想看清抖动的真实频率就填 0。这一条同时守住「0 不是『关掉日志』」——
    把 `0` 当成假值回落到默认窗口的写法（`seconds or DEFAULT`）会让这条转红。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), account_limit=9)
    repository.replace_military_attack_tiers(
        '[{"min_score": 0, "preset": "AAA"}]', account_line_limit=9, auto_toggle_log_seconds=0
    )
    snapshot = next(item for item in scheduler.snapshot().snapshots if item.task_id == bot)
    recorded.messages.clear()

    for index in range(3):
        clock.now = NOW + timedelta(seconds=index)
        scheduler._disable_task(
            row_of(repository, bot), snapshot, "航线不足", recovery=DisabledRecovery.FREE_LINES
        )
        repository.resume_mission_task(bot, recovery=DisabledRecovery.FREE_LINES)

    assert len(recorded.messages) == 3, "窗口填 0 就是不限流"


def test_disable_and_resume_are_throttled_separately(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, clock, run_id, session_factory, recorded: RecordingLog
) -> None:
    """「停用」和「恢复」各有各的窗口：两件事合用一个窗口的话，一次真的恢复会被
    刚刚那条停用压掉，于是日志里只剩「它被关了」，看不到「它又回来了」。
    """
    bot = with_lines(repository, session_factory, (FIRST, 6), account_limit=9)
    snapshot = next(item for item in scheduler.snapshot().snapshots if item.task_id == bot)
    recorded.messages.clear()

    scheduler._disable_task(
        row_of(repository, bot), snapshot, "航线不足", recovery=DisabledRecovery.FREE_LINES
    )
    clock.now = NOW + timedelta(seconds=1)
    scheduler._resume_tasks_waiting_for_a_line(clock.now)

    assert len(recorded.messages) == 2
    assert "已被自动停用" in recorded.messages[0]
    assert "自动恢复" in recorded.messages[1]
    assert task(repository, MissionKind.BOT).disabled_reason is None
