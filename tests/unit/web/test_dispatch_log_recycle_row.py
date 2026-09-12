"""派遣日志上回收那一行。

⚠️⚠️ **这是同一个坑的第三次。** 「某一类发次永远不会产生战报，而那一格
问的是『有没有战报』」——

1. 2026-08-13：111 发**侦察**全部挂着「待战报」，用户连提两次「战果列状态没更新」。
2. `#320`：未读战报卡上 23 发「到点未读」里 16 发是**回收**（七成是假的）。
3. `#321`：周期统计的「攻击战报」把**撞保护期**那种合成战报也算了进去。

这一页是第四处。回收永远不会有战报（残骸捞回来只有一封回收报告邮件，
不进 `battle_reports`），所以它必须走自己那两档。
"""

from __future__ import annotations

import re
from pathlib import Path

from evo_helper.web.display import (
    RECYCLE_RESULT_BACK,
    RECYCLE_RESULT_GLYPHS,
    RECYCLE_RESULT_LABELS,
    RECYCLE_RESULT_TONES,
    RECYCLE_RESULT_WAITING,
)

TEMPLATE = (
    Path(__file__).resolve().parents[3] / "src" / "evo_helper" / "web" / "templates" / "logs.html"
)


def test_the_two_recycle_states_are_named_the_way_the_user_asked() -> None:
    """用户口径 2026-09-12：「回收可以使用待回收，读到邮件后，显示已回收」。"""
    assert RECYCLE_RESULT_LABELS[RECYCLE_RESULT_WAITING] == "待回收"
    assert RECYCLE_RESULT_LABELS[RECYCLE_RESULT_BACK] == "已回收"


def test_every_recycle_state_has_a_tone_and_a_glyph() -> None:
    """⚠️ 色永远配一个字形和一个词（全站规矩）。

    漏一档不会报错，只会让那一格**没有颜色也没有图标**——而它旁边那些
    有色有形的 chip 会让人以为这一行「没状态」。
    """
    for state in (RECYCLE_RESULT_WAITING, RECYCLE_RESULT_BACK):
        assert state in RECYCLE_RESULT_LABELS
        assert state in RECYCLE_RESULT_TONES
        assert state in RECYCLE_RESULT_GLYPHS


def test_the_recycle_row_never_falls_through_to_the_awaiting_report_branch() -> None:
    """⚠️⚠️ **源码级断言：回收必须在「没战报 → 待战报」之前分流出去。**

    这一条只能从源码上钉：两种写法**渲染出来看着都正常**——回收行确实
    「还没有结果」，写「待战报」也像那么回事。分叉只发生在**永远**这个字上：
    战报不会来，而回收报告邮件会。

    实测（2026-09-12 生产库）：回收行的 `battle_reports` 外连接恒为 NULL，
    所以一旦落到那条兜底分支，**每一行回收都会永久显示「待战报」**。
    """
    html = TEMPLATE.read_text(encoding="utf-8")

    recycle_at = html.index("entry.mission_kind == 'RECYCLE'")
    awaiting_at = html.index("result_labels.get('AWAITING'")

    assert recycle_at < awaiting_at, "回收那一档排在「待战报」兜底之后，会被它吃掉"


def test_the_recycle_branch_is_driven_by_the_haul_flag_not_by_the_report() -> None:
    """⚠️ 两档之间切换的判据必须是**实收读回来了没有**，不是「有没有战报」。

    写成 `entry.report_received` 的话，「已回收」这一档**永远不会出现**——
    回收发拿不到战报。而那种写法照样全绿：现在恒为「待回收」，
    和正确实现在**今天**的表现一模一样，只有邮件链路上线那天才分家。
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    block = html[html.index("entry.mission_kind == 'RECYCLE'") :][:600]

    assert "entry.recycle_haul_back" in block
    assert "report_received" not in block, "回收那一档的判据串到战报上去了"


def test_waiting_is_the_quiet_state_and_back_is_the_coloured_one() -> None:
    """「待回收」是常态，不该上色——每行都挂个 chip 只会让真收到的那几行不显眼。

    同侦察那一档的取舍（`SCOUT_RESULT_TONES` 里 WAITING 也是空串）。
    """
    assert RECYCLE_RESULT_TONES[RECYCLE_RESULT_WAITING] == ""
    assert RECYCLE_RESULT_TONES[RECYCLE_RESULT_BACK] == "kind-recycle"


def test_the_template_map_covers_both_states() -> None:
    """⚠️ 模板里那三张 `.get()` 的兜底是**状态名本身**（`RECYCLE_WAITING`）。

    漏一档不会 KeyError，会在页面上显示一个没人翻译过的英文常量。
    所以这里按名字核一遍，而不是指望 Jinja 报错。
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    block = html[html.index("entry.mission_kind == 'RECYCLE'") :][:800]

    assert re.search(r"recycle_labels\.get\(", block)
    assert re.search(r"recycle_tones\.get\(", block)
    assert re.search(r"recycle_glyphs\.get\(", block)
    assert "'RECYCLE_BACK'" in block and "'RECYCLE_WAITING'" in block
