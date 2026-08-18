"""关浮层之前先认出那个 ✕；认不出就**一下都不点**。

事故（实机 2026-08-18 10:04:05 与 10:05:08 各一次）：`dismiss_overlays` 在固定
像素 (750, 71) 上盲点最多 4 下，不看那儿是什么。那一刻画面上是**军力排行榜面板**，
4 下全落进了榜单里。用户口径：「点 4 下关闭，应校验按钮形态，不然就会点到排行榜中去」。

而 (750, 71) 上没有浮层时坐着的**不是「什么都没有」**：恒星系视图上它压在导航栏
第一个输入框「银河系」里（`var/logs/atk-0-panel.png`），星球地表上是等级徽章那一格
（`var/logs/rank-closed.png`）。所以「点空无害」这句话从来就不成立。

判据是**图形**不是 OCR（那个 ✕ 不是字）。这里的每一块画面都是从实拍上量下来的
原样点阵，见 `tests/support/screens.py`。
"""

from __future__ import annotations

import pytest

from evo_helper.game.overlay import (
    OVERLAY_CLOSE_ATTEMPTS,
    OVERLAY_CLOSE_BUTTON,
    close_button_visible,
    dismiss_overlays,
    look_at_close_button,
)
from support.screens import (
    BACK_BUTTON_PATCH,
    CLOSE_BUTTON_PATCH,
    SHIFTED_CLOSE_BUTTON_PATCH,
    screen_all_white,
    screen_with,
    screen_without_overlay,
)


class _Driver:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int, str]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))

    def wait(self, _seconds: float) -> None:
        pass


# -- 判据本身 ------------------------------------------------------------------


def test_the_real_close_button_is_recognised() -> None:
    """实拍上那个 ✕（`var/logs/rankv/21-panel.png`）当然要认出来。"""
    assert close_button_visible(screen_with(CLOSE_BUTTON_PATCH))


def test_a_panel_sliding_in_is_still_recognised() -> None:
    """浮层还在滑入动画里、整块偏了几像素的那一帧也算数。

    量自 `var/logs/dump-mail-list-unrecognised-175927.png`：全部实拍里唯一一张
    「认出来了但不是逐像素相同」的，IoU 0.873。判据的下界就是被它钉住的——
    阈值再往上挪一点，这一张就掉出去了。
    """
    assert close_button_visible(screen_with(SHIFTED_CLOSE_BUTTON_PATCH))


def test_the_back_arrow_in_the_same_frame_is_not_a_close_button() -> None:
    """⚠️ **同一个按钮框里还坐过一个 «。**

    实拍里有 107 张（战报详情、简报、信箱详情，如 `var/logs/atk-4-dispatched.png`）
    在这个像素上放的是「返回」双箭头。外框一模一样，而「返回」和「关闭」在游戏里
    是两个动作——判据只认 ✕（« 的实测 IoU 只有 0.348）。
    """
    assert not close_button_visible(screen_with(BACK_BUTTON_PATCH))


def test_a_screen_without_any_overlay_is_not_a_close_button() -> None:
    assert not close_button_visible(screen_without_overlay())


def test_an_all_white_screen_is_not_a_close_button() -> None:
    """⚠️ **只看点阵重合是挡不住这一张的。**

    整屏泛白时（浏览器还在加载，`var/logs/rankv/00-baseline.png`）框里当然「全中」，
    IoU 恰好 167/306 = 0.546——离阈值 0.60 只差一点点。挡住它的是**静默环**：
    ✕ 是孤立的图形，紧贴它的那一圈上不该有白。
    """
    look = look_at_close_button(screen_all_white())

    assert look.quiet_ratio == pytest.approx(1.0)
    assert not look.visible


def test_the_two_readings_are_carried_out_for_the_log() -> None:
    """认不出时得说得出「看到了什么」，否则库里又是一句没有下文的话。"""
    payload = look_at_close_button(screen_with(BACK_BUTTON_PATCH)).as_payload()

    assert payload["close_button_visible"] is False
    assert 0.0 < payload["close_button_iou"] < 0.6, "« 该落在阈值外，而不是压根没读数"
    assert "close_button_quiet_ratio" in payload


# -- 点击循环 ------------------------------------------------------------------


def test_nothing_is_clicked_when_the_close_button_is_not_recognised() -> None:
    """**本文件的重点。** 认不出就一下都不点，并把这件事如实报出去。"""
    driver = _Driver()

    outcome = dismiss_overlays(driver, see_close_button=lambda: False)

    assert driver.clicks == []
    assert outcome.clicked == 0
    assert outcome.recognised is False


def test_a_recognised_close_button_is_clicked_as_before() -> None:
    """认出来了就照旧点——这道闸不该把正常的关浮层一起挡掉。"""
    driver = _Driver()

    outcome = dismiss_overlays(driver, see_close_button=lambda: True)

    assert driver.clicks == [(*OVERLAY_CLOSE_BUTTON, "关闭面板")] * OVERLAY_CLOSE_ATTEMPTS
    assert outcome.clicked == OVERLAY_CLOSE_ATTEMPTS
    assert outcome.still_visible is True


def test_the_clicking_stops_as_soon_as_the_close_button_is_gone() -> None:
    """关掉了就停手——原先不给 `is_clear` 时会闷头点满 4 下，多出来的那几下
    落在**已经没有浮层**的画面上，也就是落在导航输入框或等级徽章上。
    """
    driver = _Driver()
    looks = iter([True, True, False, False])

    outcome = dismiss_overlays(driver, see_close_button=lambda: next(looks))

    assert len(driver.clicks) == 2
    assert outcome.still_visible is False


def test_a_stacked_overlay_still_gets_more_than_one_click() -> None:
    """列表 → 详情这种套了两层的，第一下只退回列表，第二下才关掉。"""
    driver = _Driver()
    cleared = iter([False, True])

    outcome = dismiss_overlays(
        driver, see_close_button=lambda: True, is_clear=lambda: next(cleared)
    )

    assert len(driver.clicks) == 2
    assert outcome.recognised is True


def test_the_loop_is_bounded_even_when_the_overlay_never_closes() -> None:
    """关不掉的画面可能压根不是浮层（维护公告、界面改版），在上面无限点下去
    比停下来糟得多。
    """
    driver = _Driver()

    dismiss_overlays(driver, see_close_button=lambda: True, is_clear=lambda: False)

    assert len(driver.clicks) == OVERLAY_CLOSE_ATTEMPTS
