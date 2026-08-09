"""Display choices for the console that are not domain rules."""

from __future__ import annotations

#: 三条任务链路在界面上的名字。
#:
#: 页面和桌面悬浮窗（`tools/scan_console.py`）都要显示「当前跑的是哪条链路」，
#: 而悬浮窗是个瘦客户端——它只认接口给的字符串。所以标签由服务端下发，
#: 两处不会各写一份、也就不会有一天对不上。
MISSION_LABELS: dict[str, str] = {
    "PIRATE": "侦查+攻击海盗",
    "BOT": "扫描+攻击 bot",
    "SCAN": "扫描全星系 bot",
}


#: Ship types shown as their own column in the intel list.
#:
#: The list is a scanning surface, so it carries only the few types the user
#: sorts targets by. Every other type stays in the detail dialog — a column per
#: recorded ship type made the table wider than any laptop screen.
LIST_SHIP_COLUMNS: tuple[str, ...] = (
    "深空吞噬者",
    "噬能截击者",
    "钛能守卫者",
    "收割者",
)
