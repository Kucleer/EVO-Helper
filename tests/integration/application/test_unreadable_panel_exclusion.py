"""面板名读不出的目标不再被下一轮重新挑中。

## 修的是什么

站到目标星球上，行星面板上的归属名有时读不出来
（`vision.scan_reading.PlanetPanel.display_name is None`）。判据只能说「这不是
bot」，于是这一发不派——而**这件事一个字都没落库**，下一轮选靶看到的候选池和上一轮
一模一样，又把同一个挑出来。

生产库 + `system_log` 实测（2026-08-20，近 24 小时）：

    「不是 bot（面板名 None）」        40 次，只涉及 3 个坐标
    「不是 bot」但真读出了名字          0 次
    这 3 个坐标历史上成功派出           0 次

也就是说，这个判据 **100% 是在报识别失败**，从来没真的认出过一个「非 bot」。
典型一轮（从出发到收工 44 秒，一发没派）：

    12:33:10  目标 X（NEEDS_ATTACK）
    12:33:31    X 不是 bot（面板名 None）
    12:33:31    [耗时] X 导航 共 21s（ABSENT，要重试）
    12:33:31    复位画面后重试一次
    12:33:54    X 不是 bot（面板名 None）
    12:33:54  完成：目标 0 个，攻击 0 发

代价是双份的：每撞一次白花 21--44 秒鼠标时间；更贵的是**整轮空手而归**（65 轮里
16 轮，25%），于是 `waiting_for_a_line` 把那颗球压到下一条航线空出为止——实测一次
压了 117 分钟。

⚠️ **这份用例只钉「失败留记录 + 排除得掉」，不碰面板名为什么读不出**（识别层，
根因未知，要实机才查得动），**也不碰 `waiting_for_a_line`**。

## 这份用例钉的六件事

1. **读不出 → 落库**（`note_unreadable_panel`），连续第几次也记下来。
2. **下一轮选靶不再挑中它。**
3. **排除窗口到期后重新可选**——排除是有尽头的，不是把目标永久删掉。
4. **排除排在「花掉航线预算」之前**，和 24 小时、保护期那两条一样。
5. **旋钮留空 = 默认 6 小时**，配了就按配的算，不可能的取值当场拒掉。
6. ⚠️ **「读不出」和「真的不是 bot」必须分开。** 实测目前 100% 是前者，但代码不许
   假设永远如此：读出了名字、只是不以 `bot_` 开头，那是事实变了（该由坐标扫描更新
   `is_bot`），记成识别失败等于往识别层的统计里掺假数据。

⚠️ **这里的坐标全是编的。** 仓库是公开的，真实坐标不进用例夹具。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.missions import ORIGIN, MissionParamError
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.domain.target_order import (
    DEFAULT_UNREADABLE_EXCLUSION,
    UNREADABLE_EXCLUSION_MAX_HOURS,
    ScoredTarget,
    attack_value,
)
from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    current_system_log_sink,
    install_system_log_sink,
    reset_knob_override_memo,
    shutdown_system_log_sink,
)
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools.bot_loop import BotLoop
from evo_helper.tools.pirate_loop import TargetCheck
from evo_helper.vision.scan_reading import PlanetPanel

from .conftest import Clock, make_supervisor

NOW = datetime(2026, 8, 20, 12, 33, tzinfo=UTC)
#: `top_n` 只剩「窗口门限」一个身份，填 2 是把门限压到候选数之下，好让窗口不被放弃。
BOT_TOP_TWO = '{"by_military": true, "top_n": 2}'

#: ⚠️ **编出来的坐标**，不是实机上那三个——仓库是公开的。
UNREADABLE = Coordinate(6, 100, 7)
READABLE = Coordinate(6, 101, 9)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


def _task_id(repository: SqlAlchemyRepository, kind: MissionKind) -> int:
    return next(row.id for row in repository.mission_tasks() if row.kind == kind.value)


def _enable(repository: SqlAlchemyRepository, kind: MissionKind, **fields: object) -> None:
    """启用一条链路。**默认给 2 条航线。**

    理由同 `test_protection_period_exclusion._enable`：`fleet_line_limit` 默认只有
    1 条，而按得分分配只派到航线用完为止。给 1 条的话「排除了谁」和「谁的得分更高」
    结果长得一模一样，把排除整条删掉用例照样绿。
    """
    fields.setdefault("fleet_lines", 2)
    repository.update_mission_task(_task_id(repository, kind), enabled=True, **fields)  # type: ignore[arg-type]


def _only_bot(repository: SqlAlchemyRepository) -> None:
    """把会填空隙的那几种关掉，只留 bot 攻击。"""
    for kind in (MissionKind.SCAN, MissionKind.RANKING, MissionKind.PIRATE):
        repository.update_mission_task(_task_id(repository, kind), enabled=False)


def _add_target(  # type: ignore[no-untyped-def]
    session_factory, coordinate: Coordinate, *, military_score: float
) -> None:
    """放一颗有军力读数的 bot。**分数必须给**：没读数的不参与攻击。"""
    with session_factory() as session:
        session.add(
            orm.BotTargetRow(
                id=uuid4(),
                galaxy=coordinate.galaxy,
                system=coordinate.system,
                position=coordinate.position,
                is_bot=True,
                military_score=military_score,
                military_score_at_utc=NOW,
            )
        )
        session.commit()


def _row(session_factory, coordinate: Coordinate) -> orm.BotTargetRow:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        return session.scalars(
            select(orm.BotTargetRow).where(
                orm.BotTargetRow.galaxy == coordinate.galaxy,
                orm.BotTargetRow.system == coordinate.system,
                orm.BotTargetRow.position == coordinate.position,
            )
        ).one()


def _configure(repository: SqlAlchemyRepository, **knobs: int | None) -> None:
    """写一份全局攻击配置。没提到的旋钮一律留空（= 用默认值）。"""
    repository.replace_military_attack_tiers("[]", **knobs)


def _launched(launcher) -> list[str]:  # type: ignore[no-untyped-def]
    return list(launcher.latest.command) if launcher.spawned else []


# -- ① 读不出 → 落库 ----------------------------------------------------------


def test_an_unreadable_panel_lands_in_the_database(  # type: ignore[no-untyped-def]
    repository, session_factory
) -> None:
    """`note_unreadable_panel` 把读不出的时刻写进 `bot_targets`，并数第几次。

    这是这条修复的地基：在它之前，「读不出」只是 runner 控制台上的一句中文，
    而选靶读的是库表——40 条「不是 bot」一条都没能拦住下一轮。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)

    note = repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW)

    assert note is not None
    assert note.attempts == 1
    assert note.military_score == 9_000.0
    assert _row(session_factory, UNREADABLE).unreadable_seen_at_utc == NOW


