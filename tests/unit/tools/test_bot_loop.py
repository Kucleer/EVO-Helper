"""直接 BBB 攻击 → 读战报 → 平局再打：判定这一层的规则。

真正驱动鼠标的部分在 `pirate_loop` 里已经实机跑通，这里只守判定：
**一趟只把每个目标推进一态**、战报在开工那一趟信箱里先读回来、
以及本轮起点绝不以 None 的形式传进仓储。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.domain.bot_round import BOT_ATTACK_PRESET, BotPhase
from evo_helper.domain.models import Coordinate
from evo_helper.tools.bot_loop import BotLoop, BotOptions, parse_round_start, parse_target
from evo_helper.tools.pirate_loop import LoopOptions

TARGET = Coordinate(2, 137, 14)


def test_targets_are_parsed_as_full_coordinates() -> None:
    target = parse_target("2:137:14")

    assert (target.galaxy, target.system, target.position) == (2, 137, 14)


def test_bot_attacks_are_labelled_bot_not_pirate() -> None:
    """BotLoop 继承 PirateLoop 的写库路径，标签必须跟着子类走。

    标错的代价不是「日志难看」：海盗每天 32 次是游戏硬限制，bot 的发数
    混进去会让助手以为配额还没用完，多打的那一发会被强制返回。
    """
    from evo_helper.domain.records import TARGET_KIND_BOT, TARGET_KIND_PIRATE
    from evo_helper.tools.pirate_loop import PirateLoop

    assert PirateLoop.TARGET_KIND == TARGET_KIND_PIRATE
    assert BotLoop.TARGET_KIND == TARGET_KIND_BOT


# -- 一趟只推进一态 ---------------------------------------------------------


class _FakeNavigator:
    def ensure_system_view(self, read_labels: Any) -> bool:
        return True

    def invalidate(self) -> None:
        return None


def _run_with_phases(
    monkeypatch: pytest.MonkeyPatch,
    phases: dict[Coordinate, BotPhase],
    *,
    reconcile_on_start: bool = False,
) -> list[str]:
    """跑一趟 `run()`，返回这一趟按顺序调了哪些动作。"""
    from evo_helper.game import game_window
    from evo_helper.tools.pirate_loop import Outcome

    monkeypatch.setattr(game_window, "ensure_game_window", lambda: None)

    calls: list[str] = []
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(
        targets=tuple(phases), attack=True, reconcile_on_start=reconcile_on_start
    )
    loop._options = LoopOptions(
        systems=(),
        scout=False,
        attack=True,
        reconcile_on_start=reconcile_on_start,
    )
    loop._outcome = Outcome()
    loop._navigator = _FakeNavigator()
    loop._reset_to_known_screen = lambda: None
    # `run()` 归父类管（开工前置 + `RoundExhausted` 收尾），会话巡检要真截屏，
    # 这条测试只关心分态路由，桩掉即可。
    loop._ensure_session = lambda **_k: False
    # 切出发星球要开浮层、OCR、回读派遣面板，那条链路自己有专门的用例
    # （`tests/unit/game/test_planet_list.py` 与 `test_pirate_loop_origin_planet.py`）。
    loop.ensure_origin_planet = lambda: True
    # 只有显式要求时才读一趟信箱；调度器续跑时不准进来。
    loop.reconcile_today = lambda: calls.append("开工那一趟信箱")
    loop._nav_labels = lambda: ""
    loop._phase_of = lambda coordinate: phases[coordinate]
    loop._attack_once = lambda coordinate: calls.append("打一发")
    loop._say_still_waiting = lambda coordinate: calls.append("说还在等战报")

    loop.run()
    return calls


def _run_with_phase(monkeypatch: pytest.MonkeyPatch, phase: BotPhase) -> list[str]:
    return _run_with_phases(monkeypatch, {TARGET: phase})


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (BotPhase.NEEDS_ATTACK, ["打一发"]),
        (BotPhase.AWAITING_ATTACK_REPORT, ["说还在等战报"]),
        (BotPhase.DONE, []),
    ],
)
def test_each_phase_routes_to_exactly_one_action(
    monkeypatch: pytest.MonkeyPatch, phase: BotPhase, expected: list[str]
) -> None:
    """三种态各自该做什么。

    分流一旦失效（比如无条件开打），每个目标每趟都会重新派一发——一趟烧一条航线
    和一次配额，而画面上看不出异常。原先五个态里的两个探路态已经删掉，**不是留成
    死态**：留着的话这张参数表里会有两行永远走不到的分支，读的人会以为那条路还在。

    等待态在这里不进信箱：战报由启动时显式补录统一收（见下一条）。它在这里只剩下
    一句话，而那句话要说准是「还没到点」还是「到点了却没翻到」。

    只有 `DONE` 什么都不做。
    """
    assert _run_with_phase(monkeypatch, phase) == expected


def test_reports_are_read_only_when_explicitly_requested_before_the_phases_are_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式补录排在分态之前，而且整轮只有那一趟。

    用户口径（2026-08-11）：「任务启动先去读战报。」顺序不能反——反了的话，
    这一趟刚读回来的战报要等下一轮才作数，于是「平局就再打」也要晚一整个调度
    周期才动得起来。

    整轮只进一趟信箱，是因为开工那一趟为了数「今天已经打了几发」**本来就要把
    信箱最上面那几屏翻一遍**，顺手把认得出的战报都开了、都入了库。另起一趟收取
    要把「关浮层 → 切地表 → 开信箱 → 慢拖回顶 → 翻页 → 关面板」整套再付一遍
    （实机约 20 秒），还要和它抢那 8 封的开封预算。

    这条也堵住了那个死结的复发形状：收取不再按「本轮在等哪几个目标」的名单走，
    名单也就漏不了态。归属只认报告自己写的目标坐标。
    """
    other = Coordinate(2, 137, 15)
    calls = _run_with_phases(
        monkeypatch,
        {
            TARGET: BotPhase.AWAITING_ATTACK_REPORT,
            other: BotPhase.NEEDS_ATTACK,
        },
        reconcile_on_start=True,
    )

    assert calls == ["开工那一趟信箱", "说还在等战报", "打一发"]
    assert calls.count("开工那一趟信箱") == 1


