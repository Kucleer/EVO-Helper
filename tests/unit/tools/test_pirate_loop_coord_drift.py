"""海盗链路遇到「导航漂移」要自愈一次——但**只对坐标核对不过**自愈。

事故（2026-08-11）：`SystemNavigator.goto` 只重设它认为变了的字段。一次「设恒星系」
的点击落到了银河系框上，游戏把 136 截断成最大值 9，此后导航栏是 `[9:137:12]` 而
缓存说 `2:137`；后面每个目标的银河系都是 2，于是那个字段再没被重设，连续 44 个
目标坐标核对全不过。bot 链路已经修了（`_goto_checked`），海盗链路当时没有。

海盗这边的难点是**「认不出」大多数时候是正常的**：1–4 位里没有海盗是家常便饭。
所以两种 False 必须分开（`TargetCheck`）：

- `ABSENT`（这一位没有海盗）→ 照常走下一位。当成异常去复位重试，每个空位都要
  多付一次复位+重导航，整轮慢一倍。
- `MISMATCH`（面板是真的，但显示的不是请求的那一位）→ 导航漂了，必须自愈。

⚠️ **坐标判据本身一个字都不许放松**：实机那一轮里有一次面板读到的是上一个目标的
星系（请求 2:321:5，面板 2:320:5），核对拦对了。放松成「位次对上就行」就是往错误
的星球扔舰队。这里改的只是核对不过之后怎么办。
"""

from __future__ import annotations

from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.game import pirate_ui
from evo_helper.tools.pirate_loop import LoopOptions, Outcome, PirateLoop, TargetCheck

TARGET = Coordinate(2, 137, 3)


# -- 三值判定本身 --------------------------------------------------------------


def _reading_loop(*, title: str, coordinate_text: str) -> tuple[Any, list[str]]:
    """一个 `check_target` 走真实实现、但读屏被替掉的循环。"""
    dumped: list[str] = []

    def _read(roi: tuple[int, int, int, int], **_recipe: Any) -> str:
        return coordinate_text if roi == pirate_ui.PIRATE_COORD_ROI else title

    loop = PirateLoop.__new__(PirateLoop)
    loop._coord_dumps = 0  # type: ignore[attr-defined]
    loop._read = _read  # type: ignore[assignment, method-assign]
    loop._dump_frame = lambda name, roi=None: dumped.append(name)  # type: ignore[assignment, method-assign]
    return loop, dumped


def test_an_empty_position_reads_as_absent() -> None:
    """没有海盗就是 `ABSENT`——正常结果，不是异常。"""
    loop, dumped = _reading_loop(title="", coordinate_text="")

    assert loop.check_target(TARGET) is TargetCheck.ABSENT
    # 空位不留现场：那会把最常见的正常结果写成一地图。
    assert dumped == []


def test_a_pirate_panel_showing_another_planet_reads_as_mismatch() -> None:
    """面板是真的海盗面板，坐标却是别人——导航漂了。"""
    loop, dumped = _reading_loop(title=pirate_ui.PIRATE_TITLE_TEXT, coordinate_text="2:136:3")

    assert loop.check_target(TARGET) is TargetCheck.MISMATCH
    assert dumped == ["pirate-coord-mismatch"]


def test_the_coordinate_criterion_is_not_relaxed() -> None:
    """位次对上、星系不对，仍然是 `MISMATCH`。

    实机上真读到过上一个目标的星系（请求 2:321:5，面板 2:320:5）。放松成
    「位次对上就行」就是往错误的星球扔舰队。
    """
    loop, _dumped = _reading_loop(title=pirate_ui.PIRATE_TITLE_TEXT, coordinate_text="9:137:3")

    assert loop.check_target(TARGET) is TargetCheck.MISMATCH


def test_a_matching_pirate_panel_is_confirmed() -> None:
    loop, dumped = _reading_loop(
        title=f"[{pirate_ui.PIRATE_TITLE_TEXT}]", coordinate_text="2:137:3"
    )

    assert loop.check_target(TARGET) is TargetCheck.CONFIRMED
    assert dumped == []


def test_mismatch_dumps_are_capped() -> None:
    """一整轮漂下去也不能写出上百张几乎一样的现场图。"""
    loop, dumped = _reading_loop(title=pirate_ui.PIRATE_TITLE_TEXT, coordinate_text="9:137:3")

    for _ in range(10):
        loop.check_target(TARGET)

    assert len(dumped) == PirateLoop.MAX_COORD_DUMPS


# -- 自愈 ---------------------------------------------------------------------


class _Navigator:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def goto(self, coordinate: Coordinate) -> None:
        self._events.append(f"goto {coordinate.position}")

    def invalidate(self) -> None:
        self._events.append("清缓存")


def _loop(events: list[str], checks: list[TargetCheck]) -> Any:
    loop = PirateLoop.__new__(PirateLoop)
    loop._navigator = _Navigator(events)  # type: ignore[attr-defined]

    def _check(_coordinate: Coordinate) -> TargetCheck:
        check = checks.pop(0)
        events.append(f"核对 {check.value}")
        return check

    def _reset() -> None:
        events.append("复位画面")

    def _session(*, force: bool = False) -> bool:
        events.append("查会话")
        return False

    loop.check_target = _check  # type: ignore[assignment, method-assign]
    loop._reset_to_known_screen = _reset  # type: ignore[assignment, method-assign]
    loop._ensure_session = _session  # type: ignore[assignment, method-assign]
    return loop


