"""挂机运行时长：调度器到底开着多久。**心跳，不是启停。**

这个指标是「航线利用率」的另一半。2026-08-20 起利用率的分母换成了
**周期总时长 × 航线数**（见 `domain.overview.available_seconds`），于是「关一晚上
机器、第二天利用率腰斩」这件事不再由分母替用户解释——**改由这个数直说**：
利用率答「航线闲不闲」，挂机时长答「那段时间到底开没开工」。两个必须一起看，
只有前一半的页面比改之前更难读。

## ⚠️ 为什么必须新增落库，不许拿现有的数凑

- `state_events` 全表只有 **1 行**（`run started`），写它的那条路径早就删了
  （`web.persistent_service` 里 `_event` 那段注释）。
- **不许把「轮次时长相加」当挂机时长。** 实测 2026-08-20 近 24 小时：`mission_runs`
  的轮次覆盖 17.7h、首尾跨度 23.8h，大于 2 分钟的空隙 15 个合计 6.0h。其中
  11:45→12:25 那 41 分钟**调度器是开着的**，只是扫描间隔挡住了 RANKING、
  `waiting_for_a_line` 压住了 BOT。拿轮次覆盖当挂机时长，会把「开着但没活干」
  误报成「关机」——那是让指标说假话，比没有这个指标糟。

## ⚠️ 必须扛住进程被杀

崩溃、断电、任务管理器结束进程时**不会有人写「已停止」**。所以这里不记「启 / 停」
两个事件，而是**一段一行、每拍把末端往前推**（`last_beat_at_utc`）：进程被杀，
那一行就停在最后一拍上，挂机时长不会继续涨。这条性质有用例钉着。

## 「无数据」不是 0

心跳是 2026-08-20 才加的，**之前的天补不回来**。那些天必须显示「—」：显示 0 等于
说「那天没开机」，而那是假话（同 `web.overview_routes.TodayCard.resources_seen`
那一段的判据——库里分不开「真的 0」和「没观测过」时，不许拿 0 冒充）。
`uptime_seconds` 因此返回 `float | None`，None 就是「这一段没有观测」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from evo_helper.domain.overview import overlap_seconds

#: 两拍之间隔多久。**标定常量，不是旋钮**（CLAUDE.md 那条的判据：改它会让结果
#: 变「错」，不是变「更适合我」）。
#:
#: 它同时定死两件事：**这个指标的分辨率**，以及**进程被杀时最多白送多少挂机时长**
#: （最后一拍之后到被杀那一刻，最坏就是一整个间隔）。调大到 10 分钟，被杀一次
#: 最多虚报 10 分钟；调小到 1 秒，一天 86,400 次 UPDATE 全白烧。60 秒是让
#: 「虚报不超过 1 分钟」和「一天 1,440 次 UPDATE」两边都能接受的取值——
#: 用户改它不会让这个数更贴合自己的处境，只会让它更不准。
HEARTBEAT_INTERVAL_S = 60.0

#: 隔多久没心跳就判「这中间没在跑」，下一拍另开一段。**同样是标定常量。**
#:
#: 它回答的是「多久的空档算关机」。必须**明显大于**一拍的间隔，否则一次卡顿
#: （tick 去查库、起子进程，`wait(5)` 都在同一个线程里）就会把一段连续的挂机
#: 切成一串碎段；也必须**明显小于**「一次重启要多久」，否则重启前后会被接成
#: 一段，把中间关着的那阵算成开着。5 分钟 = 5 拍。
MAX_HEARTBEAT_GAP_S = 300.0


@dataclass(frozen=True, slots=True)
class UptimeSegment:
    """一段「调度器一直开着」的时间。`storage.overview` 把库里的行翻成它。

    `last_beat` 是**最后一拍的时刻**，不是「停止时刻」——没有人保证会有停止时刻。
    """

    start: datetime
    last_beat: datetime


def due_for_a_beat(*, last_beat: datetime | None, now: datetime) -> bool:
    """现在该不该落一拍。

    ⚠️ tick 是**每秒一次**的，每次都写等于一天 86,400 次 UPDATE。按间隔限流，
    同 `record_unrecognised_screen` 那 120 秒的先例（CLAUDE.md：每 tick 可能触发的
    都要限流）。
    """
    if last_beat is None:
        return True
    return (now - last_beat).total_seconds() >= HEARTBEAT_INTERVAL_S


def opens_a_new_segment(*, last_beat: datetime | None, now: datetime) -> bool:
    """这一拍是接着上一段，还是另开一段。

    - 上一拍不知道（刚起进程、刚被点开）→ 另开一段。⚠️ **重启之后一律另开一段，
      不去库里找上一段接回来**：接回来会把控制台重启那几十秒算成挂机（最坏
      `MAX_HEARTBEAT_GAP_S` 那么多）。宁可少算一拍，也不让这个数说大话。
    - 离上一拍超过 `MAX_HEARTBEAT_GAP_S` → 另开一段：那中间要么进程死了、
      要么机器睡了，**不许把那段空档接进挂机时长**。
    """
    if last_beat is None:
        return True
    return (now - last_beat).total_seconds() > MAX_HEARTBEAT_GAP_S


def uptime_seconds(
    segments: tuple[UptimeSegment, ...],
    *,
    observed_since: datetime | None,
    window_start: datetime,
    window_end: datetime,
) -> float | None:
    """这个窗口里调度器开着多少秒。**没有观测的窗口返回 None，不是 0。**

    `observed_since` 是库里**最早那一拍**的时刻（`None` = 一拍都没有）。窗口整段
    落在它之前，就是「心跳还没上线的那些天」——返回 None，页面显示「—」。
    显示 0 等于说「那天没开机」，而那是假话，这一条有用例钉着。

    窗口**跨着** `observed_since`（比如「合计」那一档，起点固定在 2026-08-17）时
    照常给数，但那个数是**下界**：前半段没观测。判据在 `partially_observed`，
    页面要把这件事说出来。
    """
    if observed_since is None or observed_since >= window_end:
        return None
    return sum(
        overlap_seconds(item.start, item.last_beat, window_start, window_end) for item in segments
    )


def partially_observed(*, observed_since: datetime | None, window_start: datetime) -> bool:
    """这个窗口是不是只有一部分有心跳观测（因此挂机时长是下界）。"""
    return observed_since is not None and observed_since > window_start


__all__ = [
    "HEARTBEAT_INTERVAL_S",
    "MAX_HEARTBEAT_GAP_S",
    "UptimeSegment",
    "due_for_a_beat",
    "opens_a_new_segment",
    "partially_observed",
    "uptime_seconds",
]
