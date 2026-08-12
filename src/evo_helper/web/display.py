"""Display choices for the console that are not domain rules."""

from __future__ import annotations

from evo_helper.domain.records import TARGET_KIND_LABELS
from evo_helper.domain.scheduler import TaskStatus
from evo_helper.game.pirate_ui import PIRATE_TRIGGER_SHIPS

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
#:
#: **就是海盗侦察的四个判定舰种**，所以直接引用 `PIRATE_TRIGGER_SHIPS` 而不是
#: 再抄一份字面量：这四列存在的理由就是「侦察判定看的是这四个」，判定表哪天增删
#: 一种，列跟着变才对。抄一份的话，改了那边而这边没改，页面会安静地少显示一列——
#: 而少的那一列恰恰是决定打不打的那一个数。
LIST_SHIP_COLUMNS: tuple[str, ...] = PIRATE_TRIGGER_SHIPS


#: 情报中心列表里 bot 与海盗各自的 chip 样式。
#:
#: 两者要一眼分得开——它们是两条完全不同的链路（bot 走扫描+探路+分档，海盗走
#: 侦察+判定+攻击），混在一张表里读的时候，最先要回答的就是「这行是哪种」。
#:
#: 色永远配一个字形和一个词（同 `STATUS_TONES` 的规矩）：控制台要在灰度下、
#: 对色盲用户也读得懂，所以 chip 里写的是「bot」「海盗」，色只是加速。
TARGET_KIND_TONES: dict[str, str] = {"bot": "kind-bot", "pirate": "kind-pirate"}
TARGET_KIND_GLYPHS: dict[str, str] = {"bot": "▣", "pirate": "☠"}


#: 「结果」（最近一次派遣有没有真的发出去）的中文标签与 chip 样式。
#:
#: 键是 `storage.intel.DISPATCH_*`。库里与接口里一律是英文常量，界面只显示中文。
DISPATCH_STATE_LABELS: dict[str, str] = {
    "SENT": "已派出",
    "BLOCKED": "未派出",
    "REJECTED": "被拒",
    "NEVER": "从未派遣",
}

DISPATCH_STATE_TONES: dict[str, str] = {
    "SENT": "ok",
    "BLOCKED": "warn",
    "REJECTED": "danger",
    "NEVER": "",
}

DISPATCH_STATE_GLYPHS: dict[str, str] = {
    "SENT": "▶",
    "BLOCKED": "◷",
    "REJECTED": "✕",
    "NEVER": "○",
}


#: 「战果」的中文标签与 chip 样式。键是 `storage.intel.RESULT_*`。
#:
#: 认不出来的 outcome **原样显示**，不拿「不是胜就是负」兜底：库里存的是画面
#: 原文，将来多一档会被静默显示成败仗（`logs.html` 上同一条取舍）。
BATTLE_RESULT_LABELS: dict[str, str] = {
    "VICTORY": "胜",
    "FAIL": "负",
    "DRAW": "平",
    "AWAITING": "待战报",
    "NONE": "不适用",
}

BATTLE_RESULT_TONES: dict[str, str] = {
    "VICTORY": "ok",
    "FAIL": "danger",
    "DRAW": "",
    "AWAITING": "warn",
    "NONE": "",
}

BATTLE_RESULT_GLYPHS: dict[str, str] = {
    "VICTORY": "★",
    "FAIL": "✕",
    "DRAW": "＝",
    "AWAITING": "🕗",
    "NONE": "—",
}


def missing_intel_labels() -> list[str]:
    """三张标签表里没有位置的取值。测试拿它当断言。

    与 `missing_status_tones()` 同一个用意：漏一档就意味着页面上会出现一个
    没人翻译过的英文常量，或者更糟——两档被当成同一件事。
    """
    from evo_helper.storage.intel import BATTLE_RESULTS, DISPATCH_STATES

    missing = [
        f"DISPATCH_{state}"
        for state in DISPATCH_STATES
        if state not in DISPATCH_STATE_LABELS
        or state not in DISPATCH_STATE_TONES
        or state not in DISPATCH_STATE_GLYPHS
    ]
    missing += [
        f"RESULT_{result}"
        for result in BATTLE_RESULTS
        if result not in BATTLE_RESULT_LABELS
        or result not in BATTLE_RESULT_TONES
        or result not in BATTLE_RESULT_GLYPHS
    ]
    missing += [
        f"KIND_{kind}"
        for kind in TARGET_KIND_LABELS
        if kind not in TARGET_KIND_TONES or kind not in TARGET_KIND_GLYPHS
    ]
    return missing