def test_an_absent_pirate_costs_nothing_extra() -> None:
    """这条是本文件的重点之一：空位**不许**触发复位重试。

    1–4 位里没有海盗是家常便饭。每个空位多付一次复位+重导航，整轮慢一倍。
    """
    events: list[str] = []
    loop = _loop(events, [TargetCheck.ABSENT])

    assert loop._goto_checked(TARGET) is TargetCheck.ABSENT
    assert events == ["goto 3", "核对 不是目标"]


def test_a_confirmed_pirate_costs_nothing_extra() -> None:
    events: list[str] = []
    loop = _loop(events, [TargetCheck.CONFIRMED])

    assert loop._goto_checked(TARGET) is TargetCheck.CONFIRMED
    assert events == ["goto 3", "核对 认出目标"]


def test_a_mismatch_resets_the_screen_and_retries() -> None:
    """另一个重点：坐标核对不过要走完整条自愈，**清缓存**在里面。

    清缓存不是可有可无的一步，而是整条重试的全部意义：导航器认为某个字段已经
    对了就不去重设，所以只要它的记忆和导航栏实际值分了岔，不清缓存的重试会
    一字不差地重演上一次失败。
    """
    events: list[str] = []
    loop = _loop(events, [TargetCheck.MISMATCH, TargetCheck.CONFIRMED])

    assert loop._goto_checked(TARGET) is TargetCheck.CONFIRMED
    assert events == [
        "goto 3",
        "核对 坐标核对不过",
        "查会话",
        "复位画面",
        "清缓存",
        "goto 3",
        "核对 认出目标",
    ]


def test_it_gives_up_after_one_retry() -> None:
    """重试一次就够。无限重试会把整轮卡死在一个位次上，比跳过还糟。"""
    events: list[str] = []
    loop = _loop(events, [TargetCheck.MISMATCH, TargetCheck.MISMATCH])

    assert loop._goto_checked(TARGET) is TargetCheck.MISMATCH
    assert events.count("复位画面") == 1
    assert events.count("goto 3") == 2


# -- 扫一遍 1–4 位 -------------------------------------------------------------


def _sweeping_loop(events: list[str], per_position: dict[int, list[TargetCheck]]) -> Any:
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=((2, 137),), scout=True, attack=False)  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._navigator = _Navigator(events)  # type: ignore[attr-defined]

    def _check(coordinate: Coordinate) -> TargetCheck:
        return per_position[coordinate.position].pop(0)

    def _reset() -> None:
        events.append("复位画面")

    def _session(*, force: bool = False) -> bool:
        return False

    def _scout(coordinate: Coordinate) -> bool:
        events.append(f"scout {coordinate.position}")
        return True

    loop.check_target = _check  # type: ignore[assignment, method-assign]
    loop._reset_to_known_screen = _reset  # type: ignore[assignment, method-assign]
    loop._ensure_session = _session  # type: ignore[assignment, method-assign]
    loop.scout = _scout  # type: ignore[assignment, method-assign]
    return loop


def test_a_sweep_of_empty_positions_never_resets() -> None:
    """整整一轮空位，一次复位都不许有——否则整轮慢一倍。"""
    events: list[str] = []
    loop = _sweeping_loop(events, {position: [TargetCheck.ABSENT] for position in (1, 2, 3, 4)})

    pirates, scouted = loop._find_pirates(2, 137)

    assert events == ["goto 1", "goto 2", "goto 3", "goto 4"]
    assert (pirates, scouted) == ([], 0)


def test_a_drifted_position_heals_and_still_gets_scouted() -> None:
    """漂移的那一位自愈之后照常侦察；后面几位不受影响。"""
    events: list[str] = []
    loop = _sweeping_loop(
        events,
        {
            1: [TargetCheck.ABSENT],
            2: [TargetCheck.MISMATCH, TargetCheck.CONFIRMED],
            3: [TargetCheck.ABSENT],
            4: [TargetCheck.ABSENT],
        },
    )

    pirates, scouted = loop._find_pirates(2, 137)

    assert events == [
        "goto 1",
        "goto 2",
        "复位画面",
        "清缓存",
        "goto 2",
        "scout 2",
        "goto 3",
        "goto 4",
    ]
    assert [c.position for c in pirates] == [2]
    assert scouted == 1


def test_a_position_that_stays_drifted_is_recorded_as_refused() -> None:
    """自愈完还不过就要**记一笔**。

    不记的话它长得和「这一位没有海盗」一模一样，而后者是最常见的正常结果——
    整轮一发没派，从日志和收尾统计上都看不出异常。
    """
    events: list[str] = []
    loop = _sweeping_loop(
        events,
        {
            1: [TargetCheck.MISMATCH, TargetCheck.MISMATCH],
            2: [TargetCheck.ABSENT],
            3: [TargetCheck.ABSENT],
            4: [TargetCheck.ABSENT],
        },
    )

    pirates, scouted = loop._find_pirates(2, 137)

    assert (pirates, scouted) == ([], 0)
    assert loop._outcome.refused == [(Coordinate(2, 137, 1), "坐标核对不过")]
    # 认不出的那一位绝不许派侦察出去。
    assert "scout 1" not in events
