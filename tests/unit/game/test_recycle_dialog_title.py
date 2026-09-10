"""残骸框标题：一个字的 OCR 抖动**不许**把好屏判死。

⚠️ **这条守的是一个真出过事的 bug。** 2026-09-11 01:34 实测：
回收按钮找对了、点下去了、框**确实弹出来了**，OCR 读到 `'回收残仍'`
（「骸」被读成「仍」）。而 v1 的判据是精确子串 `"回收残骸" not in title`，
于是那一趟被判成「点错了按钮」—— 复位画面、作业结成 `screen_error`，
**而实际上一切正常，只差按一下绿✓**。
"""

from __future__ import annotations

import pytest

from evo_helper.game.pirate_ui import RECYCLE_DIALOG_TITLE, looks_like_recycle_dialog


@pytest.mark.parametrize(
    "raw",
    [
        RECYCLE_DIALOG_TITLE,
        "回收残仍",  # ⚠️ 实拍读数：「骸」→「仍」
        "回 收 残 仍",  # OCR 常把字距读成空格
        "回收残骸 11.2M",  # 框里的资源数被一起读进来
    ],
)
def test_the_dialog_is_recognised_despite_ocr_wobble(raw: str) -> None:
    assert looks_like_recycle_dialog(raw)


@pytest.mark.parametrize("raw", ["", "取消 发送", "发送给 bot_4_277_14", "未选择任何战舰"])
def test_other_screens_are_not_mistaken_for_it(raw: str) -> None:
    """⚠️ 松归松，**不许把发私信那个窗口认成残骸框** —— 那正是这道闸要挡的。"""
    assert not looks_like_recycle_dialog(raw)


def test_exact_substring_would_have_missed_the_real_reading() -> None:
    """把「为什么不能用精确子串」钉死：实拍那一次它就会漏。"""
    assert RECYCLE_DIALOG_TITLE not in "回收残仍"
    assert looks_like_recycle_dialog("回收残仍")
