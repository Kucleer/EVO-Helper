"""攻击侦查 → 分档 → 攻击：判定这一层的规则。

真正驱动鼠标的部分在 `pirate_loop` 里已经实机跑通，这里只守判定：
**分档用的是游戏里真实存在的预设标题**、最低那一档不派，以及分档用的三道边界
来自这一轮传进来的配置而不是写死的常量。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.domain.bot_round import BotPhase
from evo_helper.domain.fleet_preset import DEFAULT_PRESET
from evo_helper.domain.fleet_tier import (
    DEFAULT_TIER_THRESHOLDS,
    FleetTier,
    TierThresholds,
    tier_for,
)
from evo_helper.domain.models import Coordinate
from evo_helper.tools.bot_loop import (
    PROBE_PRESET,
    BotLoop,
    BotOptions,
    parse_round_start,
    parse_target,
    parse_thresholds,
)

#: 用户给的那一套（2K / 4K / 8K）。三道边界可配，三个预设标题不可配。
EDGES = DEFAULT_TIER_THRESHOLDS


def test_the_probe_uses_the_in_game_scout_preset() -> None:
    """攻击侦查用「探路」——这是游戏里真实存在的标题，选预设是按标题找的。"""
    assert PROBE_PRESET == DEFAULT_PRESET.name == "探路"


def test_each_tier_maps_to_a_real_in_game_preset_title() -> None:
    """用户确认（2026-08-09）：三档分别用 AAA / BBB / CCC。

    守的是**这三个字符串必须是游戏里真实存在的预设标题**：派遣链路按标题在预设
    条上 OCR 找（`game.preset_picker`），找不到就抛 `PresetNotFound`，整发放弃。
    实机日志里出现过 `预设条上找不到 'CCC'；这一屏读到的是 ['AAA', '探路']`
    ——那次的成因是选择器只往左拖、够不到右边的预设（PR #100 已修），但它说明
    「标题对不上 = 这一发不用打了」这条后果是真会发生的。

    ⚠️ 三个**标题**不可配，只有三道**边界**可配（`/tiers` 页）。所以这条断言
    不跟着阈值走：换任何一套阈值，三档映射到的仍然是这三个标题。
    """
    assert tier_for(3000, EDGES).preset == "AAA"
    assert tier_for(6000, EDGES).preset == "BBB"
    assert tier_for(9000, EDGES).preset == "CCC"


def test_a_negligible_fleet_is_not_attacked() -> None:
    """最低那一档不派：用户明确说过那个量级不值得为它挑组合。"""
    tier = tier_for(1500, EDGES)

    assert tier is FleetTier.NEGLIGIBLE
    assert tier.preset is None


def test_the_thresholds_come_from_the_options_not_from_a_constant() -> None:
    """`BotLoop` 分档用的是这一轮传进来的阈值，不是模块里写死的数。

    调度器把它写进 argv（`domain.missions.bot_command`），手工跑时由 `main()`
    从库里读。任何一处回落到写死的默认值，就等于用一套用户没见过的数派舰队，
    而日志上看不出来。
    """
    loosened = TierThresholds(alpha_from=2000, beta_from=7000, gamma_from=8000)
    options = BotOptions(targets=(TARGET,), probe=True, attack=True, tier_thresholds=loosened)

    assert tier_for(6000, options.tier_thresholds) is FleetTier.ALPHA


def test_a_non_increasing_command_line_is_refused_at_the_entry_point() -> None:
    """不递增的阈值在入口就拒，不让它一路走到分档那一步。

    走到那一步之后，日志里看到的只是「这一轮一发 BBB 都没派」，看不出是阈值把
    那一档变成了死区。
    """
    with pytest.raises(argparse.ArgumentTypeError):
        parse_thresholds([2000, 9000, 8000])


def test_targets_are_parsed_as_full_coordinates() -> None:
    target = parse_target("2:137:14")

    assert (target.galaxy, target.system, target.position) == (2, 137, 14)


def test_bot_attacks_are_labelled_bot_not_pirate() -> None:
    """BotLoop 继承 PirateLoop 的写库路径，标签必须跟着子类走。

    标错的代价不是「日志难看」：海盗每天 32 次是游戏硬限制，bot 的发数
    混进去会让助手以为配额还没用完，多打的那一发会被强制返回。
    """
    from evo_helper.domain.records import TARGET_KIND_BOT, TARGET_KIND_PIRATE
    from evo_helper.tools.bot_loop import BotLoop
    from evo_helper.tools.pirate_loop import PirateLoop

    assert PirateLoop.TARGET_KIND == TARGET_KIND_PIRATE
    assert BotLoop.TARGET_KIND == TARGET_KIND_BOT


# -- 一趟只推进一态 ---------------------------------------------------------

TARGET = Coordinate(2, 137, 14)


class _FakeNavigator:
    def ensure_system_view(self, read_labels: Any) -> bool:
        return True

    def invalidate(self) -> None:
        return None


def _run_with_phases(
    monkeypatch: pytest.MonkeyPatch, phases: dict[Coordinate, BotPhase]
) -> list[str]:
    """跑一趟 `run()`，返回这一趟按顺序调了哪些动作。"""
    from evo_helper.game import game_window
    from evo_helper.tools.pirate_loop import Outcome

    monkeypatch.setattr(game_window, "ensure_game_window", lambda: None)

    calls: list[str] = []
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=tuple(phases), probe=True, attack=True)
    loop._outcome = Outcome()
    loop._navigator = _FakeNavigator()
    loop._reset_to_known_screen = lambda: None
    # `run()` 现在归父类管（开工前置 + `RoundExhausted` 收尾），会话巡检要真截屏，
    # 这条测试只关心分态路由，桩掉即可。
    loop._ensure_session = lambda **_k: False
    # 开工那一趟信箱（读战报 + 数今天打了几发）要开库、要看屏，这里只记它跑过。
    loop.reconcile_today = lambda: calls.append("开工那一趟信箱")
    loop._nav_labels = lambda: ""
    loop._phase_of = lambda coordinate: phases[coordinate]
    loop._probe = lambda coordinate: calls.append("probe")
    loop._tier_and_attack = lambda coordinate: calls.append("tier_and_attack")
    loop._say_still_waiting = lambda coordinate: calls.append("说还在等战报")

    loop.run()
    return calls


def _run_with_phase(monkeypatch: pytest.MonkeyPatch, phase: BotPhase) -> list[str]:
    return _run_with_phases(monkeypatch, {TARGET: phase})


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (BotPhase.NEEDS_PROBE, ["probe"]),
        (BotPhase.NEEDS_ATTACK, ["tier_and_attack"]),
        (BotPhase.AWAITING_PROBE_REPORT, ["说还在等战报"]),
        (BotPhase.AWAITING_ATTACK_REPORT, ["说还在等战报"]),
        (BotPhase.DONE, []),
    ],
)
def test_each_phase_routes_to_exactly_one_action(
    monkeypatch: pytest.MonkeyPatch, phase: BotPhase, expected: list[str]
) -> None:
    """五种态各自该做什么。

    分流一旦失效（比如无条件走探路），每个目标每趟都会重新派一发探路——
    一趟烧一条航线和一次配额，而画面上看不出异常。

    **两个等待态在这里不再进信箱**：战报由开工那一趟统一收（见下一条）。
    它们在这里只剩下一句话，而那句话要说准是「还没到点」还是「到点了却没翻到」。

    只有 `DONE` 什么都不做。
    """
    assert _run_with_phase(monkeypatch, phase) == ["开工那一趟信箱", *expected]


def test_reports_are_read_before_the_phases_are_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**本文件的重点。** 开工那一趟信箱排在分态之前，而且整轮只有那一趟。

    用户口径（2026-08-11）：「任务启动先去读战报。」顺序不能反——反了的话，
    这一趟刚读回来的探路战报要等下一轮才作数，每一份报告白等一个调度周期。

    整轮只进一趟信箱，是因为开工那一趟为了数「今天已经打了几发」**本来就要把
    信箱最上面那几屏翻一遍**，顺手把认得出的战报都开了、都入了库。另起一趟收取
    要把「关浮层 → 切地表 → 开信箱 → 慢拖回顶 → 翻页 → 关面板」整套再付一遍
    （实机约 20 秒），还要和它抢那 8 封的开封预算。

    这条也堵住了 `AWAITING_ATTACK_REPORT` 那个死结的复发形状：收取不再按「本轮在
    等哪几个目标」的名单走，名单也就漏不了态。归属只认报告自己写的目标坐标。
    """
    other = Coordinate(2, 137, 15)
    calls = _run_with_phases(
        monkeypatch,
        {
            TARGET: BotPhase.AWAITING_PROBE_REPORT,
            other: BotPhase.NEEDS_ATTACK,
        },
    )

    assert calls == ["开工那一趟信箱", "说还在等战报", "tier_and_attack"]
    assert calls.count("开工那一趟信箱") == 1