def test_repeated_failures_count_up(repository, session_factory) -> None:  # type: ignore[no-untyped-def]
    """**连续第几次**要能数出来——用户要分得开「偶发」和「这个坐标永远读不出」。

    排除窗口一过坐标就回到候选池（那是有意的，见 `DEFAULT_UNREADABLE_EXCLUSION`），
    所以「无可救药」这件事只能从这个数字上看出来，不能靠人去数日志行数。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)

    repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW - timedelta(hours=7))
    second = repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW)

    assert second is not None
    assert second.attempts == 2
    assert _row(session_factory, UNREADABLE).unreadable_attempts == 2


def test_a_readable_panel_resets_the_streak(repository, session_factory) -> None:  # type: ignore[no-untyped-def]
    """读通一次就归零，**连排除标记一起撤掉**。

    没有这一步，一个「第一次读不出、复位重试之后读通了」的坐标会被排除好几个小时
    ——而它明明是能打的。归零同时让这个数保持「连续」的语义：累计数会把「上个月坏过
    三次、现在好了」和「一直坏着」混成同一个数。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW)

    assert repository.clear_unreadable_panel(UNREADABLE) == 1

    row = _row(session_factory, UNREADABLE)
    assert row.unreadable_attempts == 0
    assert row.unreadable_seen_at_utc is None
    assert repository.unreadable_bot_targets_since(NOW - DEFAULT_UNREADABLE_EXCLUSION) == set()


