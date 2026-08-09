"""攻击侦查 → 分档 → 攻击：判定这一层的规则。

真正驱动鼠标的部分在 `pirate_loop` 里已经实机跑通，这里只守判定：
**分档用的是游戏里真实存在的预设标题**，以及「2K 以下不派」。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.domain.bot_round import BotPhase
from evo_helper.domain.fleet_preset import DEFAULT_PRESET
from evo_helper.domain.fleet_tier import FleetTier, tier_for
from evo_helper.domain.models import Coordinate
from evo_helper.tools.bot_loop import (
    PROBE_PRESET,
    BotLoop,
    BotOptions,
    parse_round_start,
    parse_target,
)


def test_the_probe_uses_the_in_game_scout_preset() -> None:
    """攻击侦查用「探路」——这是游戏里真实存在的标题，选预设是按标题找的。"""
    assert PROBE_PRESET == DEFAULT_PRESET.name == "探路"


def test_each_tier_maps_to_a_real_in_game_preset_title() -> None:
    """用户确认（2026-08-09）：甲=AAA、乙=BBB、丙=CCC。

    原先写的是「攻击组合甲/乙/丙」——游戏里没有这些预设，`PresetPicker`
    照它去找一定找不到，于是每一发都在「找不到预设」上整发放弃。
    """
    assert tier_for(3000).preset == "AAA"
    assert tier_for(6000).preset == "BBB"
    assert tier_for(9000).preset == "CCC"


def test_a_negligible_fleet_is_not_attacked() -> None:
    """2K 以下不派：用户明确说过那个量级不值得为它挑组合。"""
    tier = tier_for(1500)

    assert tier is FleetTier.NEGLIGIBLE
    assert tier.preset is None


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


def _run_with_phase(monkeypatch: pytest.MonkeyPatch, phase: BotPhase) -> list[str]:
    """跑一趟 `run()`，返回这一趟调了哪些动作。"""
    from evo_helper.game import game_window
    from evo_helper.tools.pirate_loop import Outcome

    monkeypatch.setattr(game_window, "ensure_game_window", lambda: None)

    calls: list[str] = []
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(TARGET,), probe=True, attack=True)
    loop._outcome = Outcome()
    loop._navigator = _FakeNavigator()
    loop._reset_to_known_screen = lambda: None
    loop._phase_of = lambda coordinate: phase
    loop._probe = lambda coordinate: calls.append("probe")
    loop._tier_and_attack = lambda coordinate: calls.append("tier_and_attack")

    loop.run()
    return calls


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (BotPhase.NEEDS_PROBE, ["probe"]),
        (BotPhase.NEEDS_ATTACK, ["tier_and_attack"]),
        (BotPhase.AWAITING_PROBE_REPORT, []),
        (BotPhase.AWAITING_ATTACK_REPORT, []),
        (BotPhase.DONE, []),
    ],
)
def test_each_phase_routes_to_exactly_one_action(
    monkeypatch: pytest.MonkeyPatch, phase: BotPhase, expected: list[str]
) -> None:
    """五种态各自该做什么，以及**等待中的三种什么都不做**。

    分流一旦失效（比如无条件走探路），每个目标每趟都会重新派一发探路——
    一趟烧一条航线和一次配额，而画面上看不出异常。等战报的那三态尤其要守：
    它们的正确行为就是「这一趟不碰它」。
    """
    assert _run_with_phase(monkeypatch, phase) == expected


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
