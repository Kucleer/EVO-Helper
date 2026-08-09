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

#: 判定像素算不算墨迹的亮度门槛。
NUMBER_INK_THRESHOLD = 150

#: 名与数之间至少这么宽的空白才算「缝」。
NUMBER_COLUMN_GAP = 6

#: 量出来的数字列左右各留一点余量。
NUMBER_COLUMN_PADDING = 4

#: 一段墨迹至少这么宽才可能是数字列。面板边框只有两三像素宽。
NUMBER_COLUMN_MIN_WIDTH = 10

#: 「单位」数值的字符集。大舰队显示成 `5.36K`，所以要收 `.` 和 `K`。
UNIT_WHITELIST = "0123456789.K"

#: 名称列相对列左沿的裁剪范围与放大倍数。
FLEET_NAME_INSET = 15
FLEET_NAME_WIDTH = 115
FLEET_NAME_UPSCALE = 3

#: 名称列只认出一行时的兜底行距（实测值）。
FLEET_ROW_PITCH = 22


class _Ocr(Protocol):
    def image_to_string(self, image: Any, lang: str, config: str) -> str: ...

    #: 逐行定位要用词框，所以除了整段文字还需要结构化输出。
    def image_to_data(self, image: Any, **kwargs: Any) -> Any: ...

    Output: Any


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

    def read_fleet_rows(self, band: ColumnBand, top: int, bottom: int) -> str:
        """逐行读一列舰队：名字一遍、数量一遍，按行拼。

        ⚠️ **还没接进 `participating_columns`。** 在用户给的 5 份样本上它明显更好
        （80 行 61% → 88%，四个核心舰种 58% → 95%），但在既有的 2026-08-07 那份
        回归样本上**反而退步**：`95` 被读成 `35`，而旧的整列两遍读法在那份上 17 行全对。
        不能拿一个已知良好的样本去换平均值——接线前得先弄清这两份样本差在哪。

        为什么不整列一次读：tesseract 在中文字形旁边切不准行，`11` 被吞成 `1`、
        `39` 被切成 `33`；两遍行数一对不上就退回英文那遍，名字全成拉丁乱码。
        逐行读之后，实测 5 份样本 80 行的准确率 61% → 88%，
        四个核心舰种 58% → 95%。

        三条缺一不可（每一条都是实测踩出来的）：

        - **行位置用等距网格**，不用逐行检测。实测 17 行的表检出 18 行——
          `钛能守卫者` 整行没认出来，位置被碎片顶替，之后所有索引错开一位。
        - **数字列现场量**。数字左对齐，起点随内容变；不同来源的截图宽度也不同，
          实测两者差 31px，按写死的左界裁正好切掉首位。
        - **选票时后缀让位于更长的候选**。丢首位是恒定的失败模式，
          `74` 读成 `4` 比读对还多 6 票。
        """
        from evo_helper.vision.fleet_counts import COUNT_RECIPES, pick_count, row_grid

        names = self._fleet_names(band, top, bottom)
        if not names:
            return ""
        first_top, pitch, labels = names
        column = number_column(self._image, band, top, bottom)
        lines = []
        for index, y in enumerate(row_grid(first_top, pitch, len(labels))):
            crop = self._image.crop((column[0], y - 3, column[1], y + pitch - 3)).convert("L")
            votes: dict[str, int] = {}
            for scale, resample in COUNT_RECIPES:
                filt = (
                    self._image_module.Resampling.NEAREST
                    if resample == "nearest"
                    else self._image_module.Resampling.LANCZOS
                )
                grey = crop.resize((crop.width * scale, crop.height * scale), filt)
                text = self._ocr.image_to_string(
                    grey, lang="eng", config=f"--psm 7 -c tessedit_char_whitelist={UNIT_WHITELIST}"
                ).strip()
                if text:
                    votes[text] = votes.get(text, 0) + 1
            count = pick_count(votes)
            if count:
                lines.append(f"{labels[index]}  {count}")
        return "\n".join(lines)

    def _fleet_names(
        self, band: ColumnBand, top: int, bottom: int
    ) -> tuple[int, int, list[str]] | None:
        """名称列：返回首行位置、行距与每行的舰种名。"""
        from statistics import median

        crop = self._image.crop(
            (band.left + FLEET_NAME_INSET, top, band.left + FLEET_NAME_WIDTH, bottom)
        ).convert("L")
        grey = crop.resize(
            (crop.width * FLEET_NAME_UPSCALE, crop.height * FLEET_NAME_UPSCALE),
            self._image_module.Resampling.LANCZOS,
        )
        data = self._ocr.image_to_data(
            grey,
            lang="chi_sim",
            config=f"--psm {OCR_PSM_COLUMN}",
            output_type=self._ocr.Output.DICT,
        )
        rows: dict[tuple[int, int, int], tuple[int, str]] = {}
        for index, word in enumerate(data["text"]):
            if not word.strip():
                continue
            key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
            y = top + data["top"][index] // FLEET_NAME_UPSCALE
            previous = rows.get(key)
            rows[key] = (min(previous[0], y), previous[1] + word) if previous else (y, word)
        ordered = sorted(rows.values())
        if not ordered:
            return None
        tops = [y for y, _name in ordered]
        pitch = (
            int(median([b - a for a, b in zip(tops, tops[1:], strict=False)]))
            if len(tops) > 1
            else FLEET_ROW_PITCH
        )
        return (tops[0], max(pitch, 1), [name for _y, name in ordered])

    def round_columns(self) -> list[tuple[int, str, str]]:
        return [
            (
                number,
                self._read_band(self._layout.attacker_column, top, bottom),
                self._read_band(self._layout.defender_column, top, bottom),
            )
            for number, top, bottom in self._rounds
        ]

    def unit_totals(self) -> tuple[str, str]:
        """读战斗详情页的「单位」总数，双方各一。

        **这是总数的权威来源**，不是逐行明细之和：大舰队的数量显示成 `5.36K`
        这样的四舍五入值，逐行相加永远凑不出精确总数。

        详情页要滚动才看得到这一行，所以位置按「战斗详情」横幅定位，不写死——
        与回放页的分节定位同一套办法（`banner_bands`）。
        """
        from evo_helper.vision.fleet_counts import COUNT_RECIPES, pick_count

        bottom = self._layout.viewport[1]
        profile = row_brightness(
            self._image,
            self._layout.attacker_column.left + 20,
            self._layout.defender_column.right - 20,
            UNIT_SCAN_TOP,
            bottom,
        )
        bands = banner_bands(profile, top=UNIT_SCAN_TOP)
        if not bands:
            return ("", "")
        # 「战斗详情」是详情页最靠下的那条横幅；「战报」在它上面。
        top = bands[-1][1] + UNIT_ROW_OFFSET
        row = (top, min(top + UNIT_ROW_HEIGHT, bottom))

        def read(band: ColumnBand) -> str:
            """一侧的数值。标签在左、数值在右，只取右半。"""
            crop = self._image.crop(
                (band.left + UNIT_VALUE_INSET, row[0], band.right, row[1])
            ).convert("L")
            votes: dict[str, int] = {}
            for scale, resample in COUNT_RECIPES:
                filt = (
                    self._image_module.Resampling.NEAREST
                    if resample == "nearest"
                    else self._image_module.Resampling.LANCZOS
                )
                grey = crop.resize((crop.width * scale, crop.height * scale), filt)
                text = self._ocr.image_to_string(
                    grey, lang="eng", config=f"--psm 7 -c tessedit_char_whitelist={UNIT_WHITELIST}"
                ).strip()
                if text:
                    votes[text] = votes.get(text, 0) + 1
            return pick_count(votes)

        return (read(self._layout.attacker_column), read(self._layout.defender_column))

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


