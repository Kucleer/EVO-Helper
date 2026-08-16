"""Display choices for the console that are not domain rules."""

from __future__ import annotations

from evo_helper.application.backfill import BACKFILL_KINDS, BackfillPhase
from evo_helper.domain.records import TARGET_KIND_LABELS
from evo_helper.domain.scheduler import TaskStatus

#: 三条任务链路在界面上的名字。
#:
#: 页面和桌面悬浮窗（`tools/scan_console.py`）都要显示「当前跑的是哪条链路」，
#: 而悬浮窗是个瘦客户端——它只认接口给的字符串。所以标签由服务端下发，
#: 两处不会各写一份、也就不会有一天对不上。
#:
#: ⚠️ **它是链路的名字，不是任务的名字。** 同一链路现在可以有多个任务
#: （多个 bot 攻击），每个任务自己带一个 `name`；这张表只在任务没起名时兜底。
MISSION_LABELS: dict[str, str] = {
    "PIRATE": "侦查+攻击海盗",
    "BOT": "扫描+攻击 bot",
    "SCAN": "扫描全星系 bot",
    "RANKING": "扫描军力榜",
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


#: 每一档状态各自的 chip 色调与字形。
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
    # 定时窗口这两档和「未启用」同色：它们同样是「用户自己配成不跑」，
    # 不是故障也不是警告。区别全靠那句话本身和字形。
    TaskStatus.BEFORE_WINDOW.value: "",
    TaskStatus.AFTER_WINDOW.value: "",
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
    # 沙漏的两个方向：还没到 = 沙子在上，已经过 = 沙子在下。
    TaskStatus.BEFORE_WINDOW.value: "⧗",
    TaskStatus.AFTER_WINDOW.value: "⧖",
}


#: 补录能补的两条链路在界面上的名字。键是 `application.backfill.BACKFILL_KINDS`
#: 的取值，也就是 CLI 的 `--kind`。接口与命令行一律英文，界面只显示中文。
BACKFILL_KIND_LABELS: dict[str, str] = {
    "pirate": "海盗战报",
    "bot": "bot 战报",
}

#: 补录六档状态各自的 chip 色调与字形。键是 `BackfillPhase` 的**每一个**成员。
#:
#: 「补录完成」与「补录失败」不能共用一格：失败意味着那批战报还躺在信箱里，
#: 而任务马上要拿这份仍然不全的数据去决定要不要再打一遍。
#:
#: 色永远配一个字形（见 `STATUS_TONES` 上那条）：控制台要在灰度下、对色盲用户
#: 也读得懂，所以 chip 里写的是那六个中文词，色只是加速。
BACKFILL_PHASE_TONES: dict[str, str] = {
    BackfillPhase.IDLE.value: "",
    BackfillPhase.PENDING.value: "warn",
    BackfillPhase.RUNNING.value: "ok",
    BackfillPhase.DONE.value: "ok",
    BackfillPhase.FAILED.value: "danger",
    BackfillPhase.CANCELLED.value: "",
}

BACKFILL_PHASE_GLYPHS: dict[str, str] = {
    BackfillPhase.IDLE.value: "○",
    BackfillPhase.PENDING.value: "◷",
    BackfillPhase.RUNNING.value: "▶",
    BackfillPhase.DONE.value: "★",
    BackfillPhase.FAILED.value: "✕",
    BackfillPhase.CANCELLED.value: "■",
}


def missing_backfill_phases() -> list[str]:
    """色调表或字形表里没有位置的补录状态。测试拿它当断言。

    同 `missing_status_tones()`：漏一档就意味着页面上会出现一个没有颜色、
    没有字形的 chip，而最可能漏的恰恰是新加的那一档。
    """
    return [
        phase.name
        for phase in BackfillPhase
        if phase.value not in BACKFILL_PHASE_TONES or phase.value not in BACKFILL_PHASE_GLYPHS
    ]


