"""点面板图标的 y **必须落在图标上**，不许落进图标和标签之间的空隙。

## 为什么

本文件守的是一个**犯过两次**的错误。

第一次（`pirate_ui.BOT_ATTACK_BUTTON` 的注释里记着）：照主面板的
`ATTACK_BUTTON` 往 bot 面板上点，`(1032, 540)` 落在图标排和舰船格之间的
**空白处** —— 什么都没发生，接着去读预设条自然是噪声，bot 链路从来没派出过一发。

第二次（2026-09-11 整夜）：回收点的是 `label_y - 15 = 420`，
而实拍量得图标亮区是 **362…416**、标签文字 **429…440** ——
**420 正卡在中间的空隙上**。于是 `dispatched` 7 次、`screen_error` 6 次，
失败那些读到 `'UF HK'` / `'UF A'`：残骸框根本没开，标题 ROI 读到了底层面板。

⚠️ **两次的共同点：点空隙不会报错。** 点下去什么都没发生，
链路继续往下走，然后在**别的地方**以别的面目失败 —— 上一次是「找不到预设」，
这一次是「残骸框没弹出来」。所以这条判据得由用例守着。
"""

from __future__ import annotations

from evo_helper.game import pirate_ui


def test_the_attack_button_sits_on_the_icon() -> None:
    """攻击按钮的 y 在图标亮区里 —— 它是这一排唯一有生产实绩的点位。"""
    lo, hi = pirate_ui.BOT_PANEL_ICON_Y_RANGE
    assert lo <= pirate_ui.BOT_ATTACK_BUTTON[1] <= hi


def test_the_label_row_is_not_on_the_icon() -> None:
    """标签行**不在**图标上 —— 点标签不等于点图标，这就是两者必须分开的理由。"""
    lo, hi = pirate_ui.BOT_PANEL_ICON_Y_RANGE
    label_y = pirate_ui.BOT_ATTACK_BUTTON[1] + pirate_ui.BOT_PANEL_LABEL_Y_OFFSET
    assert not (lo <= label_y <= hi)

    top, bottom = pirate_ui.BOT_PANEL_LABEL_Y_RANGE
    assert top > hi, "标签行 ROI 与图标亮区重叠了，逐格读会读进图标"


def test_the_old_click_point_was_in_the_dead_gap() -> None:
    """⚠️ 把「420 是坏的」钉死。

    这个数看着很像「图标下沿再往上一点」，改回去不会报错、跑流程也看不出来 ——
    只会让一半的回收静默失败。
    """
    lo, hi = pirate_ui.BOT_PANEL_ICON_Y_RANGE
    old = pirate_ui.BOT_ATTACK_BUTTON[1] + pirate_ui.BOT_PANEL_LABEL_Y_OFFSET - 15

    assert old == 420
    assert not (lo <= old <= hi), "420 又落回图标里了？那说明图标亮区被改过，请重新实拍量"


def test_the_recycle_click_uses_the_icon_row_y() -> None:
    """⚠️⚠️ **源码级：回收点击的 y 必须来自 `BOT_ATTACK_BUTTON`。**

    同一排的图标没有理由用两个不同的 y。写成任何自己算的偏移量都会重蹈覆辙，
    而且**不报错** —— 见模块 docstring 里那两次。
    """
    import inspect

    from evo_helper.tools.bot_loop import BotLoop

    source = inspect.getsource(BotLoop._recycle_once)
    assert (
        'self._driver.click(recycle_x, pirate_ui.BOT_ATTACK_BUTTON[1], label="回收")' in source
    ), "回收点击没用攻击按钮那一行的 y —— 自己算偏移量会点进空隙，静默失败"
