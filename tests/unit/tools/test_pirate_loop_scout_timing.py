"""侦察必须在**识别的同一趟**里发出去，不能等认完一圈再回头。

事故（2026-08-11 凌晨）：用户开着 `--scout --attack` 跑海盗链路，43 秒后判定
「侦查和攻击都没触发」，把进程停了。复现下来那一轮确实一发都没派出去，但原因
不是派不出去——是**还没轮到派**：

    01:20:47 模式：侦察+攻击
    01:21:13   2:137:1 敌对海盗      ← 这 68 秒里日志只有这四行
    01:21:21   2:137:2 敌对海盗
    01:21:28   2:137:3 敌对海盗
    01:21:35   2:137:4 敌对海盗
    01:21:55   已派出侦察 → 2:137:1  ← 首发在开跑后 68 秒

原因是走了两趟导航：先 1–4 位认一遍，再对认出来的每个海盗重新 `goto` 一次才
侦察。认出海盗的那一刻面板已经开着、侦察按钮就在眼前，第二趟纯属白跑。

所以这里钉的不是「侦察发了几发」——两趟导航那版发的数量一模一样，只数数量的
测试**照样绿**。要钉的是**交错顺序**：第 1 位的侦察必须发生在走到第 2 位之前。
"""

from __future__ import annotations

from evo_helper.domain.models import Coordinate
from evo_helper.tools.pirate_loop import LoopOptions, Outcome, PirateLoop, TargetCheck


class _Navigator:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def goto(self, coordinate: Coordinate) -> None:
        self._events.append(f"goto {coordinate.position}")


def _loop(events: list[str], *, scout: bool, pirates_at: set[int]) -> PirateLoop:
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=((2, 137),), scout=scout, attack=True)  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._navigator = _Navigator(events)  # type: ignore[attr-defined]

    def _check(coordinate: Coordinate) -> TargetCheck:
        return TargetCheck.CONFIRMED if coordinate.position in pirates_at else TargetCheck.ABSENT

    loop.check_target = _check  # type: ignore[assignment, method-assign]

    def _scout(coordinate: Coordinate) -> bool:
        events.append(f"scout {coordinate.position}")
        return True

    loop.scout = _scout  # type: ignore[assignment, method-assign]
    return loop


def test_each_scout_goes_out_before_the_next_planet_is_visited() -> None:
    """这条是本文件的重点：两趟导航那版会先四个 goto、再四个 scout。"""
    events: list[str] = []
    loop = _loop(events, scout=True, pirates_at={1, 2, 3, 4})

    pirates, scouted = loop._find_pirates(2, 137)

    assert events == [
        "goto 1",
        "scout 1",
        "goto 2",
        "scout 2",
        "goto 3",
        "scout 3",
        "goto 4",
        "scout 4",
    ]
    assert scouted == 4
    assert [c.position for c in pirates] == [1, 2, 3, 4]


def test_non_pirates_are_never_scouted() -> None:
    """认不出海盗的位次不许发侦察——发出去就是往空位扔舰队。"""
    events: list[str] = []
    loop = _loop(events, scout=True, pirates_at={2})

    pirates, scouted = loop._find_pirates(2, 137)

    assert events == ["goto 1", "goto 2", "scout 2", "goto 3", "goto 4"]
    assert scouted == 1
    assert [c.position for c in pirates] == [2]


def test_without_the_scout_flag_it_only_identifies() -> None:
    """`--attack` 不给 `--scout` 时用信箱里已有的报告，这一趟只认不派。"""
    events: list[str] = []
    loop = _loop(events, scout=False, pirates_at={1, 3})

    pirates, scouted = loop._find_pirates(2, 137)

    assert events == ["goto 1", "goto 2", "goto 3", "goto 4"]
    assert scouted == 0
    assert [c.position for c in pirates] == [1, 3]


def test_identified_pirates_still_land_in_the_outcome() -> None:
    """融合之后 `Outcome.pirates` 不能漏记——攻击阶段是照它逐个判定的。"""
    events: list[str] = []
    loop = _loop(events, scout=True, pirates_at={1, 4})

    loop._find_pirates(2, 137)

    assert [c.position for c in loop._outcome.pirates] == [1, 4]
