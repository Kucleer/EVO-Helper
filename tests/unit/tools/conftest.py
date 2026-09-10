"""现场图限流的状态是**进程级**的，用例之间必须清干净。

`pirate_loop._last_evidence_frame_at` 记的是「这一类现场上次存图的时刻」，按
`time.monotonic` 算。同一个 pytest 进程里前后两条用例相隔远不到 120 秒，所以
不清的话，先跑的那条会把后跑的那条的图掐掉——而后者往往正是断言「这里应该有
一张图」的那条。⚠️ **这种红是按用例顺序变的**，`-k` 单独跑就绿，混着跑就红，
排查起来最费时间。

只清这一个字典，不动窗口常量：用例要验限流本身时靠自己传 `now`（先例是
`record_planet_list_overlay_retry` 与 `record_unrecognised_screen` 的 `now` 参数）。
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _clear_evidence_frame_throttle() -> Any:
    from evo_helper.tools import pirate_loop

    pirate_loop._last_evidence_frame_at.clear()
    yield
    pirate_loop._last_evidence_frame_at.clear()
