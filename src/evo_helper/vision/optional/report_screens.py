"""``ReportScreens`` backed by Pillow crops and Tesseract OCR.

Optional: Pillow and pytesseract live in the ``vision`` extra. Importing this
module without them raises, so the core stays installable without a vision
stack — the same degradation rule the rest of the project follows.

The recipe here is measured, not assumed. See
:mod:`evo_helper.vision.report_layout` for why the images are upscaled and
never binarized, and why coordinates get their own single-line ROI.
"""

from __future__ import annotations

from typing import Any, Protocol

from evo_helper.vision.report_layout import (
    OCR_COORDINATE_WHITELIST,
    OCR_PSM_COLUMN,
    OCR_PSM_LINE,
    ColumnBand,
    Region,
    ReportLayout,
)

OCR_LANGUAGES = "chi_sim+eng"


class _Ocr(Protocol):
    def image_to_string(self, image: Any, lang: str, config: str) -> str: ...


def _load_backends() -> tuple[Any, _Ocr]:
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Pillow is required; install the 'vision' extra") from exc
    try:
        import pytesseract  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("pytesseract is required; install the 'vision' extra") from exc
    return Image, pytesseract


class ImageReportScreens:
    """Crops one screenshot into the named regions and OCRs each of them.

    One instance reads one screen. The caller re-creates it after navigating,
    which keeps a stale screenshot from being read as the new page.
    """

    def __init__(
        self,
        image: Any,
        layout: ReportLayout,
        *,
        rounds: list[tuple[int, int, int]] | None = None,
        tesseract_cmd: str | None = None,
    ) -> None:
        """``rounds`` is ``(round_number, top, bottom)`` per located round banner.

        Round sections scroll, so their vertical extent cannot be baked into the
        layout; the caller locates each ``第N回合【剩余战舰】`` banner and passes
        the row band it introduces.
        """
        self._image_module, self._ocr = _load_backends()
        if tesseract_cmd:
            self._ocr.pytesseract.tesseract_cmd = tesseract_cmd  # type: ignore[attr-defined]
        self._image = image
        self._layout = layout
        self._rounds = rounds or []

    # -- ReportScreens ---------------------------------------------------

    def mail_rows(self) -> list[str]:
        return [
            self._read(self._layout.mail_row(index), OCR_PSM_COLUMN)
            for index in range(self._layout.mail_visible_rows)
        ]

    def report_header(self) -> str:
        return self._read(self._layout.report_header, OCR_PSM_COLUMN)

    def versus_block(self) -> str:
        """Rebuild the VS block as two aligned columns.

        The names come from the wide crop, but each coordinate is read from its
        own single-line ROI, because in the wide crop Tesseract turns the
        leading ``2`` of ``[2:137:18]`` into ``e``.
        """
        wide = self._read(self._layout.detail_versus, OCR_PSM_COLUMN)
        left, right = _name_columns(wide)
        attacker = self._read_coordinate(self._layout.detail_attacker_coordinate)
        defender = self._read_coordinate(self._layout.detail_defender_coordinate)
        return _compose_versus(left, right, attacker, defender)

    def replay_versus_block(self) -> str:
        wide = self._read(self._layout.replay_versus, OCR_PSM_COLUMN)
        left, right = _name_columns(wide)
        attacker = self._read_coordinate(self._layout.replay_attacker_coordinate)
        defender = self._read_coordinate(self._layout.replay_defender_coordinate)
        return _compose_versus(left, right, attacker, defender)

    def participating_columns(self) -> tuple[str, str]:
        return (
            self._read_fleet(self._layout.participating(self._layout.attacker_column)),
            self._read_fleet(self._layout.participating(self._layout.defender_column)),
        )

    def round_columns(self) -> list[tuple[int, str, str]]:
        return [
            (
                number,
                self._read_band(self._layout.attacker_column, top, bottom),
                self._read_band(self._layout.defender_column, top, bottom),
            )
            for number, top, bottom in self._rounds
        ]

    # -- internals -------------------------------------------------------

    def _read_band(self, band: ColumnBand, top: int, bottom: int) -> str:
        return self._read_fleet(band.rows(top, bottom))

    def _read_fleet(self, region: Region) -> str:
        """Read a fleet column twice and take the best half of each pass.

        Measured on the batch: ``chi_sim+eng`` gets every count right but drops
        some names into Latin noise (``无畏舰`` -> ``AKER``), while ``chi_sim``
        alone keeps the names within one character but corrupts counts
        (``5`` -> ``日``). Neither pass is good enough alone, so names come from
        the Chinese pass and counts from the mixed one, joined row by row.
        """
        counts = _rows(self._read(region, OCR_PSM_COLUMN))
        names = _names(self._read(region, OCR_PSM_COLUMN, language="chi_sim"))
        if len(names) != len(counts):
            # Row counts disagree, so the two passes cannot be aligned. Fall
            # back to the pass whose counts are trustworthy rather than pairing
            # a name with another row's number.
            return "\n".join(f"{name}  {count}" for name, count in counts)
        return "\n".join(f"{name}  {count}" for name, (_, count) in zip(names, counts, strict=True))

    def _read_coordinate(self, region: Region) -> str:
        return self._read(
            region,
            OCR_PSM_LINE,
            language="eng",
            whitelist=OCR_COORDINATE_WHITELIST,
        ).strip()

    def _read(
        self,
        region: Region,
        psm: int,
        *,
        language: str = OCR_LANGUAGES,
        whitelist: str | None = None,
    ) -> str:
        crop = self._image.crop(region.as_box()).convert("L")
        scale = self._layout.ocr_upscale
        crop = crop.resize(
            (crop.width * scale, crop.height * scale),
            self._image_module.Resampling.LANCZOS,
        )
        config = f"--psm {psm}"
        if whitelist:
            config += f" -c tessedit_char_whitelist={whitelist}"
        return self._ocr.image_to_string(crop, lang=language, config=config)


def _name_columns(wide: str) -> tuple[list[str], list[str]]:
    """Split the wide VS crop into left and right name columns.

    The middle ``VS`` glyph lands in whichever column Tesseract puts it in, so
    it is dropped rather than mistaken for a planet name.
    """
    left: list[str] = []
    right: list[str] = []
    for raw in wide.splitlines():
        parts = [part.strip() for part in raw.split("  ") if part.strip()]
        parts = [part for part in parts if part.upper() != "VS"]
        if len(parts) < 2:
            continue
        left.append(parts[0])
        right.append(parts[-1])
    return left, right


def _compose_versus(left: list[str], right: list[str], attacker: str, defender: str) -> str:
    """Re-emit the block in the two-column form ``parse_versus_block`` expects."""
    rows = [f"{a}    {b}" for a, b in zip(left[:2], right[:2], strict=False)]
    rows.append(f"{attacker}    {defender}")
    return "\n".join(rows)


def _rows(text: str) -> list[tuple[str, str]]:
    """Split OCR text into ``(name, count)`` pairs, dropping rows without a count."""
    import re

    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        match = re.match(r"^(.+?)\s{1,}(\d{1,7})$", raw.strip())
        if match is None:
            continue
        pairs.append((match.group(1).strip(), match.group(2)))
    return pairs


def _names(text: str) -> list[str]:
    """Take the leading name from each non-empty line of the name-only pass.

    The Chinese pass corrupts counts (``5`` -> ``日``), so a row must not be
    dropped for lacking a numeric tail — dropping it would shift every later
    name onto the wrong count.
    """
    import re

    names: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        name = re.split(r"\s{2,}", stripped)[0].strip()
        if name:
            names.append(name)
    return names
