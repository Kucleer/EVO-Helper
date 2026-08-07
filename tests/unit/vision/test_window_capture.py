from __future__ import annotations

import pytest

from evo_helper.vision.optional.window_capture import (
    BLANK_UNIQUE_COLOURS,
    PW_RENDERFULLCONTENT,
    WindowInfo,
    _is_blank,
)

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")


class TestWindowGeometry:
    def test_size_comes_from_the_rect(self) -> None:
        window = WindowInfo(handle=1, title="EVO", rect=(100, 50, 1300, 950))
        assert (window.width, window.height) == (1200, 900)

    def test_offscreen_rect_still_has_a_positive_size(self) -> None:
        """Chrome can sit on a secondary monitor at negative coordinates."""
        window = WindowInfo(handle=1, title="EVO", rect=(-1928, -8, 8, 1048))
        assert (window.width, window.height) == (1936, 1056)


class TestBlankDetection:
    def test_uniform_image_is_blank(self) -> None:
        """PrintWindow returns a flat bitmap when it cannot reach the compositor."""
        assert _is_blank(Image.new("RGB", (32, 32), (255, 255, 255)))
        assert _is_blank(Image.new("RGB", (32, 32), (0, 0, 0)))

    def test_a_handful_of_colours_is_still_blank(self) -> None:
        image = Image.new("RGB", (32, 32), (0, 0, 0))
        image.putpixel((0, 0), (255, 255, 255))
        assert _is_blank(image)

    def test_a_real_screenshot_is_not_blank(self) -> None:
        image = Image.new("RGB", (64, 64))
        image.putdata([(x, y, (x * y) % 256) for y in range(64) for x in range(64)])
        assert not _is_blank(image)

    def test_threshold_is_small_enough_to_reject_only_flat_renders(self) -> None:
        assert BLANK_UNIQUE_COLOURS <= 8


def test_print_window_flag_requests_gpu_composited_content() -> None:
    """Without PW_RENDERFULLCONTENT, Chrome's WebGL canvas renders empty."""
    assert PW_RENDERFULLCONTENT == 0x00000002


class TestClientCrop:
    def test_crop_drops_the_non_client_border(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A whole-window render includes the drop shadow; the crop removes it."""
        from evo_helper.vision.optional import window_capture

        window = WindowInfo(handle=1, title="EVO", rect=(-7, -7, 1543, 831))
        monkeypatch.setattr(window_capture, "client_box", lambda _w: (0, 0, 1536, 824))
        image = Image.new("RGB", (window.width, window.height))

        cropped = window_capture._crop_client(image, window)

        assert cropped.size == (1536, 824)

    def test_crop_keeps_the_client_pixels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evo_helper.vision.optional import window_capture

        window = WindowInfo(handle=1, title="EVO", rect=(-7, -7, 13, 13))
        monkeypatch.setattr(window_capture, "client_box", lambda _w: (0, 0, 10, 10))
        image = Image.new("RGB", (20, 20), (0, 0, 0))
        # (7, 7) in window space is the client origin.
        image.putpixel((7, 7), (255, 0, 0))

        cropped = window_capture._crop_client(image, window)

        assert cropped.getpixel((0, 0)) == (255, 0, 0)
