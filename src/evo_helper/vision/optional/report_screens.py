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
    OCR_PSM_COLUMN,
    OCR_PSM_LINE,
    ColumnBand,
    Region,
    ReportLayout,
    banner_bands,
    sections_from_banners,
)
from evo_helper.vision.scan_reading import COORD_RECIPES, COORD_WHITELIST, COORDINATE_RE

OCR_LANGUAGES = "chi_sim+eng"


class _Ocr(Protocol):
    def image_to_string(self, image: Any, lang: str, config: str) -> str: ...


def _load_backends() -> tuple[Any, _Ocr]:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Pillow is required; install the 'vision' extra") from exc
    try:
        import pytesseract
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
        participating_rows: tuple[int, int] | None = None,
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
        #: 覆盖布局里写死的参战区行界。回放会滚动，写死的下界会穿透到下一节——
        #: 实测 750 把「第1回合【剩余战舰】」框了进去，同一批数量被读了两遍。
        self._participating_rows = participating_rows

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
        top, bottom = self._participating_rows or self._layout.participating_rows
        return (
            self._read_fleet(self._layout.attacker_column.rows(top, bottom)),
            self._read_fleet(self._layout.defender_column.rows(top, bottom)),
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

        Measured on the batch: a Chinese-capable pass keeps names within one
        character but corrupts counts (``5`` -> ``日``), while an English pass
        reads every count exactly but renders the names as Latin noise. Neither
        is good enough alone, so names come from the Chinese pass and counts
        from the English one, joined row by row.

        The count pass runs ``eng`` rather than ``chi_sim+eng``: loading the
        Chinese model costs ~0.43s per invocation and buys nothing here, since
        only the trailing number is used. Measured 1.53s -> 0.66s for both
        columns, with identical counts. A digit whitelist is *not* used — it
        starves Tesseract of the glyphs it segments rows by, collapsing 15 rows
        into 1.
        """
        counts = _rows(self._read(region, OCR_PSM_COLUMN, language="eng"))
        names = _names(self._read(region, OCR_PSM_COLUMN, language="chi_sim"))
        if len(names) != len(counts):
            # Row counts disagree, so the two passes cannot be aligned. Fall
            # back to the pass whose counts are trustworthy rather than pairing
            # a name with another row's number.
            return "\n".join(f"{name}  {count}" for name, count in counts)
        return "\n".join(f"{name}  {count}" for name, (_, count) in zip(names, counts, strict=True))

    def _read_coordinate(self, region: Region) -> str:
        """逐套配方读坐标，读出合法三元组就采信。

        单套配方在这一行上不够。实测同一屏、同一形状的两个 ROI，守方读出
        `[2:137:14]`，攻方却只读出 `]`——于是 `parse_versus_block` 判成「单边战报」
        并整份拒收，而战报本身是好的。

        两条对策与坐标扫描器那边同源（`vision.scan_reading.COORD_RECIPES`）：
        方括号从白名单里去掉（`]` 会被读成数字，反过来也会吃掉相邻字符），
        放大兼用最近邻（LANCZOS 会把细笔画之间的缝插值糊掉）。
        """
        for scale, resample in COORD_RECIPES:
            text = self._read(
                region,
                OCR_PSM_LINE,
                language="eng",
                whitelist=COORD_WHITELIST,
                scale=scale,
                resample=resample,
            ).strip()
            if COORDINATE_RE.search(text):
                return text
        return text

    def _read(
        self,
        region: Region,
        psm: int,
        *,
        language: str = OCR_LANGUAGES,
        whitelist: str | None = None,
        scale: int | None = None,
        resample: str = "lanczos",
    ) -> str:
        crop = self._image.crop(region.as_box()).convert("L")
        scale = scale or self._layout.ocr_upscale
        filters = {
            "lanczos": self._image_module.Resampling.LANCZOS,
            "nearest": self._image_module.Resampling.NEAREST,
        }
        crop = crop.resize((crop.width * scale, crop.height * scale), filters[resample])
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


def row_brightness(
    image: Any, left: int, right: int, top: int, bottom: int, step: int = 4
) -> list[float]:
    """面板中列的逐行平均亮度，喂给 `banner_bands` 定位分节横幅。"""
    grey = image.convert("L")
    pixels = grey.load()
    columns = range(left, right, step)
    return [sum(pixels[x, y] for x in columns) / len(columns) for y in range(top, bottom)]


def locate_sections(image: Any, layout: ReportLayout, *, top: int = 300) -> list[tuple[int, int]]:
    """定位回放页的各分节行区间：第 0 节是参战战舰，其后每节对应一个回合。

    从 `top` 往下扫，跳过上方的 VS 块与增益表——那里也有亮条，会被误认成分节横幅。
    """
    bottom = layout.viewport[1]
    profile = row_brightness(
        image, layout.attacker_column.left + 20, layout.defender_column.right - 20, top, bottom
    )
    return sections_from_banners(banner_bands(profile, top=top), bottom=bottom)