def test_clearing_a_clean_target_reports_that_nothing_changed(  # type: ignore[no-untyped-def]
    repository, session_factory
) -> None:
    """本来就没标记时返回 0——调用方据此**只在状态真的变了时才写日志**。

    每次读通都写一条的话，每轮每个目标一条纯噪音，而真正的状态跃迁会被埋掉。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)

    assert repository.clear_unreadable_panel(UNREADABLE) == 0


def test_a_coordinate_with_no_row_reports_that_it_was_not_recorded(  # type: ignore[no-untyped-def]
    repository,
) -> None:
    """`bot_targets` 里没有这一行时**返回 None，而不是插一行**。

    这条链路上的坐标也可能是海盗位（1--4 号位）。凭一次「读不出」就插一行，等于用
    一个我们**解释不了的判据**去断言「这坐标是个 bot 目标」。返回 None 让调用方能把
    话说清楚——runner 那侧据此写的是「没能记下来」而不是假装记上了。
    """
    assert repository.note_unreadable_panel(Coordinate(6, 100, 2), seen_at_utc=NOW) is None


def test_only_failures_inside_the_window_come_back(  # type: ignore[no-untyped-def]
    repository, session_factory
) -> None:
    """`unreadable_bot_targets_since` 按时刻划线，与 `protected_bot_targets_since` 同形。"""
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    _add_target(session_factory, READABLE, military_score=8_000.0)
    repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW - timedelta(hours=1))
    repository.note_unreadable_panel(READABLE, seen_at_utc=NOW - timedelta(hours=7))

    assert repository.unreadable_bot_targets_since(NOW - timedelta(hours=6)) == {UNREADABLE}


# -- ② 下一轮选靶不再挑中它 ----------------------------------------------------


def test_the_next_round_no_longer_picks_an_unreadable_target(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """**这一条就是那 25% 的空轮。**

    上一轮读不出的那个不再进候选池，顺位让给下一个——而不是又一轮「导航过去、
    读不出、复位重试、还是读不出、收工 0 发」，然后把那颗球的航线压住一两个小时。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    _add_target(session_factory, READABLE, military_score=8_000.0)
    repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW - timedelta(minutes=12))
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    command = _launched(launcher)
    assert any(part.startswith("6:101:9") for part in command), "读得出的那个该照打"
    assert not any(part.startswith("6:100:7") for part in command), (
        "读不出的那个又被挑出来了——这正是每轮白烧 21--44 秒、四分之一的轮次空手而归的那个循环"
    )


def test_an_unreadable_panel_is_the_only_reason_it_dropped_out(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """同一份数据、只差「读没读不出过」，结果必须相反。

    不设这个对照的话，上一条的绿色可能来自任何别的闸门（没读数、超期、本轮走完），
    而那几道闸门自己都有用例——重复守一遍等于什么都没守。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    assert any(part.startswith("6:100:7") for part in _launched(launcher))


# -- ③ 排除窗口到期后重新可选 --------------------------------------------------


def test_a_target_returns_once_the_exclusion_window_expires(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """排除**有尽头**：读不出那一刻起满 6 小时之后它重新进候选池。

    ⚠️ **这是一个刻意的设计选择，不是偷懒。** 面板名为什么读不出，根因至今不明；
    永久拉黑等于凭一个我们解释不了的判据，把一个军力排在最前的目标永久丢掉。
    「它到底是不是无可救药」改由 `unreadable_attempts` 回答——那个数写进库、也写进
    每一条日志。没有这一条，「不再挑中它」这句话可以由「永久删掉它」来满足，
    而那会把候选池一夜一夜地锁小下去，症状和这次修的缺陷一样静默。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    repository.note_unreadable_panel(
        UNREADABLE, seen_at_utc=NOW - DEFAULT_UNREADABLE_EXCLUSION - timedelta(minutes=1)
    )
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    assert any(part.startswith("6:100:7") for part in _launched(launcher)), (
        "排除窗口过了它还进不来——那不是排除，是永久删除"
    )


# -- ④ 排除必须排在「按得分花掉航线预算」之前 ---------------------------------


def test_the_exclusion_runs_before_the_budget_is_spent(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **顺序本身就是判据**，同保护期与 24 小时那两条。

    **只有 1 条航线**，两个候选，而**得分最高的那个正是读不出的那个**：

    - 先排除、再分配（正确）：池 = {读得出的}，那条航线派给它。
    - 先分配、再排除（错）：那条唯一的航线被读不出的那个占掉，排除之后 →
      **一发不派**，而另一个明明能打——正是实机上那 16 轮空轮的形状。

    这不是假想：读不出的那 3 个坐标军力 39,030 / 20,960 / 20,630，**正因为高才排在
    候选池最前**，也才每轮都被重新挑中。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    _add_target(session_factory, READABLE, military_score=8_000.0)
    # 前置条件明写出来：得分高的那个必须正是读不出的那个，否则这条用例什么都没验。
    assert attack_value(ScoredTarget(UNREADABLE, military_score=9_000.0), ORIGIN) > attack_value(
        ScoredTarget(READABLE, military_score=8_000.0), ORIGIN
    )

    repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW - timedelta(minutes=12))
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO, fleet_lines=1)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    command = _launched(launcher)
    assert command, "一发都没派——那条唯一的航线被读不出的目标占掉了，排除跑晚了"
    assert any(part.startswith("6:101:9") for part in command)


