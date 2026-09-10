"""读残骸框标题**必须二值化**，否则读出来是拉丁乱码。

## 为什么

2026-09-11 整夜，回收链路上 `screen_error` 8 次，日志里标题读到的只有两个串：
`'UF HK'`（3 次）、`'UF A'`（3 次）。我据此推断「残骸框根本没打开」，
并按这个推断改了点击坐标（`#315`）。

⚠️ **推断错了。** `#314` 的取证图拍到之后一眼就看见：**框好好地开着**，
标题「回收残骸」清清楚楚，绿✓ 红✗ 都在，读字 ROI 也框得正正好好。

拿那张原分辨率裁片把配方逐个跑了一遍：

    upscale=3（`_read` 的默认）  → 'UF A'      ← 日志里那串，精确复现
    upscale=4                    → 'UF'
    upscale=5 / 6                → 'DUI' / 'EIU'
    upscale=4, threshold=120     → '回收残徽'  ✅
    upscale=4, threshold=150     → '回收残航'  ✅

⇒ **真因是没有二值化**（浅色字压在蓝色渐变上），不是点击、不是 ROI、不是重试次数。
**光加 upscale 反而越放越糟。**
"""

from __future__ import annotations

import inspect

from evo_helper.game import pirate_ui


def test_the_recipe_binarises() -> None:
    """⚠️⚠️ **`threshold` 是这条配方存在的全部理由。**

    去掉它就退回 `'UF A'` —— 而那**不会报错**，只会让回收在残骸框那一步
    静默失败，然后以「框没弹出来、可能点错了按钮」的面目出现在日志里，
    把排障引向点击和 ROI（我就是这么被引偏的）。
    """
    recipe = pirate_ui.RECYCLE_DIALOG_TITLE_RECIPE
    assert recipe.get("threshold") is not None, (
        "标题配方没有 threshold —— 不二值化读出来是 'UF A'，而且不报错"
    )


def test_the_recipe_upscales_beyond_the_default() -> None:
    """`_read` 的默认 upscale 是 3，实测 3 读不出来；配方要显式抬高。"""
    assert pirate_ui.RECYCLE_DIALOG_TITLE_RECIPE.get("upscale", 0) >= 4


def test_the_dialog_title_read_uses_the_recipe() -> None:
    """⚠️ **源码级：等残骸框那一读必须带上配方。**

    漏传不报错 —— `_read` 有默认值，跑起来一切正常，只是一部分回收静默失败。
    """
    from evo_helper.tools.bot_loop import BotLoop

    source = inspect.getsource(BotLoop._wait_for_recycle_dialog)
    assert "RECYCLE_DIALOG_TITLE_RECIPE" in source, (
        "读标题没带配方 —— 会退回 upscale=3 无阈值，也就是 'UF A'"
    )


def test_fuzzy_matching_still_covers_the_last_character() -> None:
    """⚠️ 二值化之后仍然会错一个字，模糊匹配（`#311`）必须还在。

    实测两次分别读成「回收残徽」「回收残航」——**都不是**「回收残骸」。
    换回精确子串的话，这个 PR 修完照样全灭。
    """
    for reading in ("回收残徽", "回收残航", "回收残仍", "回收残骸"):
        assert pirate_ui.looks_like_recycle_dialog(reading), reading
