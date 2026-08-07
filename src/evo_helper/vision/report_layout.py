"""Measured ROI geometry for the live report screens.

Every value here was measured on the ``evo-20260807-live`` capture batch
(1920x879 viewport). Geometry is viewport-specific by construction, so
:func:`layout_for_viewport` refuses any other size rather than scaling a guess:
a shifted crop silently truncates OCR text, and a truncated fleet column looks
exactly like a smaller fleet.
"""

from __future__ import annotations

from dataclasses import dataclass

#: OCR recipe measured against Tesseract on the batch images.
#:
#: Do not binarize. The panels render dim ``COMMAND OFFICERS`` / ``TOTAL CREWS``
#: / ``-17003`` / ``personnel`` filler behind the real rows, and a luminance cut
#: at 140 does remove it to the eye — but measured against Tesseract it makes
#: results *worse*, because it defeats Tesseract's own adaptive thresholding:
#: counts degrade (``95`` -> ``a5``, ``166`` -> ``165``, ``16`` -> ``15``).
#: Plain grayscale plus a LANCZOS upscale reads every count on the batch
#: correctly, and the filler is dim enough that Tesseract drops it anyway — the
#: filler-heavy attacker column yields no spurious rows.
#:
#: Measured 2026-08-07 on the ``evo-20260807-live`` report, comparing whole-report
#: reads (all ROIs, three repeats, median):
#:
#: ==========  ========  ==============
#: upscale     time      fully exact
#: ==========  ========  ==============
#: ``4``       7.70s     3/3
#: ``3``       6.17s     **0/3**
#: ``2``       5.72s     3/3
#: ==========  ========  ==============
#:
#: ``2`` is 26% faster than ``4`` with byte-identical output, so it is the
#: default. Tesseract is *not* monotonic in scale — ``3`` misreads a ship name
#: that both neighbours get right — so this value cannot be tuned by
#: interpolation. It is one sample; if a future report misreads, raise it back
#: to ``4`` and re-measure rather than trying ``3``.
OCR_UPSCALE = 2

#: Multi-row column of text (fleet columns, mail rows).
OCR_PSM_COLUMN = 6

#: A single line read on its own (coordinates).
OCR_PSM_LINE = 7

#: Coordinates are read from their own tight ROI, never lifted out of the wide
#: VS crop: in the wide crop Tesseract reads ``[2:137:18]`` as ``[e:137:18]``,
#: which then fails the coordinate regex. Read alone at ``--psm 7`` both sides
#: come back exact.
OCR_COORDINATE_WHITELIST = "0123456789:[]"

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
    ocr_upscale: int
    mail_first_row: Region
    mail_row_pitch: int
    mail_visible_rows: int
    report_header: Region
    detail_versus: Region
    replay_versus: Region
    #: Tight single-line ROIs for the coordinates, read separately at psm 7.
    detail_attacker_coordinate: Region
    detail_defender_coordinate: Region
    replay_attacker_coordinate: Region
    replay_defender_coordinate: Region
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
    ocr_upscale=OCR_UPSCALE,
    # Row pitch is ~85.6px; 6 rows are fully visible and the 7th is clipped, so
    # only the 6 complete rows are addressable.
    mail_first_row=Region(700, 205, 1220, 290),
    mail_row_pitch=86,
    mail_visible_rows=6,
    report_header=Region(720, 125, 1200, 195),
    detail_versus=Region(720, 370, 1200, 460),
    replay_versus=Region(720, 150, 1200, 240),
    detail_attacker_coordinate=Region(760, 428, 900, 452),
    detail_defender_coordinate=Region(1020, 428, 1160, 452),
    replay_attacker_coordinate=Region(760, 210, 900, 234),
    replay_defender_coordinate=Region(1020, 210, 1160, 234),
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
