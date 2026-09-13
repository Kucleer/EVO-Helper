"""列表页那一行的**框**：离网格量怎么交出来，自对齐重读框在哪。

## 这份用例守的是什么

2026-09-12 那一夜，65 分钟里开封 58 封、白开 50 封；58 封里 **55 封的主题读数
连「攻击报告」「海盗」字样都没有**，读成 `'TTT     seesere'` / `'一一 band a |
rt rm kar Ae'` 这种噪声。主题闸（`MailRow.may_be`）因此拦不住，未读必开那一条
就把它们开了。

离线在七屏实拍 42 行上量出来的根因**不是配方**（框宽 / 放大倍数 / 二值化），
是**框的纵向原点**：

- 一行的文字只占时刻带顶的 −30..+9（`MAIL_SUBJECT_INK_DY` 那三段，39 像素），
  全挤在行的上三分之一；名义行 ROI 高 85、行距 86，空出来的 46 像素全在文字**下方**。
- ⇒ **向上一个像素的余量都没有。** 列表一离网格，主题就被 ROI 上沿横着切掉。
- 实测：偏移 ≥ 30 的 18 行认出 17 行；偏移 < 30 的 24 行只认出 5 行，而那 5 行
  （偏移 +10 / +11）读到的还是**下一行**的主题。

⚠️ **`ReportLayout.mail_row` 与 `mail_rows()` 这次一个字都没动。** 本文件验的是
取证那一侧新加的两样东西（离网格量、自对齐重读），生产的读法照旧走名义 ROI。
换不换框要等实机裁片，见
`tools.pirate_loop.PirateLoop._record_unreadable_subject_evidence`。

⚠️ 合成图只验几何，不验「读得准」——后者只有实拍能回答，那张表在
`mail_row_aligned` 的 docstring 上。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.vision.report_layout import LIVE_LAYOUT
from support.mailbox import BASE_TIME_BAND_OFFSET, build_mail_list_screens

pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")


# -- 离网格量 --------------------------------------------------------------------


def test_the_offset_is_the_time_band_top_minus_the_nominal_row_top() -> None:
    """交出来的就是那个差值，逐行一个，下标跟着 `mail_rows()` 走。"""
    offsets = build_mail_list_screens(set()).mail_title_band_offsets()

    assert len(offsets) == LIVE_LAYOUT.mail_visible_rows
    assert set(offsets) == {BASE_TIME_BAND_OFFSET}


def test_the_offset_follows_the_list_off_the_grid() -> None:
    """列表整体漂多少，这个数就跟着变多少——它就是拿来量「漂了多少」的。"""
    offsets = build_mail_list_screens(set(), drift=+24).mail_title_band_offsets()

    assert set(offsets) == {BASE_TIME_BAND_OFFSET + 24}


def test_a_row_without_a_time_band_has_no_offset() -> None:
    """⚠️ 定位不到就交回 `None`，**不回落到名义行顶（也就是 0）**。

    0 是一个**合法而且最坏**的偏移（主题整条被切掉），回落等于把「没量到」
    写成「量到了，而且正是最糟的那一档」。取证记录里这两件事必须分得开：
    一个说「这一行的框错了」，另一个说「这一行连锚点都没找到」，处置不同。
    """
    offsets = build_mail_list_screens(set(), time_column=False).mail_title_band_offsets()

    assert set(offsets) == {None}


# -- 自对齐重读的框 ---------------------------------------------------------------


def _captured_region(screens: Any, index: int, monkeypatch: pytest.MonkeyPatch) -> Any:
    """截下 `mail_row_aligned` 真正喂给 OCR 的那块 ROI。**不跑 OCR。**

    这一层要验的是「框在哪」，而 OCR 读出什么是实拍才答得了的问题；让合成图上的
    OCR 参与断言，等于让一条读不出任何字的通路给出「通过」。
    """
    seen: list[Any] = []

    def fake_read(self: Any, region: Any, psm: int, **kwargs: Any) -> str:
        seen.append((region, psm))
        return ""

    monkeypatch.setattr(type(screens), "_read", fake_read)
    screens.mail_row_aligned(index)
    return seen[0]


def test_the_aligned_roi_hangs_off_this_row_s_own_time_band(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上沿 = 这一行的时刻带顶 − `MAIL_ROW_ALIGNED_DY`，高度与横向照抄名义行。

    ⚠️ **只换纵向原点，别的一个都不换。** 框宽、放大、psm 全部照抄 `mail_rows()`
    是这条取证成立的前提：一次只换一个变量，交回来的读数才说得清是哪一个在起作用。
    """
    from evo_helper.vision.optional.report_screens import MAIL_ROW_ALIGNED_DY, OCR_PSM_COLUMN

    screens = build_mail_list_screens(set(), drift=+24)
    nominal = LIVE_LAYOUT.mail_row(2)
    band_top = nominal.top + BASE_TIME_BAND_OFFSET + 24

    region, psm = _captured_region(screens, 2, monkeypatch)

    assert region.top == band_top - MAIL_ROW_ALIGNED_DY
    assert region.bottom - region.top == nominal.bottom - nominal.top
    assert (region.left, region.right) == (nominal.left, nominal.right)
    assert psm == OCR_PSM_COLUMN