def test_a_draw_only_costs_one_extra_shot_per_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """平局重打也是**一趟一发**，不在同一趟里把配额一次烧光。

    `_sweep` 每个目标只走一个分支，所以「再打一发」之后这一趟对它就结束了；
    下一发要等下一趟——而那时刚才那一发的战报多半已经回来了。在同一趟里循环
    重打的话，三发会在几十秒内全部飞出去，全部打的是同一支还没被削弱的守军。
    """
    assert _run_with_phase(monkeypatch, BotPhase.NEEDS_ATTACK).count("打一发") == 1


def test_the_look_only_mode_never_touches_the_database() -> None:
    """默认档一次点击都不做，就不该凭空要求一个数据库。

    这一档没有任何派遣，查库问「打过几发」既没有答案也没有意义。
    """
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), attack=False)

    def _forbidden(coordinate: Coordinate) -> tuple[Any, ...]:
        raise AssertionError("只认目标模式不该查库")

    loop._dispatch_facts = _forbidden

    assert loop._phase_of(TARGET) is BotPhase.NEEDS_ATTACK


def test_the_loop_dispatches_with_the_bot_attack_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_attack_once` 交给派遣链路的必须是 BBB 那个标题。

    传错（或者传成父类默认的海盗预设）不会报错：选预设那一步会在预设条上找不到
    它、抛 `PresetNotFound`，这一发就静默地没派出去，日志上只有一句「找不到预设」。
    """
    from evo_helper.tools.pirate_loop import Outcome, TargetCheck

    presets: list[str | None] = []
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), attack=True)
    loop._options = LoopOptions(systems=(), scout=False, attack=True)
    loop._outcome = Outcome()
    loop._goto_checked = lambda _c: TargetCheck.CONFIRMED
    loop.attack = lambda coordinate, *, preset=None: presets.append(preset) or True

    loop._attack_once(TARGET)

    assert presets == [BOT_ATTACK_PRESET]


# -- 本轮从何时算起 ---------------------------------------------------------


def test_a_missing_round_start_falls_back_to_todays_utc_midnight() -> None:
    """**绝不把 None 传给仓储。**

    `since=None` 在查询侧是「不限时间范围」：`bot_dispatch_facts` 会把这个坐标
    历史上每一发都算进本轮，于是它的重打配额永远是满的
    （`domain.bot_round.MAX_ATTACKS_PER_TARGET`），看起来像是「早就打完了」。
    """
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), attack=True)

    start = loop._round_start()

    assert start.tzinfo is not None
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert start.date() == datetime.now(UTC).date()


def test_an_explicit_round_start_is_used_as_given() -> None:
    given = datetime(2026, 8, 9, 3, 30, tzinfo=UTC)
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), attack=True, round_started_at=given)

    assert loop._round_start() == given


def test_a_round_start_without_a_timezone_is_rejected() -> None:
    """help 上写着 UTC 不构成执行。

    naive 值进查询在 SQLite 上不报错，只是结果悄悄偏掉时差——
    上一轮的派遣被算进本轮，于是这一轮的目标看起来「已经打过了」。
    """
    with pytest.raises(argparse.ArgumentTypeError, match="没带时区"):
        parse_round_start("2026-08-09T00:00:00")


def test_a_round_start_is_normalised_to_utc() -> None:
    assert parse_round_start("2026-08-09T08:00:00+08:00") == datetime(2026, 8, 9, tzinfo=UTC)


def test_an_unparseable_round_start_is_rejected() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_round_start("昨天")


def test_running_out_of_lines_ends_the_round_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """航线占满是**必然**会发生的事，不能算失败。

    事故（2026-08-11 02:43，调度器跑着）：`BotLoop` 当时覆盖的是 `run()` 而不是
    `_sweep()`，把开工前置抄了一遍、却漏了父类那个 `except RoundExhausted`。于是

        RoundExhausted: 同时派遣的舰队数量已达上限

    从派遣那一步一路漏到进程外，退出码 1。调度器连撞三次就把整条 bot 链路
    **自动停用**了——而它只是需要等舰队飞回来。

    同一个覆盖还让父类 `run()` 里的断线重连对这条链路完全失效。两个洞同一个根。
    """
    from evo_helper.game import game_window
    from evo_helper.tools.pirate_loop import Outcome, RoundExhausted

    monkeypatch.setattr(game_window, "ensure_game_window", lambda: None)

    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), attack=True)
    loop._options = LoopOptions(systems=(), scout=False, attack=True)
    loop._outcome = Outcome()
    loop._navigator = _FakeNavigator()
    loop._reset_to_known_screen = lambda: None
    loop._ensure_session = lambda **_k: False
    loop.ensure_origin_planet = lambda: True
    loop.reconcile_today = lambda: None
    loop._nav_labels = lambda: ""
    loop._phase_of = lambda _c: BotPhase.NEEDS_ATTACK
    loop._attack_once = lambda _c: (_ for _ in ()).throw(
        RoundExhausted("同时派遣的舰队数量已达上限")
    )

    outcome = loop.run()  # 不抛就算过：退出码 0，不计入连续失败

    assert isinstance(outcome, Outcome)
