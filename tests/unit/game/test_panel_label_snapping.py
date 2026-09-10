"""面板标签贴词：阈值放宽到 2 会让这个函数**恒返回 None**。

⚠️ **这条守的是一个真出过事的 bug。** 2026-09-10 夜里实测：逐格 OCR 读到的
是干干净净的 `'回收'`，而 `snap_panel_label('回收')` 返回 `None` —— 于是每一颗
星球都被判成「没有回收按钮 ⇒ 这颗没残骸」，**回收一发都派不出去**。

病根是词表**每一项都是两个汉字**，而两个两字词之间的编辑距离最大就是 2。
阈值取 2 ⇒ 任何两字输入同时命中全部六项 ⇒「唯一命中」规则判成歧义 ⇒ None。
"""

from __future__ import annotations

import pytest

from evo_helper.game.pirate_ui import PANEL_ACTION_LABELS, snap_panel_label


@pytest.mark.parametrize("label", PANEL_ACTION_LABELS)
def test_every_label_snaps_to_itself(label: str) -> None:
    """⚠️ **读得一字不差的标签必须贴得回去。** 这是最低要求，v1 连这个都不满足。"""
    assert snap_panel_label(label) == label


def test_one_character_wobble_still_snaps() -> None:
    """一个字的 OCR 抖动仍要认得出 —— 实测「回收」被读成过「回叙」。"""
    assert snap_panel_label("回叙") == "回收"


def test_garbage_does_not_snap() -> None:
    """贴不上就返回 None，**不许**随便挑一个最像的。"""
    assert snap_panel_label("以而") is None
    assert snap_panel_label("TARR") is None
    assert snap_panel_label("") is None


def test_widening_the_threshold_makes_it_always_ambiguous() -> None:
    """⚠️ **阈值为什么不能是 2**：两字词表下，2 会让每个输入都命中全部六项。

    这条用例把「为什么」钉死在代码里 —— 下一个人想放宽阈值时会先撞到它。
    """
    for label in PANEL_ACTION_LABELS:
        assert snap_panel_label(label, max_distance=2) is None, (
            f"{label!r} 在阈值 2 下本应因歧义返回 None；若这条挂了说明词表变了，阈值的上界要重新算"
        )