# -- ⑤ 旋钮 --------------------------------------------------------------------


def test_an_empty_knob_excludes_for_six_hours(repository, scheduler) -> None:  # type: ignore[no-untyped-def]
    """留空 = 6 小时，**断言的是具体数字**。

    写成「等于那个常量」的自反断言，改了常量用例照样绿，等于什么都没守住。
    6 这个数被三条边夹着（整段在 `DEFAULT_UNREADABLE_EXCLUSION`）：实测那个撞了 24
    次的坐标约每小时被重新挑中一次，所以窗口必须明显大于 1 小时；空轮把航线压住
    1--2 小时，所以也要大于 2 小时；而根因未明，锁一整天赌的是一个没有证据的结论。
    """
    _configure(repository)

    assert scheduler._unreadable_exclusion_window() == timedelta(hours=6)
    assert DEFAULT_UNREADABLE_EXCLUSION == timedelta(hours=6)


def test_a_missing_config_row_also_falls_back_to_the_code_default(scheduler) -> None:  # type: ignore[no-untyped-def]
    """配置行压根没建出来时也走代码默认值，**不是 0、不是抛异常**。

    ⚠️ 「没配」和「配了 0」是两回事。这一档退化成 0 的话排除当场失效，而页面上
    一切正常——正是这条功能要修的缺陷本身。
    """
    assert scheduler._unreadable_exclusion_window() == DEFAULT_UNREADABLE_EXCLUSION


