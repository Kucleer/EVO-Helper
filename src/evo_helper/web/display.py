"""Display choices for the console that are not domain rules."""

from __future__ import annotations

from evo_helper.domain.scheduler import TaskStatus

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


#: 任务参数在界面上的名字。
#:
#: 键是 `mission_tasks.params_json` 里的字段名，也就是 `domain.missions` 那几个
#: 换算函数的形参名。配置固化记录要把「改了什么」念给人听，而
#: `first_system 100 → 120` 里那个英文字段名，页面上从来没出现过——参数框旁边
#: 写的是「起始系号」。认不出来的键回落到原样显示：宁可露出一个英文字段名，
#: 也不要把一条真的发生过的改动从记录里吞掉。
PARAM_LABELS: dict[str, str] = {
    "radius": "半径",
    "galaxy": "星系",
    "first_system": "起始系号",
    "last_system": "结束系号",
}


#: 八档状态各自的 chip 色调与字形。
#:
#: 键是 `TaskStatus` 的**每一个**成员，一个都不许缺——调度台按状态上色，
#: 少一格就意味着有两档被当成了同一件事，而恰恰是「未启用 / 待命」与
#: 「冷却中 / 等航线」这两对最不能混：没勾的任务显示「待命」是谎话，
#: 冷却中显示「等航线」会让用户去调航线数、调完还是不动。
#: `missing_status_tones()` 把「漏了一档」变成一条会红的断言。
#:
#: 色永远配一个字形（见 `console.css` 顶部）：控制台要在灰度下、对色盲用户
#: 也读得懂。
STATUS_TONES: dict[str, str] = {
    TaskStatus.RUNNING.value: "ok",
    TaskStatus.READY.value: "",
    TaskStatus.WAITING_LINES.value: "warn",
    TaskStatus.COOLING_DOWN.value: "",
    TaskStatus.QUOTA_EXHAUSTED.value: "warn",
    TaskStatus.DONE.value: "ok",
    TaskStatus.DISABLED.value: "danger",
    TaskStatus.OFF.value: "",
}

STATUS_GLYPHS: dict[str, str] = {
    TaskStatus.RUNNING.value: "▶",
    TaskStatus.READY.value: "●",
    TaskStatus.WAITING_LINES.value: "◷",
    TaskStatus.COOLING_DOWN.value: "◴",
    TaskStatus.QUOTA_EXHAUSTED.value: "■",
    TaskStatus.DONE.value: "★",
    TaskStatus.DISABLED.value: "✕",
    TaskStatus.OFF.value: "○",
}


def missing_status_tones() -> list[str]:
    """色调表或字形表里没有位置的状态。测试拿它当断言。"""
    return [
        status.name
        for status in TaskStatus
        if status.value not in STATUS_TONES or status.value not in STATUS_GLYPHS
    ]


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
