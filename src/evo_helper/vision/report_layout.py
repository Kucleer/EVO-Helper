"""Measured ROI geometry for the live report screens.

Every value here was measured on the ``evo-20260807-live`` capture batch
(1920x879 viewport). Geometry is viewport-specific by construction, so
:func:`layout_for_viewport` refuses any other size rather than scaling a guess:
a shifted crop silently truncates OCR text, and a truncated fleet column looks
exactly like a smaller fleet.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Luminance cut that removes the decorative background text.
#:
#: The report panels render dim ``COMMAND OFFICERS`` / ``TOTAL CREWS`` /
#: ``-17003`` / ``personnel`` filler behind the real rows, inside the same
#: columns. Measured on the batch: at 140 the filler disappears completely and
#: the foreground glyphs keep their full stroke weight. Raising it to 170 starts
#: eroding Chinese glyphs, so 140 is the value with margin on both sides.
BINARIZE_THRESHOLD = 140

LAYOUT_VIEWPORT = (1920, 879)


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    right: int
    bottom: int

    def as_box(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    def shifted(self, dy: int) -> Region:
        return Region(self.left, self.top + dy, self.right, self.bottom + dy)


@dataclass(frozen=True)
class ColumnBand:
    """The horizontal band of one side's fleet column.

    Only ``x`` is fixed. Round sections scroll, so their ``y`` range comes from
    locating the ``第N回合【剩余战舰】`` banner at capture time.
    """

    left: int
    right: int

    def rows(self, top: int, bottom: int) -> Region:
        if bottom <= top:
            raise ValueError(f"column row band must be non-empty: {top}..{bottom}")
        return Region(self.left, top, self.right, bottom)


@dataclass(frozen=True)
class ReportLayout:
    viewport: tuple[int, int]
    binarize_threshold: int
    mail_first_row: Region
    mail_row_pitch: int
    mail_visible_rows: int
    report_header: Region
    detail_versus: Region
    replay_versus: Region
    attacker_column: ColumnBand
    defender_column: ColumnBand
    #: ``(top, bottom)`` of the 参战战舰 rows, before any scrolling.
    participating_rows: tuple[int, int]

    def mail_row(self, index: int) -> Region:
        """Region of the ``index``-th visible mail row, counting from 0."""
        if not 0 <= index < self.mail_visible_rows:
            raise IndexError(
                f"mail row {index} is outside the {self.mail_visible_rows} visible rows"
            )
        return self.mail_first_row.shifted(index * self.mail_row_pitch)

    def participating(self, band: ColumnBand) -> Region:
        top, bottom = self.participating_rows
        return band.rows(top, bottom)


#: Measured on evo-20260807-live (1920x879).
LIVE_LAYOUT = ReportLayout(
    viewport=LAYOUT_VIEWPORT,
    binarize_threshold=BINARIZE_THRESHOLD,
    # Row pitch is ~85.6px; 6 rows are fully visible and the 7th is clipped, so
    # only the 6 complete rows are addressable.
    mail_first_row=Region(700, 205, 1220, 290),
    mail_row_pitch=86,
    mail_visible_rows=6,
    report_header=Region(720, 125, 1200, 195),
    detail_versus=Region(720, 370, 1200, 460),
    replay_versus=Region(720, 150, 1200, 240),
    attacker_column=ColumnBand(720, 960),
    defender_column=ColumnBand(960, 1210),
    participating_rows=(405, 750),
)


def layout_for_viewport(width: int, height: int) -> ReportLayout:
    """Return the layout for this viewport, or fail closed.

    Geometry is not scaled to other sizes. Resizing the browser window does not
    even re-flow the game canvas without a reload, so a mismatched viewport
    means the capture setup drifted and must be fixed, not approximated.
    """
    if (width, height) != LAYOUT_VIEWPORT:
        raise ValueError(
            f"no measured report layout for viewport {width}x{height}; "
            f"only {LAYOUT_VIEWPORT[0]}x{LAYOUT_VIEWPORT[1]} is calibrated"
        )
    return LIVE_LAYOUT
