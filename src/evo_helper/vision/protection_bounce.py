"""读「到达之后才发现目标在保护期，舰队原路返航」那封通知。

## 这和已知的保护期不是同一件事，代价差两个数量级

仓库里原有的保护期判据是**派遣时**弹出来的那个弹窗
（`game.pirate_ui.DIALOG_NO_MISSION`「没有可执行的任务」）：舰队根本没起飞，
跳过这个目标继续下一个，代价约 38 秒鼠标时间。

这一封说的是另一件事：**舰队飞到了才发现**目标在保护期，原路返航。用户 2026-08-21
提供的两张截图配着生产库核对过，邮件时刻与那一发的到达时刻分秒吻合：

    5:222:3   派出 13:12:07 UTC，单程 1467 秒 → 到达 13:36:34   邮件 13:36:34
    4:321:9   派出 13:27:26 UTC，单程 3724 秒 → 到达 14:29:31   邮件 14:29:32

也就是说白占的是**一整趟往返**的航线时间（分别是 48.9 分钟与 124.1 分钟），
不是几十秒。

## 三重后果，而且账上看不出来

1. 这一发**结构上永远不会有战报**（没打起来，游戏不产出战报），于是它会永久沉在
   「未读回」里，把回收率往下拽——收益看起来比实际差；
2. `bot_targets.protection_seen_at_utc` 不会被写，那个坐标留在候选池里，下一轮
   可能又被挑中，再白占一趟；
3. 白占的那一趟航线，在账上和「战报还没回来」长得一模一样，分不出来。

生产库佐证（2026-08-21 只读查得）：近 3 天 `system_log` 里提到「保护状态」的日志
0 条——解析器从来没认过它。

## ⚠️ 它不是战报，读法也不该照战报那一套

没有 VS 块、没有参战舰队、没有战损、没有收获，只有页眉一行时间和正文一句话。
所以这个模块与 `vision.planet_scout_alert` 同形（同一块正文 ROI、同样两个字段、
同样「读不齐就整封拒收」），而不是与 `vision.pirate_reports` 同形。

判据（坐标 + 「处于保护状态」+ 「已返航」三样缺一不可）住在
`vision.parsers.PROTECTION_BOUNCE_RE`，**只有那一份**：邮件列表行要用它决定
「这一行值不值得打开」，正文要用它决定「这封是不是」，两处用同一个模式，
放宽或收紧只有一个地方能改。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from evo_helper.domain.models import Coordinate
from evo_helper.vision.parsers import (
    GAME_DISPLAY_ZONE,
    find_protection_bounce_targets,
    parse_report_timestamp,
)

_TIME_TEXT_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\b")


class ProtectionBounceScreens(Protocol):
    def report_header(self) -> str: ...

    def security_message(self) -> str: ...


class ProtectionBounceUnreadable(ValueError):
    """这一封读不齐；**不猜、不存半份**，下一趟它还在信箱里。"""


@dataclass(frozen=True)
class ProtectionBounceReading:
    """读通了的一封「到达时撞保护期」。

    ⚠️ **`reported_at_utc` 是邮件自己写的时刻，也就是舰队到达的那一刻**，
    不是我们翻到它的时刻。两者可能差好几个小时（信箱是隔一阵才翻一次的），
    而 `bot_targets.protection_seen_at_utc` 要的是「什么时候撞上的」——写成处理
    时刻会把保护期的起点往后推，那个坐标于是被多排除几个小时。
    """

    raw_time_text: str
    reported_at_utc: datetime
    target: Coordinate
    raw_body: str


def read_protection_bounce(screens: ProtectionBounceScreens) -> ProtectionBounceReading:
    """把详情页读成一条「到达时撞保护期」。读不齐就抛。

    时间取**页眉**那一行（与战报、安全告警同一个来源），不取正文——正文里根本
    没有时间。目标坐标取正文里那一句自己写的那个。
    """
    header = screens.report_header()
    raw_body = screens.security_message()
    reported_at = parse_report_timestamp(header, GAME_DISPLAY_ZONE)
    if reported_at is None:
        raise ProtectionBounceUnreadable("邮件页眉没有可用的时间")
    targets = find_protection_bounce_targets(raw_body)
    if not targets:
        raise ProtectionBounceUnreadable("正文里没有读到完整的「X 处于保护状态…已返航」")
    if len(targets) > 1:
        # 一封信里两句，说明要么 OCR 把两封串到了一起，要么游戏改了版面。
        # 两种都不该猜着取第一个：认错目标就会把保护期记到别人头上，
        # 而那个坐标从此被无故排除。
        raise ProtectionBounceUnreadable(
            f"正文里读到了 {len(targets)} 个保护期返航目标（{targets}）；不猜是哪一个"
        )
    return ProtectionBounceReading(
        raw_time_text=_time_text(header),
        reported_at_utc=reported_at,
        target=targets[0],
        raw_body=raw_body,
    )


def _time_text(header: str) -> str:
    match = _TIME_TEXT_RE.search(header)
    if match is None:
        raise ProtectionBounceUnreadable("邮件页眉没有可用的时间原文")
    return match.group(0)


__all__ = [
    "ProtectionBounceReading",
    "ProtectionBounceScreens",
    "ProtectionBounceUnreadable",
    "read_protection_bounce",
]