def test_the_look_only_mode_never_touches_the_database() -> None:
    """默认档一次点击都不做，就不该凭空要求一个数据库。

    这一档没有任何派遣，查库问「派过哪些发」既没有答案也没有意义。
    """
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), probe=False, attack=False)

    def _forbidden(coordinate: Coordinate) -> tuple[Any, ...]:
        raise AssertionError("只认目标模式不该查库")

    loop._dispatch_facts = _forbidden

    assert loop._phase_of(TARGET) is BotPhase.NEEDS_PROBE


# -- 本轮从何时算起 ---------------------------------------------------------


def test_a_missing_round_start_falls_back_to_todays_utc_midnight() -> None:
    """**绝不把 None 传给仓储。**

    `since=None` 在查询侧是「不限时间范围」，`mark_bot_target_skipped` 会把这个
    坐标历史上每一轮的每一条 intent 全刷成跳过。手工跑一次 `--probe --attack`，
    只要有一个目标被分档判成「不值得打」就会触发。
    """
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), probe=True, attack=True)

    start = loop._round_start()

    assert start.tzinfo is not None
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert start.date() == datetime.now(UTC).date()


def test_an_explicit_round_start_is_used_as_given() -> None:
    given = datetime(2026, 8, 9, 3, 30, tzinfo=UTC)
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), probe=True, attack=True, round_started_at=given)

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

    从 `_probe` 一路漏到进程外，退出码 1。调度器连撞三次就把整条 bot 链路
    **自动停用**了——而它只是需要等舰队飞回来。

    同一个覆盖还让父类 `run()` 里的断线重连对这条链路完全失效。两个洞同一个根。
    """
    from evo_helper.game import game_window
    from evo_helper.tools.pirate_loop import Outcome, RoundExhausted

    monkeypatch.setattr(game_window, "ensure_game_window", lambda: None)

    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), probe=True, attack=True)
    loop._outcome = Outcome()
    loop._navigator = _FakeNavigator()
    loop._reset_to_known_screen = lambda: None
    loop._ensure_session = lambda **_k: False
    loop.reconcile_today = lambda: None
    loop._nav_labels = lambda: ""
    loop._phase_of = lambda _c: BotPhase.NEEDS_PROBE
    loop._probe = lambda _c: (_ for _ in ()).throw(RoundExhausted("同时派遣的舰队数量已达上限"))

    outcome = loop.run()  # 不抛就算过：退出码 0，不计入连续失败

    assert isinstance(outcome, Outcome)