def number_column(image: Any, band: ColumnBand, top: int, bottom: int) -> tuple[int, int]:
    """在一列舰队里量出**数字子列**的横向范围。

    不能写死。数字列的起点随内容变：短数（`117`）与长数（`5.73K`）落点不同，
    不同来源的截图宽度也不一样（自采 1920、外部截图 1909）。实测两者的数字列
    起点差 31px——按写死的左界裁，正好**切掉首位数字**，`210` 读成 `10`、
    `74` 读成 `4`。这类错误在合计上看不出来，只在逐行比对时才现形。

    做法：把这一列切成若干段连续墨迹，取**最右那段够宽的**。
    不能取「最右的墨迹」——面板边框也在右边，实测边框那两三列像素会把结果
    带到 1194 去，整个数字列反而落在外面。
    """
    grey = image.convert("L")
    pixels = grey.load()
    height = grey.size[1]
    inked = [
        x
        for x in range(band.left, band.right)
        if sum(1 for y in range(top, min(bottom, height)) if pixels[x, y] > NUMBER_INK_THRESHOLD)
        > 1
    ]
    if not inked:
        return (band.left, band.right)

    runs: list[tuple[int, int]] = []
    start = previous = inked[0]
    for x in inked[1:]:
        if x - previous > NUMBER_COLUMN_GAP:
            runs.append((start, previous))
            start = x
        previous = x
    runs.append((start, previous))

    wide = [run for run in runs if run[1] - run[0] >= NUMBER_COLUMN_MIN_WIDTH]
    chosen = wide[-1] if wide else runs[-1]
    left, right = chosen

    # 左界取「名与数之间那道缝的中点」，而不是数字墨迹的最左端。
    # 数字是**左对齐**的，墨迹最左端就是首位笔画本身——贴着它裁，
    # 首位就会被削掉：实测 `210` 读成 `10`、`74` 读成 `4`、`28` 读成 `8`。
    # 缝里没有内容，多裁进来不会带入舰种名。
    index = runs.index(chosen)
    if index > 0:
        left = (runs[index - 1][1] + left) // 2
    else:
        left -= NUMBER_COLUMN_PADDING
    return (left, right + NUMBER_COLUMN_PADDING)


#: 详情页从这一行往下找横幅；再往上是 VS 块，那里也有亮条。
UNIT_SCAN_TOP = 100

#: 「单位」那一行相对「战斗详情」横幅下沿的偏移与高度。
UNIT_ROW_OFFSET = 18
UNIT_ROW_HEIGHT = 24

#: 两侧数值的横向范围（相对各自列）。
UNIT_VALUE_INSET = 100
