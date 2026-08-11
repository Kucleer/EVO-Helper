"""进信箱前必须先关浮层，切不过去要留下现场。

`_on_planet_surface()` 的**正面凭据是右上角信箱旁边的未读数**，而它自己的注释就
写着「浮层会盖住它」。可 `_goto_planet_surface()` 并不关浮层——它只会反复点视图
菜单，而那个坐标此刻正压在浮层底下。

这一步偏偏紧跟在 `_wait_for_reports()` 的 45 秒等待之后，正是舰队返航之类的通知
最容易冒出来的时刻。实机三次都倒在这里：

    02:10  切不到自己星球地表，读不了信箱；安全停止
    03:35  同上
    03:46  同上（同一轮里 2:137 的报告读成功过，换到 2:136 才倒）

每次都已经先派出 4 发侦察，报告读不到那 4 发就白飞；而 `RuntimeError` 计入连续
失败，三次就把整条链路自动停用。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.tools.pirate_loop import LoopOptions, PirateLoop

WANTED = (Coordinate(2, 136, 1),)


def _loop(*, reachable: bool) -> tuple[Any, list[str]]:
    events: list[str] = []
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=True, attack=True)  # type: ignore[attr-defined]
    loop._started_at = datetime(2026, 8, 11, tzinfo=UTC)  # type: ignore[attr-defined]
    loop._reset_to_known_screen = lambda: events.append("关浮层")  # type: ignore[assignment, method-assign]
    loop._goto_planet_surface = lambda: (  # type: ignore[assignment, method-assign]
        events.append("切地表"),
        reachable,
    )[1]
    loop._dump_frame = lambda name, roi=None: events.append(f"存图:{name}")  # type: ignore[assignment, method-assign]
    loop._open_mail = lambda: events.append("开信箱")  # type: ignore[assignment, method-assign]
    return loop, events


def test_overlays_are_closed_before_the_surface_check() -> None:
    """本文件的重点：关浮层必须排在切地表**之前**。

    顺序反了等于没修——判据读的就是被浮层盖住的那个未读数。
    """
    loop, events = _loop(reachable=True)

    # 开信箱之后的读报告流程不在这条测试的范围内，用一个哨兵异常截断。
    loop._open_mail = lambda: (_ for _ in ()).throw(RuntimeError("到此为止"))  # type: ignore[assignment, method-assign]
    with pytest.raises(RuntimeError, match="到此为止"):
        loop.collect_scout_reports(WANTED)

    assert events == ["关浮层", "切地表"]


def test_an_unreachable_surface_leaves_a_frame_behind() -> None:
    """切不过去就存一帧：不知道当时画面长什么样是最贵的失败。"""
    loop, events = _loop(reachable=False)

    with pytest.raises(RuntimeError, match="切不到自己星球地表"):
        loop.collect_scout_reports(WANTED)

    assert events == ["关浮层", "切地表", "存图:planet-surface-unreachable"]
