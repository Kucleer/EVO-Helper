"""「到达时撞保护期」这一发**白占了多少航线时间**。

## 为什么这个数值得算出来写进日志

这一档与派遣时弹窗那一档（`game.pirate_ui.DIALOG_NO_MISSION`）的差别全在代价上：

| | 触发点 | 代价 |
|---|---|---|
| 弹窗那一档 | 派遣时就被拦下 | 舰队没起飞，约 38 秒鼠标时间 |
| 这一档 | **舰队飞到了才发现** | **一整趟往返的航线时间** |

生产实测（2026-08-21 只读查得，用户 2026-08-21 提供的两张截图对应的那两发）：
单程 1467 秒的那一发白占 48.9 分钟，单程 3724 秒的那一发白占 124.1 分钟。
航线是这条链路唯一真正稀缺的东西（9 条线 × 24 小时 = 216 线小时/天），
所以「白占了多少」是这条日志里最贵的那个数——没有它，一行「撞保护期」读起来
和一次三十几秒的跳过没有区别。

## 两个来源，优先取库里记着的那个

- `line_free_at_utc − dispatched_at_utc`：派遣那一刻**调度器自己订下的**占线时长。
  生产实测这两发正好是单程的两倍（2934s / 1467s、7449s / 3724s，后者多出的
  一秒余量是订线时留的）。这是账本上的事实，优先用它。
- `2 × flight_seconds`：`line_free_at_utc` 没记上时的回落。往返 = 单程 × 2。

⚠️ **两个都没有就返回 None，不拿 0 顶替。** 0 会在日志里读成「一分钟都没浪费」，
而真相是「不知道浪费了多少」——这条链路存在的理由正是把这笔账算清楚，
在这里说一句假话比不说更糟（CLAUDE.md：「日志说假话比不说更糟」）。
"""

from __future__ import annotations

from datetime import datetime


def wasted_line_seconds(
    *,
    dispatched_at_utc: datetime | None,
    line_free_at_utc: datetime | None,
    flight_seconds: int | None,
) -> float | None:
    """这一发白占了多少秒航线。算不出返回 None。

    `line_free_at_utc` 早于（或等于）派出时刻是**不可能**的读数，说明那一列没记对；
    这时退回单程 × 2，而不是交出一个零或负数。
    """
    if dispatched_at_utc is not None and line_free_at_utc is not None:
        booked = (line_free_at_utc - dispatched_at_utc).total_seconds()
        if booked > 0:
            return booked
    if flight_seconds is not None and flight_seconds > 0:
        return float(flight_seconds) * 2
    return None


def wasted_line_minutes(
    *,
    dispatched_at_utc: datetime | None,
    line_free_at_utc: datetime | None,
    flight_seconds: int | None,
) -> float | None:
    """同上，换成分钟。日志里说分钟——秒对着一两个小时的航程没人读得出量级。"""
    seconds = wasted_line_seconds(
        dispatched_at_utc=dispatched_at_utc,
        line_free_at_utc=line_free_at_utc,
        flight_seconds=flight_seconds,
    )
    return None if seconds is None else seconds / 60


__all__ = ["wasted_line_minutes", "wasted_line_seconds"]