def test_a_row_without_a_time_band_is_not_re_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有锚点就交回 `None`，**而且一次 OCR 都不花**。

    退化成「那就按名义行顶读一遍」是没有意义的：那正是已经读过、而且读不出来的
    那一次。多花的是几百毫秒 × 每一行，直接加在开封循环上。
    """
    screens = build_mail_list_screens(set(), time_column=False)
    reads: list[Any] = []
    monkeypatch.setattr(
        type(screens), "_read", lambda self, region, psm, **kw: reads.append(region) or ""
    )

    assert screens.mail_row_aligned(0) is None
    assert reads == []


# -- 那个常量为什么是 48 -----------------------------------------------------------


def test_the_aligned_offset_lands_inside_the_window_its_own_measurements_imply() -> None:
    """⚠️⚠️ **这一条把 `MAIL_ROW_ALIGNED_DY` 钉在它的推导上，不是钉在 48 上。**

    48 不是调出来的，是从三段墨迹 + 行高 + 行距算出来的窗口里挑的。谁哪天改了
    墨迹常量、行高或者行距而没有重算这个偏移，这条会当场红——而不是等到实机上
    再白开五十封。

    四道边界（`dy` = ROI 上沿到时刻带顶的距离）：

    - 自己的主题不许被上沿切掉：``dy ≥ −subject_top``
    - 自己的时刻不许被下沿切掉：``dy + time_bottom ≤ 行高``
    - 下一行的主题不许挤进来：``dy + (行距 + subject_top) ≥ 行高``
    - 上一行的时刻不许挤进来：``dy − (行距 − time_bottom) ≤ 0``
    """
    from evo_helper.vision.optional.report_screens import (
        MAIL_ROW_ALIGNED_DY,
        MAIL_SUBJECT_INK_DY,
        MAIL_TIME_INK_DY,
    )

    dy = MAIL_ROW_ALIGNED_DY
    subject_top = MAIL_SUBJECT_INK_DY[0]
    time_bottom = MAIL_TIME_INK_DY[1]
    row = LIVE_LAYOUT.mail_row(0)
    height = row.bottom - row.top
    pitch = LIVE_LAYOUT.mail_row_pitch

    assert dy >= -subject_top, "自己的主题会被 ROI 上沿切掉"
    assert dy + time_bottom <= height, "自己的时刻会被 ROI 下沿切掉"
    assert dy + (pitch + subject_top) >= height, "下一行的主题会挤进这一框"
    assert dy - (pitch - time_bottom) <= 0, "上一行的时刻会挤进这一框"


def test_the_nominal_roi_has_no_headroom_at_all() -> None:
    """⚠️⚠️ **根因本身：名义行 ROI 向上的余量是 0。**

    原先 `_mail_time_bands` 的注释写着「`mail_rows()` 的 ROI 有 85px 高，装得下
    这点漂移」。装不下——文字只占上面那 39 像素，空的 46 像素全在文字下方。
    这一条把那个「装得下」换成算得出来的数：名义框上沿到主题墨迹上沿的距离，
    就是列表最多能往上漂多少而主题还完整，而它等于 `−subject_top` 减去
    合成图上那个基准偏移能给的余量。
    """
    from evo_helper.vision.optional.report_screens import (
        MAIL_SENDER_INK_DY,
        MAIL_SUBJECT_INK_DY,
        MAIL_TIME_INK_DY,
    )

    row = LIVE_LAYOUT.mail_row(0)
    height = row.bottom - row.top
    content = MAIL_TIME_INK_DY[1] - MAIL_SUBJECT_INK_DY[0]

    # 三段首尾相接，中间没有断档——也就是说 39 像素是**整行文字**的全部高度。
    assert MAIL_SUBJECT_INK_DY[1] == MAIL_SENDER_INK_DY[0]
    assert MAIL_SENDER_INK_DY[1] > MAIL_TIME_INK_DY[0]
    assert content == 39
    # 而行高是 85：空出来的 46 像素**一个都不在文字上方**，
    # 因为名义框对齐的是行顶、文字从行顶下面十几像素就开始了。
    assert height - content == 46


def test_the_offset_below_thirty_is_exactly_the_broken_case() -> None:
    """合成图的默认偏移就是出事的那一档——**夹具本身站在故障那一侧**。

    这样写是为了让「偏移 < 30 会把主题切掉」这条事实有一个每次都跑的落点：
    默认画出来的那一屏，主题墨迹上沿落在名义框**外面**。
    """
    from evo_helper.vision.optional.report_screens import MAIL_SUBJECT_INK_DY

    subject_top_in_row = BASE_TIME_BAND_OFFSET + MAIL_SUBJECT_INK_DY[0]

    assert subject_top_in_row < 0, "夹具画的是对得上网格的那一屏，守不住根因"
    assert BASE_TIME_BAND_OFFSET + 24 + MAIL_SUBJECT_INK_DY[0] >= 0, "drift=+24 那一档应当是好的"


# -- 定位只扫一次 -----------------------------------------------------------------


def test_the_time_bands_are_located_once_per_frame() -> None:
    """⚠️ 定位结果缓存在实例上。**一个实例只读一屏，所以这个答案是常量。**

    这不是省时间的优化，是省一整趟信箱：定位要在纯 Python 里逐行数墨迹
    （列表区 ~560 行 × 140 列），而现在问它的地方有四处。不缓存的话，加一处取证
    就等于给每一屏再加一遍全扫描，而一趟要翻八屏。
    """
    screens = build_mail_list_screens({0, 1})
    first = screens._mail_time_bands()

    # 把像素抽掉：再扫一次必然扫出一个不同（空）的答案，除非它根本没再扫。
    screens._image = screens._image.copy()
    screens._image.paste((0, 0, 0), (0, 0, screens._image.width, screens._image.height))

    assert screens._mail_time_bands() == first
