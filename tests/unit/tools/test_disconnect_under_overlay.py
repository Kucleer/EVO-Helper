"""弹窗盖住时，掉线那句话在**另一个位置**——两块 ROI 都要读。

⚠️ **这条守的是一次整夜停摆。** 2026-09-11 01:45 实机：残骸框开着的时候
连接断了，「连接已断开，正在重新连接…」被画进了**那个框里面**（y≈563），
而 `DISCONNECT_TEXT_ROI` 的下边界是 500 —— **一个字都读不到**。

后果不是「少认一次」：会话守护看不见掉线 ⇒ 不关窗重开；而每一轮都死在
「切出发星球前回不到星球地表」，谁也没把这件事升级。实测 **42/45 轮**
这样空转了 80 分钟，一发攻击、一发回收都没有。
"""

from __future__ import annotations

from evo_helper.game.session_keeper import ScreenState, classify_screen
from evo_helper.tools.scan_coordinates import (
    DISCONNECT_TEXT_ROI,
    DISCONNECT_TEXT_ROI_OVERLAID,
)


def test_the_two_rois_do_not_overlap_vertically() -> None:
    """⚠️ 第二块必须落在第一块**下面** —— 它存在的理由就是第一块够不着。"""
    assert DISCONNECT_TEXT_ROI_OVERLAID[1] >= DISCONNECT_TEXT_ROI[3], (
        "被弹窗盖住时那句话在 y≈563，而 DISCONNECT_TEXT_ROI 到 500 就截止了；"
        "两块若重叠说明有人把第二块挪回了读不到的地方"
    )


def test_the_overlaid_roi_covers_the_measured_position() -> None:
    """实拍量出来的位置：y 550–578，x 820–1100。"""
    left, top, right, bottom = DISCONNECT_TEXT_ROI_OVERLAID
    assert top <= 563 <= bottom, "实拍那句话的基线在 y≈563，ROI 必须罩住它"
    assert left <= 865 and right >= 1055, "实拍那句话横跨 x≈865–1055"


def test_the_recoverable_wording_is_still_recognised() -> None:
    """框里读到的是**可恢复**那一档的文案 —— 判据本身认得出它。"""
    assert classify_screen("一一连接已断开，正在重新连接…") is ScreenState.DISCONNECTED


def test_the_dead_wording_still_wins_over_the_recoverable_one() -> None:
    """「无法重新连接」是「连接已断开」的超串，先判死的那一档（既有口径）。"""
    assert classify_screen("连接已断开，无法重新连接。") is ScreenState.DEAD_SESSION