def missing_backfill_kind_labels() -> list[str]:
    """没有中文名的补录链路。页面上那个下拉框按它建，漏一条就少一个选项。"""
    return [kind for kind in BACKFILL_KINDS if kind not in BACKFILL_KIND_LABELS]


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
#: **现在是空的：列表只看舰队总数。**（用户口径 2026-08-11：「不读了吧，节约性能，
#: 在页面移除这4项，仅查看舰队总数」）
#:
#: 这四列原先是海盗侦察的四个判定舰种（`PIRATE_TRIGGER_SHIPS`）。移除的理由不是
#: 版面，是**数据源**：
#:
#: - **bot 那半边根本没有这四个数。** 逐舰种明细在战斗回放页上，而 bot 链路刻意
#:   只读详情页、`fleet_snapshots` 一行不写（见 `tools.bot_loop` 模块头）。要补上
#:   得多点开一次「查看战斗回放」——那个按钮至今没有标定过的坐标，每份报告还要多
#:   花两三秒 OCR。用户选择不为这四个数付这笔钱。
#: - 海盗那半边有（`scout_trigger_ships`），但其中 `收割者` 一列在实机 98 份报告里
#:   **一份都没读出来**（ROI 落空），摆在列表上也是满屏的「—」。
#:
#: 留成空元组而不是删掉这个常量：取数与渲染那条路仍然按它来，哪天回放页标定好了，
#: 这里填回去就有列。**别改成 `or 0`**——`None` 是「没读到」，`0` 是「真的没有」，
#: 整个 ATTACK/SKIP/UNREADABLE 三值判定就建立在这个区分上。
LIST_SHIP_COLUMNS: tuple[str, ...] = ()


#: 情报中心列表里 bot 与海盗各自的 chip 样式。
#:
#: 两者要一眼分得开——它们是两条完全不同的链路（bot 走扫描+直接 BBB 攻击+平局重打，
#: 海盗走侦察+判定+攻击），混在一张表里读的时候，最先要回答的就是「这行是哪种」。
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


#: 发次类型（侦察 / 攻击）在攻击日志上的 chip 样式。
#:
#: 用户口径（2026-08-14）：「预设中的侦查和攻击需要标记不同颜色」。两种发次在
#: 那一页混排，而它们的**下一步完全不同**——侦察等的是侦察报告、攻击等的是战报，
#: 分不出来就没法读那一列（见 `SCOUT_RESULT_*`）。
#:
#: 色永远配一个字形和一个词（同 `STATUS_TONES` 的规矩）。
MISSION_KIND_LABELS: dict[str, str] = {"SCOUT": "侦察", "ATTACK": "攻击"}
MISSION_KIND_TONES: dict[str, str] = {"SCOUT": "kind-scout", "ATTACK": "kind-attack"}
MISSION_KIND_GLYPHS: dict[str, str] = {"SCOUT": "◎", "ATTACK": "⚔"}


#: 侦察发那一格的三档。**侦察发永远不该显示「待战报」。**
#:
#: 实机 2026-08-13 通宵：111 发侦察在攻击日志上全部挂着「待战报」，而侦察根本
#: 不产生战报——它产出的是侦察报告，走 `scout_reports` 那张表。用户连提两次
#: 「战果列状态没更新」，成因就是这一格问错了问题（详见
#: `web.service.AttackLogView.mission_kind`）。
#:
#: 只分「回来了没有」两档，**不显示判定结论**：判定要拿四个舰种数现算
#: （`domain.scout_verdict.verdict_of_record`），而那几个数在这一页上没有；
#: 硬要显示就得把判定逻辑在这里再写一份，而两份判据迟早分家。想看判定去情报中心。
SCOUT_RESULT_BACK = "SCOUT_BACK"
SCOUT_RESULT_WAITING = "SCOUT_WAITING"

SCOUT_RESULT_LABELS: dict[str, str] = {
    SCOUT_RESULT_BACK: "侦察已回",
    SCOUT_RESULT_WAITING: "待侦察报告",
}
SCOUT_RESULT_TONES: dict[str, str] = {SCOUT_RESULT_BACK: "ok", SCOUT_RESULT_WAITING: ""}
SCOUT_RESULT_GLYPHS: dict[str, str] = {SCOUT_RESULT_BACK: "◉", SCOUT_RESULT_WAITING: "◌"}


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
