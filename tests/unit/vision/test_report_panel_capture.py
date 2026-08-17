"""战报存档图：ROI 覆盖了什么，编码成什么。

这一块 ROI 不喂 OCR，是给人看的——攻击日志上点开来确认「这一发到底打的是谁、
打成了什么」。所以它的判据和别处那些窄 ROI 正好相反：**宁可多截一点，也别切掉
数据**。下面钉的就是「多」这件事：ROI 必须把量过的那几块地标整个包进去，而且
四周都要有余量（面板高度会随内容变）。
"""

from __future__ import annotations

import pytest

from evo_helper.vision.report_layout import LAYOUT_VIEWPORT, LIVE_LAYOUT, Region

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

PANEL = LIVE_LAYOUT.report_panel


def _contains(outer: Region, inner: Region, *, margin: int = 0) -> bool:
    return (
        outer.left <= inner.left - margin
        and outer.top <= inner.top - margin
        and outer.right >= inner.right + margin
        and outer.bottom >= inner.bottom + margin
    )


class TestWhatTheRoiCovers:
    def test_it_covers_the_header_with_the_subject_and_the_report_time(self) -> None:
        """页眉里有主题（哪一类战报）和报告时间，两样都是认这张图的凭据。"""
        assert _contains(PANEL, LIVE_LAYOUT.report_header)
        assert _contains(PANEL, LIVE_LAYOUT.report_time)

    def test_it_covers_the_versus_block_with_both_coordinates(self) -> None:
        """⚠️ **双方名称与两侧坐标是这张图的主要用途。**

        用户口径（2026-08-17）：截图要从「战报」标题（含双方名称与两侧坐标）起。
        少了坐标，攻击日志上点开的就是一张认不出目标的图。
        """
        assert _contains(PANEL, LIVE_LAYOUT.detail_versus)
        assert _contains(PANEL, LIVE_LAYOUT.detail_attacker_coordinate)
        assert _contains(PANEL, LIVE_LAYOUT.detail_defender_coordinate)

    def test_it_covers_the_outcome_banner(self) -> None:
        from evo_helper.vision.pirate_reports import OUTCOME_ROI

        assert _contains(PANEL, OUTCOME_ROI)

    def test_it_reaches_past_the_combat_details_rows(self) -> None:
        """一直到「战斗详情」下面那两行数据。

        那个横幅的位置是**运行时按亮带找的**（面板会滚动），所以这里只能钉住
        「ROI 下沿必须探到参战区之下」——实拍上「战斗详情」横幅在参战区下界
        （750）附近，两行数值再往下 20–45 像素。
        """
        _top, participating_bottom = LIVE_LAYOUT.participating_rows

        assert PANEL.bottom > participating_bottom + 45

    def test_it_leaves_slack_around_the_measured_landmarks(self) -> None:
        """⚠️ **余量是刻意留的，不是量得不准。**

        面板高度会随内容变（舰队回收百分比、战斗详情行数）。贴着量出来的边裁，
        换一份内容更长的战报就会切掉数据；而这张图切掉一半没人会回头补。
        """
        assert _contains(PANEL, LIVE_LAYOUT.report_header, margin=15)
        assert _contains(PANEL, LIVE_LAYOUT.detail_versus, margin=15)

    def test_it_stays_inside_the_calibrated_viewport(self) -> None:
        """越界的裁剪在 Pillow 上不报错，只是补一块黑边——安静地错。"""
        width, height = LAYOUT_VIEWPORT

        assert 0 <= PANEL.left < PANEL.right <= width
        assert 0 <= PANEL.top < PANEL.bottom <= height


class TestEncoding:
    def _screens(self):  # type: ignore[no-untyped-def]
        import random

        from evo_helper.vision.optional.report_screens import ImageReportScreens

        # 标定尺寸的一张**有细节**的图。纯色不行：有损编码在纯色上不随质量单调，
        # 「质量参数有没有接上」那条会被一张平的图骗过去。种子固定，结果可复现。
        rng = random.Random(20260817)  # noqa: S311 - 造测试图，不是密码学用途
        width, height = LAYOUT_VIEWPORT
        image = Image.frombytes("RGB", LAYOUT_VIEWPORT, rng.randbytes(width * height * 3))
        return ImageReportScreens(image, LIVE_LAYOUT)

    def test_the_crop_matches_the_declared_roi(self) -> None:
        panel = self._screens().report_panel_image()

        assert (panel.width, panel.height) == (
            PANEL.right - PANEL.left,
            PANEL.bottom - PANEL.top,
        )

    def test_it_encodes_webp(self) -> None:
        """WEBP q90：实测这个尺寸下约 39 KB/张。PNG 是它的好几倍。"""
        panel = self._screens().report_panel_image()

        assert panel.image_format == "webp"
        assert panel.image_bytes[:4] == b"RIFF"
        assert panel.image_bytes[8:12] == b"WEBP"

    def test_the_bytes_decode_back_to_the_same_size(self) -> None:
        from io import BytesIO

        panel = self._screens().report_panel_image()

        decoded = Image.open(BytesIO(panel.image_bytes))
        assert decoded.size == (panel.width, panel.height)

    def test_a_lower_quality_makes_a_smaller_file(self) -> None:
        """质量参数真的接上了编码器——写死 90 却传不进去是那种全绿的错法。"""
        screens = self._screens()

        assert len(screens.report_panel_image(quality=10).image_bytes) < len(
            screens.report_panel_image(quality=95).image_bytes
        )