def test_a_configured_knob_reopens_a_target_sooner(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """配成 2 小时，3 小时前读不出的那个就重新可打。**判据落在真实选靶上**，
    不是读回配置本身——读回配置只证明它存下来了，证明不了有谁在用它。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW - timedelta(hours=3))
    _configure(repository, unreadable_exclusion_hours=2)
    _enable(repository, MissionKind.BOT, params_json=BOT_TOP_TWO)
    _only_bot(repository)
    scheduler.start()

    scheduler.tick()

    assert scheduler._unreadable_exclusion_window() == timedelta(hours=2)
    assert any(part.startswith("6:100:7") for part in _launched(launcher))
    # 同一批数据按默认的 6 小时算仍然被排除——证明上面那一发来自配置。
    assert repository.unreadable_bot_targets_since(NOW - DEFAULT_UNREADABLE_EXCLUSION) == {
        UNREADABLE
    }


def test_the_two_exclusion_knobs_are_independent(  # type: ignore[no-untyped-def]
    scheduler, repository
) -> None:
    """⚠️ **「读不出」和「保护期」是两个旋钮，改一个不许动另一个。**

    合成一个的话，用户为了治其中一个而调的数会同时改掉另一个，而两者的成因完全不同：
    保护期是**读得懂的事实**（游戏弹窗明说了），「读不出」是**根因还没查清的现象**。
    """
    _configure(repository, unreadable_exclusion_hours=3)

    assert scheduler._unreadable_exclusion_window() == timedelta(hours=3)
    assert scheduler._protection_exclusion_window() == timedelta(hours=8)


@pytest.mark.parametrize("value", [0, -1, "2.5", True, "六小时"])
def test_impossible_exclusion_windows_are_refused(
    scheduler: MissionScheduler, value: object
) -> None:
    """0 等于取消排除——那正是这条功能要修的缺陷本身，所以当场拒掉，
    而不是当成「最激进的那一档」。
    """
    with pytest.raises(MissionParamError):
        scheduler.validate_unreadable_exclusion_hours(value)


def test_the_exclusion_window_stops_at_one_day(scheduler: MissionScheduler) -> None:
    """越过一天就和 `bot_revisit_hours` 争同一件事；而且根因至今没查清，
    按它把一个高军力目标锁掉一整天，赌的是一个还没有证据的结论。
    """
    assert UNREADABLE_EXCLUSION_MAX_HOURS == 24
    assert scheduler.validate_unreadable_exclusion_hours(24) == 24
    with pytest.raises(MissionParamError):
        scheduler.validate_unreadable_exclusion_hours(25)


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_means_follow_the_default_not_zero(
    scheduler: MissionScheduler, blank: object
) -> None:
    """⚠️ **「没配」和「配了 0」是两回事。** 空串被读成 0 的话会撞上下界被拒，
    而那不是用户按下「保存」时的意思。
    """
    assert scheduler.validate_unreadable_exclusion_hours(blank) is None


# -- ⑥ runner 那一侧：分清两种「不是 bot」，并且每一次都留痕 --------------------


class _Collector:
    def __init__(self) -> None:
        self.records: list[SystemLogRecord] = []

    def __call__(self, batch) -> None:  # type: ignore[no-untyped-def]
        self.records.extend(batch)


@pytest.fixture
def collector() -> Iterator[_Collector]:
    """装一个假出口接住 runner 写的那几条。

    ⚠️ 每次都清一遍 `record_knob_override` 的去重账本：它按进程记「这个取值已经写过
    了」，不清的话用例之间会互相污染。
    """
    reset_knob_override_memo()
    sink = _Collector()
    install_system_log_sink(SystemLogSink(sink, flush_interval_s=0.01), context=SystemLogContext())
    try:
        yield sink
    finally:
        shutdown_system_log_sink()
        reset_knob_override_memo()


def _flush() -> None:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


def _events(collector: _Collector, name: str) -> list[dict[str, Any]]:
    """按 `payload_json.event` 挑出来的那几条。

    ⚠️ **不按正文里的中文挑。** `say()` 把控制台那一行也双写进 `system_log`
    （见 `tools.scan_coordinates.say`），所以正文 `LIKE` 会把控制台那份一起数进来
    ——而这几条正是要能一条一条数出来的东西。`event` 这个键就是为此存在的，
    用户想统计「识别失败几次」时用的也是它。
    """
    _flush()
    found = []
    for item in collector.records:
        payload = json.loads(item.payload_json or "{}")
        if payload.get("event") == name:
            found.append({"message": item.message, "level": item.level, **payload})
    return found


def _panel(*, owner: str | None) -> PlanetPanel:
    """一次面板读数。`owner=None` / 空串 = 名字没读出来（`display_name is None`）。"""
    return PlanetPanel(layout="owned", coordinate_text="6:100:7", owner=owner)


def _runner(repository: SqlAlchemyRepository, panel: PlanetPanel) -> Any:
    """一个只装了「善后」所需零件的 `BotLoop`。

    **不碰驱动、不碰 OCR、不动鼠标**：这几条问的是「认不出之后往库里和日志里写了
    什么」，识别本身另有用例（`tests/unit/tools/test_bot_loop_coord_retry.py`）。
    """
    loop = BotLoop.__new__(BotLoop)
    loop._last_panel = {UNREADABLE: panel}  # type: ignore[attr-defined]
    loop._ensure_run = lambda: (repository, uuid4())  # type: ignore[assignment, method-assign]
    return loop


def test_an_unreadable_panel_is_recorded_and_logged(  # type: ignore[no-untyped-def]
    repository, session_factory, collector: _Collector
) -> None:
    """读不出的那一刻**必须留痕**，而且要能事后统计。

    日志里要说清「为什么 + 当时看到了什么 + 排除到什么时候」；`payload` 里要有坐标、
    连续第几次、排除截止时刻和军力值——军力值是排障的第一个问题（「这个白跑的目标
    值不值得救」）。

    ⚠️ **不限流。** 一轮里每个目标最多走到这里一次，而每一次都对应 21--44 秒白烧掉
    的鼠标时间外加一次可能的空轮。限流管的是每 tick 都可能重复的那一档。
    """
    _add_target(session_factory, UNREADABLE, military_score=39_030.0)
    _configure(repository)
    loop = _runner(repository, _panel(owner=""))

    loop._note_check_failure(UNREADABLE, TargetCheck.ABSENT)

    assert _row(session_factory, UNREADABLE).unreadable_attempts == 1
    traces = _events(collector, "unreadable_panel")
    assert len(traces) == 1
    payload = traces[0]
    assert "连续第 1 次" in payload["message"]
    assert payload["coordinate"] == "6:100:7"
    assert payload["attempts"] == 1
    assert payload["military_score"] == 39_030.0
    assert payload["recorded"] is True
    excluded_until = datetime.fromisoformat(payload["excluded_until_utc"])
    seen_at = datetime.fromisoformat(payload["seen_at_utc"])
    assert excluded_until - seen_at == DEFAULT_UNREADABLE_EXCLUSION
    # 「当时看到了什么」也要留下：`display_name is None` 有好几种长相，
    # 分开它们是查根因的第一步，而根因这次刻意没修。
    assert payload["panel_layout"] == "owned"
    assert payload["panel_coordinate_text"] == "6:100:7"


def test_a_readable_name_that_is_not_a_bot_is_not_counted_as_a_failure(  # type: ignore[no-untyped-def]
    repository, session_factory, collector: _Collector
) -> None:
    """⚠️ **误伤这一条：真的不是 bot，绝不记进「读不出」。**

    实测目前 100% 是识别失败（读出名字的 0 次），**但代码不许假设永远如此**。
    名字读出来了、只是不以 `bot_` 开头，那是**事实变了**（这一位现在住着别人），
    该由坐标扫描去更新 `bot_targets.is_bot`。记成识别失败有两重害处：把一个本该
    永久剔除的坐标每 6 小时放回来撞一次，还往识别层的统计里掺假数据——而用户正是
    要靠那个统计判断识别层坏得多厉害。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    _configure(repository)
    loop = _runner(repository, _panel(owner="某个真人"))

    loop._note_check_failure(UNREADABLE, TargetCheck.ABSENT)

    row = _row(session_factory, UNREADABLE)
    assert row.unreadable_attempts == 0
    assert row.unreadable_seen_at_utc is None
    assert _events(collector, "unreadable_panel") == []
    # 但**不是静默**：这一档自己有一条日志，而且明说了它没被记成识别失败。
    told = _events(collector, "not_a_bot")
    assert len(told) == 1
    assert told[0]["recorded_as_unreadable"] is False
    assert told[0]["panel_display_name"] == "某个真人"


def test_a_coordinate_mismatch_is_not_counted_as_a_failure(  # type: ignore[no-untyped-def]
    repository, session_factory, collector: _Collector
) -> None:
    """坐标核对不过是**导航漂了**，和「这一位上住着谁」两码事，不归这里记。"""
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    _configure(repository)
    loop = _runner(repository, _panel(owner=""))

    loop._note_check_failure(UNREADABLE, TargetCheck.MISMATCH)

    assert _row(session_factory, UNREADABLE).unreadable_attempts == 0
    assert _events(collector, "unreadable_panel") == []
    assert _events(collector, "not_a_bot") == []


def test_a_target_with_no_row_says_so_instead_of_pretending(  # type: ignore[no-untyped-def]
    repository, collector: _Collector
) -> None:
    """没能落库时日志要**说实话**：下一轮排除不掉它。

    默不作声的话日志看起来像是记上了，而「日志说假话比不说更糟」——2026-08-17
    那次缺中文语言包整晚空转，正是栽在一句说假话的日志上。
    """
    _configure(repository)
    loop = _runner(repository, _panel(owner=""))

    loop._note_check_failure(UNREADABLE, TargetCheck.ABSENT)

    traces = _events(collector, "unreadable_panel")
    assert len(traces) == 1
    assert "没能记下来" in traces[0]["message"]
    assert traces[0]["recorded"] is False
    assert traces[0]["level"] == "WARNING"


def test_the_log_line_follows_the_configured_knob(  # type: ignore[no-untyped-def]
    repository, session_factory, collector: _Collector
) -> None:
    """日志里那句「排除到什么时候」按**当下这份配置**算，不是写死 6 小时。

    对不上的话，排障的人照着日志去推「它什么时候回来」，怎么算都对不上真实行为。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    _configure(repository, unreadable_exclusion_hours=2)
    loop = _runner(repository, _panel(owner=""))

    loop._note_check_failure(UNREADABLE, TargetCheck.ABSENT)

    (payload,) = _events(collector, "unreadable_panel")
    assert payload["exclusion_hours"] == 2
    excluded_until = datetime.fromisoformat(payload["excluded_until_utc"])
    seen_at = datetime.fromisoformat(payload["seen_at_utc"])
    assert excluded_until - seen_at == timedelta(hours=2)


def test_recovering_after_a_streak_is_logged_once(  # type: ignore[no-untyped-def]
    repository, session_factory, collector: _Collector
) -> None:
    """**状态跃迁**（从「坏着」变成「好了」）写一条；本来就好着的不写。"""
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW)
    loop = _runner(repository, _panel(owner="bot_6_100_7"))

    loop._clear_unreadable(UNREADABLE)
    loop._clear_unreadable(UNREADABLE)

    recovered = _events(collector, "unreadable_cleared")
    assert len(recovered) == 1, "第二次本来就没标记可清，再写一条就是噪音"
    assert recovered[0]["cleared_attempts"] == 1


# -- ⑦ 两条善后都真的挂在 `_attack_once` 上 -----------------------------------


def test_attack_once_records_the_failure(  # type: ignore[no-untyped-def]
    repository, session_factory
) -> None:
    """⚠️ **接线本身要有用例。** 判据写对了但没人调用，症状和没写一模一样：
    每轮照样重新挑中它，而所有单元用例照样全绿。
    """
    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    _configure(repository)
    loop = _runner(repository, _panel(owner=""))
    loop._goto_checked = lambda _c: TargetCheck.ABSENT

    assert loop._attack_once(UNREADABLE) is False

    assert _row(session_factory, UNREADABLE).unreadable_attempts == 1


def test_attack_once_clears_the_streak_when_the_panel_reads(  # type: ignore[no-untyped-def]
    repository, session_factory
) -> None:
    """读通了就归零，同样要真的挂在链路上。

    ⚠️ 这一条同时守着「复位重试之后才读通」那种：`_goto_checked` 判 `CONFIRMED`
    之后就该清干净，否则一次能自愈的抖动会把目标排除掉好几个小时。
    """
    from evo_helper.tools.pirate_loop import Outcome

    _add_target(session_factory, UNREADABLE, military_score=9_000.0)
    repository.note_unreadable_panel(UNREADABLE, seen_at_utc=NOW)
    loop = _runner(repository, _panel(owner="bot_6_100_7"))
    loop._goto_checked = lambda _c: TargetCheck.CONFIRMED
    loop._outcome = Outcome()
    loop._bot = type("_Opts", (), {"attack": False, "presets": None})()

    loop._attack_once(UNREADABLE)

    row = _row(session_factory, UNREADABLE)
    assert row.unreadable_attempts == 0
    assert row.unreadable_seen_at_utc is None
