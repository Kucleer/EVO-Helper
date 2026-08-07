from __future__ import annotations

import pytest

from evo_helper.vision.report_layout import (
    BINARIZE_THRESHOLD,
    LIVE_LAYOUT,
    ColumnBand,
    Region,
    layout_for_viewport,
)


class TestViewportGate:
    def test_returns_the_calibrated_layout(self) -> None:
        assert layout_for_viewport(1920, 879) is LIVE_LAYOUT

    def test_refuses_an_uncalibrated_viewport(self) -> None:
        with pytest.raises(ValueError, match="1920x879"):
            layout_for_viewport(1536, 647)

    def test_does_not_scale_geometry(self) -> None:
        """A scaled guess would silently truncate columns; it must raise."""
        with pytest.raises(ValueError):
            layout_for_viewport(3840, 1758)


class TestMailRows:
    def test_first_row_matches_the_measured_box(self) -> None:
        assert LIVE_LAYOUT.mail_row(0).as_box() == (700, 205, 1220, 290)

    def test_rows_advance_by_the_measured_pitch(self) -> None:
        first = LIVE_LAYOUT.mail_row(0)
        second = LIVE_LAYOUT.mail_row(1)
        assert second.top - first.top == LIVE_LAYOUT.mail_row_pitch
        assert second.left == first.left and second.right == first.right

    def test_clipped_row_is_not_addressable(self) -> None:
        with pytest.raises(IndexError):
            LIVE_LAYOUT.mail_row(LIVE_LAYOUT.mail_visible_rows)

    def test_negative_index_is_rejected(self) -> None:
        with pytest.raises(IndexError):
            LIVE_LAYOUT.mail_row(-1)


class TestColumns:
    def test_sides_do_not_overlap(self) -> None:
        assert LIVE_LAYOUT.attacker_column.right <= LIVE_LAYOUT.defender_column.left

    def test_participating_region_spans_the_measured_rows(self) -> None:
        region = LIVE_LAYOUT.participating(LIVE_LAYOUT.defender_column)
        assert region.as_box() == (960, 405, 1210, 750)

    def test_round_band_needs_a_located_banner(self) -> None:
        band = ColumnBand(720, 960)
        assert band.rows(800, 900).as_box() == (720, 800, 960, 900)

    def test_empty_round_band_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ColumnBand(720, 960).rows(900, 900)


def test_threshold_clears_background_without_eroding_glyphs() -> None:
    """140 was measured: filler gone, strokes intact. 170 erodes Chinese glyphs."""
    assert BINARIZE_THRESHOLD == 140
    assert LIVE_LAYOUT.binarize_threshold == BINARIZE_THRESHOLD


def test_region_shift_keeps_width() -> None:
    region = Region(10, 20, 110, 60)
    shifted = region.shifted(15)
    assert shifted.as_box() == (10, 35, 110, 75)
