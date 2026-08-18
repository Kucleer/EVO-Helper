"""``ReportScreens`` backed by Pillow crops and Tesseract OCR.

Optional: Pillow and pytesseract live in the ``vision`` extra. Importing this
module without them raises, so the core stays installable without a vision
stack — the same degradation rule the rest of the project follows.

The recipe here is measured, not assumed. See
:mod:`evo_helper.vision.report_layout` for why the images are upscaled and
never binarized, and why coordinates get their own single-line ROI.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol

from evo_helper.vision.fleet_counts import COUNT_RECIPES
from evo_helper.vision.parsers import REPORT_TIME_RE, normalise_report_time
from evo_helper.vision.report_layout import (
    OCR_PSM_COLUMN,
    OCR_PSM_LINE,
    ColumnBand,
    Region,
    ReportLayout,
    banner_bands,
    sections_from_banners,
)
from evo_helper.vision.resource_digits import read_resource_cell
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

#: 页眉时间那一行只可能出现这些字符。空格留着——白名单里给了它，tesseract
#: 仍然常常吞掉，所以还要 `normalise_report_time` 把分隔补回去。
REPORT_TIME_WHITELIST = "0123456789/: "

#: 页眉时间依次试这几档放大。**3× 打头而不是布局默认的 2×**：实测五张现场图，
#: 2× 有三张把日期首位削掉（`11/08/…` → `1/08/…`），3× 与 4× 五张全对。
REPORT_TIME_UPSCALES: tuple[int, ...] = (3, 4, 2)

#: 胜负横幅的「算不算横幅墨迹」门槛，量在 **R−B 通道差**上（见 `outcome_banner`）。
#:
#: 实测七张详情页：横幅墨迹的峰值 155（红 `FAIL`）与 192（金 `VICTORY`），
#: 而幽灵文字与面板背景不超过 10。40 / 60 / 80 三档读出来一模一样，
#: 取中间那档——门槛落在一个数量级的空档里，不是调出来的参数。
OUTCOME_INK_THRESHOLD = 60

#: 剥完通道之后的放大倍数。2 / 3 / 4 三档在七张上输出**逐字节相同**，
#: 取最小的那档（`OCR_UPSCALE` 那张表同样的取舍：一样准就取快的）。
OUTCOME_UPSCALE = 2

#: 名称列相对列左沿的裁剪范围与放大倍数。
FLEET_NAME_INSET = 15
FLEET_NAME_WIDTH = 115
FLEET_NAME_UPSCALE = 3

#: 名称列只认出一行时的兜底行距（实测值）。
FLEET_ROW_PITCH = 22

#: 按名字取数时，名称列依次试这几档放大倍数。
#: 不同倍数漏掉的行不一样——侦察报告那 21 行里，3× 整行漏掉 `钛能守卫者`，
#: 而漏掉的恰好是判定要看的四个舰种之一。多试一档比调参稳。
NAME_PASS_UPSCALES: tuple[int, ...] = (FLEET_NAME_UPSCALE, 4, 2)

#: 战报存档图的 WEBP 质量。理由见 `ImageReportScreens.report_panel_image`。
REPORT_PANEL_WEBP_QUALITY = 90

#: 「获得资源」那 12 格**不走 tesseract**，走 `vision.resource_digits` 的字模匹配。
#:
#: ⚠️ **这一段原先是四套 tesseract 配方 + 两套谈拢，2026-08-18 整段换掉了。**
#: 换掉的理由不是「读不全」，是「读得不对」：34 份实拍（`tests/fixtures/vision/
#: battle_report_panels/`，408 格逐格人工核过真值）上，老配方只有 10 份 12 格齐全，
#: 而那 10 份里只有 5 份逐格正确——生产库里已经因此存进过两个错数
#: （`486.2K` 存成 `466200`、`272K` 存成 `72000`）。完整的实测对比写在
#: `vision.resource_digits` 的模块头。
#:
#: 顺带省掉的是每格 2–4 次 OCR：一份战报的这一块从两秒出头降到毫秒级。


@dataclass(frozen=True, slots=True)
class ReportPanelImage:
    """裁好、编码好的战报面板。宽高一并带出来——库里那两列不该由调用方再算一遍。"""

    image_bytes: bytes
    width: int
    height: int
    image_format: str


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
        #: 「单位」/「损失单位」两行的锚点。一屏只找一次——找它本身就要读一遍数值，
        #: 而「单位」和「损失单位」会各问一次。
        self._details_anchor: int | None = None
        self._details_anchor_read = False

    # -- ReportScreens ---------------------------------------------------

    def mail_rows(self) -> list[str]:
        return [
            self._read(self._layout.mail_row(index), OCR_PSM_COLUMN)
            for index in range(self._layout.mail_visible_rows)
        ]

    def report_header(self) -> str:
        """页眉文本。时间那一行读不到时，用窄 ROI 单独补一次。

        宽 ROI 按列读能把「主题: 攻击报告」读得很干净，却会把右上角那行时间糊成
        `'wi'`——于是 `REPORT_TIME_RE` 搜不到，整份报告卡在
        「report header has no readable time」上入不了库。实机（2026-08-11）
        五份 bot 探路战报连着栽在这里，而那行时间在图上清清楚楚。

        同一个坑仓库里已经踩过一次并留了办法：VS 块的坐标也是「宽裁剪里读不准、
        各自开一个窄单行 ROI」（见 `versus_block` 的注释）。这里照搬。

        补读只在宽读没拿到时间时才发生，稳态一次 OCR 都没多花。
        """
        wide = self._read(self._layout.report_header, OCR_PSM_COLUMN)
        if REPORT_TIME_RE.search(wide):
            return wide
        stamp = self._report_time()
        return f"{stamp}\n{wide}" if stamp is not None else wide

    def security_message(self) -> str:
        """安全提示邮件的正文；与战斗报告的 VS / 舰队区域完全不同。"""
        return self._read(self._layout.security_message, OCR_PSM_COLUMN)

    def report_panel_image(self, quality: int = REPORT_PANEL_WEBP_QUALITY) -> ReportPanelImage:
        """把整块战报面板裁出来、编码成 WEBP。**这一块不喂 OCR，是给人看的。**

        用户口径（2026-08-17）：读战报时截一张图，能在攻击日志页看到。

        **复用这一屏已经拍好的像素，不另拍一次。** 调用方手里的 `page` 就是读
        这份战报用的那一屏；重新截一次屏既多花时间，又可能拍到别的画面
        （面板已经被拖到底、或者已经关掉了）——那正是 `_report_screens` 头上
        「每次重新建」那条注释在防的事，只是方向相反。

        ROI 与它为什么留这么多余量写在 `report_layout.ReportLayout.report_panel`。

        WEBP q90：实测这个尺寸下 38.8 KB/张，每天 80 张约 3 MB。选有损而不是 PNG
        （同图 PNG 是它的好几倍），选 90 而不是默认的 80，是因为这张图上要认的
        是**小字坐标**（`[2:137:18]`），压过头就等于存了一张认不出目标的图。
        """
        crop = self._image.crop(self._layout.report_panel.as_box()).convert("RGB")
        buffer = BytesIO()
        crop.save(buffer, format="WEBP", quality=quality)
        return ReportPanelImage(
            image_bytes=buffer.getvalue(),
            width=crop.width,
            height=crop.height,
            image_format="webp",
        )

    def resource_cells(self) -> tuple[str, ...]:
        """「获得资源」那 12 格的原文，**行优先**（第一行左起 0/1/2/3）。

        用户口径（2026-08-17）：只统计这 12 个值；残骸与两个百分比不做。

        **接在读战报这一趟里，不额外开一次导航**——这一块就在未滚动那一屏上，
        和 VS 块、`report_panel` 的存档图是同一屏像素（`report_panel_image`
        的注释写着为什么必须复用同一屏）。

        识别本身**不走 tesseract**，走 `vision.resource_digits` 的字模匹配：
        这一格字高只有 9 像素，tesseract 在这个尺寸上既读不全又读不对
        （实测对比在那个模块的头部）。

        读不出来的格子返回空串，由 `domain.battle_resources.parse_resource_grid`
        决定整块作废——这一层不做「补 0」这种决定。
        """
        grid = self._layout.resource_grid
        return tuple(self._read_resource_cell(grid.cell(slot)) for slot in range(grid.slots))

    def _read_resource_cell(self, region: Region) -> str:
        """把一格裁出来、转成灰度网格，交给字模匹配。"""
        crop = self._image.crop(region.as_box()).convert("L")
        # `tobytes()` 是逐行紧排的灰度字节，没有行填充；比 `getdata()` 快，
        # 也不吃 Pillow 14 要拿掉 `getdata()` 的那条弃用。
        raw = crop.tobytes()
        width = crop.width
        luminance = [raw[y * width : (y + 1) * width] for y in range(crop.height)]
        return read_resource_cell(luminance)

    def _report_time(self) -> str | None:
        """窄 ROI 读页眉时间：单行、纯英文、只认数字与分隔符。

        ⚠️ **必须用自己的放大倍数，不能跟布局默认的 2×。** 实测五张现场图：
        2× 有三张把日期首位削掉（`11/08/…` 读成 `1/08/…`，规范化随即判定失败），
        3× 与 4× 五张全对。逐档试、第一个读通的就采信——和坐标行的
        `COORD_RECIPES` 同一个路子。
        """
        for scale in REPORT_TIME_UPSCALES:
            stamp = normalise_report_time(
                self._read(
                    self._layout.report_time,
                    OCR_PSM_LINE,
                    language="eng",
                    whitelist=REPORT_TIME_WHITELIST,
                    scale=scale,
                )
            )
            if stamp is not None:
                return stamp
        return None

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
        pitch, rows = names
        first_top = rows[0][0]
        labels = [label for _y, label in rows]
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
        self, band: ColumnBand, top: int, bottom: int, *, upscale: int = FLEET_NAME_UPSCALE
    ) -> tuple[int, list[tuple[int, str]]] | None:
        """名称列：返回行距与**每一行自己量到的** `(顶端, 舰种名)`。

        为什么连每行的 y 一起交出去：等距网格在长清单上会漂。侦察报告的战舰清单
        行距是 27.5px，取整成 27 之后到第 12 行就差了半行——实测 `钛能守卫者`
        那一行的数字因此落在裁剪框外，读成空；再往下每隔一行空一次。
        按名字取数的场合，名字自己那一行的 y 才是最准的锚点。

        （`read_fleet_rows` 仍然用等距网格，那边是刻意的：它要处理「某一行整个
        没被认出来」的情况，网格能把缺的那一行补上位置，而这里缺席就直接缺席。）

        `upscale` 可换档：同一列在不同倍数下漏掉的行不一样。
        """
        from statistics import median

        crop = self._image.crop(
            (band.left + FLEET_NAME_INSET, top, band.left + FLEET_NAME_WIDTH, bottom)
        ).convert("L")
        grey = crop.resize(
            (crop.width * upscale, crop.height * upscale),
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
            y = top + data["top"][index] // upscale
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
        return (max(pitch, 1), ordered)

    def named_counts(
        self,
        wanted: Sequence[str],
        band: ColumnBand,
        top: int,
        bottom: int,
        *,
        count_band: tuple[int, int] | None = None,
    ) -> dict[str, int]:
        """在一张清单里**按名字**取数量，而不是按行序对位。

        侦察报告的战舰清单有 21 行，按行序对位的读法在实机上会掉行——实测
        `钛能守卫者` 整行没被认出来，于是它后面每一行的数字都串了位，
        `拦截导弹` 读成 5（真值 0）。**串位比读不出更坏**：数字看着都合理。

        海盗打不打只取决于四个舰种，所以这里改成「找到那几行，各读各的数」：
        名字自己就是这一行的凭据，掉行只会让那个名字缺席，不会让别人顶替它。
        缺席的名字**不出现在返回值里**——是当 0 还是整份拒收，由调用方决定：
        「这一屏没滚到」和「这个舰种真的是 0」在这里分不出来，也不该在这里猜。

        ⚠️ **`count_band` 要传。** 不传就退回 `number_column()` 现场量，而那在
        「整列都是 0」的清单上会量错：单个 `0` 太窄，够宽的墨迹段只剩下面板左边
        那层水印（`-17003` / `COMMAND OFFICERS`），于是量出来的「数字列」是 (731, 808)，
        读到的「数量」其实是水印里的数字。
        **实机后果：一个四项全 0 的海盗被读成有舰队，真的挨了一发攻击**（2026-08-09）。
        """
        from evo_helper.vision.parsers import snap_unit_name

        column = count_band or number_column(self._image, band, top, bottom)
        counts: dict[str, int] = {}
        # 换档补漏：同一列在不同放大倍数下漏掉的行不一样（实测 3× 整行漏掉
        # `钛能守卫者`，4× 读得出来）。只补没找到的名字，已经读到的不重读。
        for upscale in NAME_PASS_UPSCALES:
            if all(name in counts for name in wanted):
                break
            found = self._fleet_names(band, top, bottom, upscale=upscale)
            if not found:
                continue
            pitch, rows = found
            for row_top, label in rows:
                name = snap_unit_name(label)[0]
                if name not in wanted or name in counts:
                    continue
                value = self._count_at(column, row_top, pitch)
                if value is not None:
                    counts[name] = value
        return counts

    def _count_at(self, column: tuple[int, int], top: int, pitch: int) -> int | None:
        """读一行的数量；读不出返回 None。

        **非 0 的读数要求至少两套配方读出同一个字符串**，0 只要一套就采信。

        这条不对称是有意的：非 0 会让判定变成「打」，也就是真的送出舰队，
        所以它需要旁证；而 0 只会让我们跳过一个目标，代价是白跑一趟。
        实测那个孤零零的 `0` 只有 2× 那一档读得出来（见 `TOTALS_RECIPES`），
        对它要求两票就等于永远读不出 0——那会把「这里是空的」变成「不知道」。
        """
        from evo_helper.domain.fleet_counts import parse_fleet_count
        from evo_helper.vision.fleet_counts import pick_count

        crop = self._image.crop((column[0], top - 3, column[1], top + pitch - 3)).convert("L")
        votes: dict[str, int] = {}
        for scale, resample in TOTALS_RECIPES:
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
        picked = pick_count(votes)
        if not picked:
            return None
        value = parse_fleet_count(picked)
        if value is None:
            return None
        if value != 0 and votes.get(picked, 0) < COUNT_MIN_AGREEMENT:
            # 只有一套配方读出这个非 0 值，旁证不足。宁可当成「没读到」——
            # 调用方那边「没读到」不会变成「打」，而一个假的非 0 会。
            return None
        return value

    def scout_intro_texts(self) -> list[str]:
        """侦察报告开头那行的候选读法，一套配方一个。

        那行是「你从[2:137:18]…已对[2:137:4]…」，坐标嵌在中文句子里。
        **中英混读这一行读不出坐标**：实测 `[2:137:18]` 读成 `[e:137:18]`、
        `[2:137:4]` 读成 `[137:4]`——首位被吃掉，而 `137:4` 仍然像个合法片段。
        所以这里改用数字白名单 + `eng`，把整行当数字串读。

        代价是会读出噪声（实测 `2:137:18 382:137:4 3`——`38` 是被并进来的中文笔画）。
        所以**不在这里判对错**：交出全部候选，由 `scout_reports.parse_intro_coordinates`
        按「恰好两个、且都在银河/恒星系/位号范围内」去挑。判据留在纯函数里才测得动。
        """
        from evo_helper.vision.scout_reports import SCOUT_INTRO_LINE_ROI

        return [
            self._read(
                SCOUT_INTRO_LINE_ROI,
                OCR_PSM_COLUMN,
                language="eng",
                whitelist=COORD_WHITELIST,
                scale=scale,
                resample=resample,
            )
            for scale, resample in COORD_RECIPES
        ]

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
        return self._totals_row(0)

    def loss_totals(self) -> tuple[str, str]:
        """读「损失单位」总数，双方各一。这是海盗战报要记的「战损」。

        它紧跟在「单位」下面一行，所以用同一个横幅锚点、往下挪一行。

        ⚠️ **必须在详情页拖到底的那一屏上读。** 未滚动时这一行正好被面板下沿切掉，
        读出来是半行字（实机上「损失单位」只露出上半截）。拖到底是可标定的姿势：
        实测同一份报告拖 280px 与拖 520px 落点完全一致——面板夹到底了，
        所以这一行相对横幅的偏移是固定的。
        """
        return self._totals_row(1)

    def outcome_banner(self) -> str:
        """详情页上那行 `VICTORY` / `FAIL`。这是战报里「打赢没有」的唯一来源。

        **按颜色剥，不按亮度读。** 原先是「灰度 + psm 7」，海盗那份金色
        `VICTORY` 读得出来，bot 战报的红色 `FAIL` 却五张全废——2026-08-11 的
        五张实拍读出来是 `'- a'`、`'- a'`、`'- a'`、`''`、`''`。
        成因不是几何（横幅墨迹的外接框七张逐像素一致，都落在 `OUTCOME_ROI` 里），
        而是横幅背后压着一层「`-TOTAL CREW` / `-17003` / `-COMMAND OFFICERS`」的
        幽灵文字：它和暗红色的 `FAIL` 灰度接近，`--psm 7` 只肯交出一行，
        于是交出的是那层幽灵。

        判据用 **R−B 通道差**：横幅是红（`FAIL`，实测 `(184,52,44)`）或金
        （`VICTORY`），两者的 R 都远高于 B；幽灵文字与面板背景是蓝灰的，R≈B。
        实测七张（5 张 `FAIL` + 2 张 `VICTORY`）横幅墨迹的 R−B 峰值 155/192，
        而背景不超过 10——中间隔着一个数量级，门槛落在哪都一样。

        剥完再二值化。这不违反模块头「不要二值化」那条：那条说的是**舰队明细列**，
        灰度切一刀会打断 tesseract 自己的自适应阈值、把数字读坏；这里切的是
        通道差，切完只剩横幅那几个字母，没有别的东西可坏。

        只跑 `eng`：这一行没有中文，多加载一个中文模型白花约 0.4 秒。
        不限字符集——白名单会让 tesseract 失去切分依据，实测大字反而读不出来。
        """
        # PIL 在 `__init__` 里已经确认装得上（`_load_backends`），这里直接用。
        from PIL import ImageChops

        from evo_helper.vision.pirate_reports import OUTCOME_ROI

        crop = self._image.crop(OUTCOME_ROI.as_box()).convert("RGB")
        red, _green, blue = crop.split()
        ink = ImageChops.subtract(red, blue)
        # 黑字白底：tesseract 对这个方向最稳，而且和别处的灰度裁剪一致。
        mask = ink.point(lambda value: 0 if value >= OUTCOME_INK_THRESHOLD else 255)
        mask = mask.resize(
            (mask.width * OUTCOME_UPSCALE, mask.height * OUTCOME_UPSCALE),
            self._image_module.Resampling.LANCZOS,
        )
        return str(self._ocr.image_to_string(mask, lang="eng", config=f"--psm {OCR_PSM_LINE}"))

    def _totals_row(self, row_index: int) -> tuple[str, str]:
        """「战斗详情」横幅之下第 `row_index` 行的双方数值。"""
        anchor = self._details_banner_bottom()
        if anchor is None:
            return ("", "")
        return self._row_values(anchor + UNIT_ROW_OFFSET + row_index * UNIT_ROW_PITCH)

    def _row_values(self, top: int) -> tuple[str, str]:
        """一行里双方的数值。标签在左、数值在右，只取右半。

        数值用数字白名单读，因为这一行背后压着 `-17003` / `TOTAL CREW` 那层水印——
        不限字符集会把水印的数字一起读进来。
        """
        from evo_helper.vision.fleet_counts import pick_count

        bottom = min(top + UNIT_ROW_HEIGHT, self._layout.viewport[1])

        def read(band: ColumnBand) -> str:
            crop = self._image.crop(
                (band.left + UNIT_VALUE_INSET, top, band.right, bottom)
            ).convert("L")
            votes: dict[str, int] = {}
            for scale, resample in TOTALS_RECIPES:
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

    def _details_banner_bottom(self) -> int | None:
        """「战斗详情」横幅的下沿；找不到返回 None。一屏只算一次。

        ⚠️ **不能直接取最靠下的那条亮带。** 详情页拖到底之后，最靠下的亮带是那个
        黄色的「查看战斗回放」按钮——照它算出来的行落在按钮下面的空白上，
        读回来是空字符串，于是报「战损读不出来」，而真正的毛病是锚点找错了。
        实机踩过：未滚动那屏按钮不在可视区，取最后一条恰好是对的，
        所以这个错要等到拖到底之后才暴露。

        判据是**那条亮带下面第一行是不是两个能解析的数**。不用回读标签：
        「单位:」那几个字是暗灰小字，`chi_sim` 实测读成 `后亿:`／`下`，
        拿读不准的东西当判据等于换了个地方失败。而这两个数本来就是要读的，
        读得出来即证明锚点对了——判据和答案是同一件事。
        """
        if self._details_anchor_read:
            return self._details_anchor
        from evo_helper.domain.fleet_counts import parse_fleet_count

        profile = row_brightness(
            self._image,
            self._layout.attacker_column.left + 20,
            self._layout.defender_column.right - 20,
            UNIT_SCAN_TOP,
            self._layout.viewport[1],
        )
        anchor: int | None = None
        for _start, end in reversed(banner_bands(profile, top=UNIT_SCAN_TOP)):
            left, right = self._row_values(end + UNIT_ROW_OFFSET)
            if left and right and parse_fleet_count(left) is not None:
                if parse_fleet_count(right) is not None:
                    anchor = end
                    break
        self._details_anchor = anchor
        self._details_anchor_read = True
        return anchor

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
        chinese = self._read(region, OCR_PSM_COLUMN, language="chi_sim")
        names = _names(chinese)
        if len(names) != len(counts):
            # 中文那遍常多出几行装饰性噪声（实测：一行孤零零的 `”`、一行 `1 17`）。
            # 舰种名是封闭词表，对不上词表的行就是噪声——去掉之后往往就能和
            # 数字那遍对齐，而不必牺牲名称。
            names = _vocabulary_names(names)
        if len(names) == len(counts):
            return "\n".join(
                f"{name}  {count}" for name, (_, count) in zip(names, counts, strict=True)
            )
        # 仍然对不上。**绝不退回英文那遍的名字**：那一遍把 `轻型战斗机` 读成
        # `SRLS HL`、`重型战斗机` 读成 `BHR`，而这些字符串会原样入库成舰种名。
        # 2026-08-08 那份战报就是这么变成一屏拉丁乱码的，而且从头到尾没有报错——
        # 数字是对的，看起来一切正常。名称是舰队时间线做差异的键，错了比缺了更糟：
        # 每份战报都会显示成「首次出现」。
        # 宁可交出中文那遍自己的数字（下游 `read_until_total` 会因为合计对不上
        # 而拒收整列），也不交出一个数字漂亮、名字全错的结果。
        from evo_helper.vision.parsers import snap_unit_name

        return "\n".join(
            f"{name}  {count}"
            for name, count in _rows(chinese)
            if snap_unit_name(name)[1] != "unknown"
        )

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


def _vocabulary_names(names: list[str]) -> list[str]:
    """只保留能落到已知舰种/防御设施词表上的行。

    用 `snap_unit_name` 而不是精确相等：中文那遍读出来的名字通常差一个字
    （`无晨舰` → `无畏舰`），差一个字仍是一行真数据，不能当噪声丢掉。
    真正要丢的是 `”`、`1 17` 这种压根不像单位名的行。
    """
    from evo_helper.vision.parsers import snap_unit_name

    return [name for name in names if snap_unit_name(name)[1] != "unknown"]


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
#:
#: 高度是 20 而不是行距 22：**行窗不能碰到下一行**。「单位」下面紧跟着「损失单位」，
#: 窗口取 24 时下一行的顶边会挤进来，`--psm 7`（单行）当场读空——
#: 实测同一张图 height=20 读出 `100`、height=24 读出空字符串。
UNIT_ROW_OFFSET = 18
UNIT_ROW_HEIGHT = 20

#: 「单位」到「损失单位」的行距（实机量于 2026-08-09 的海盗战报详情页）。
UNIT_ROW_PITCH = 22


#: 两侧数值的横向范围（相对各自列）。
UNIT_VALUE_INSET = 100

#: 「单位」/「损失单位」两行的配方阶梯：比 `COUNT_RECIPES` 多一档 **2×**。
#:
#: 战损常常是孤零零一个 `0`（我方一艘没损失），而实测**只有 2× 才读得出它**：
#: 3×/4×/5×/6×/8× 配数字白名单一律读空。放大反而更差不是笔误——
#: 白名单剥掉了 tesseract 用来定位字形的上下文，单个窄字形放得越大越像噪点。
#: 白名单本身不能去掉：这一行背后压着 `-17003` / `COMMAND OFFICERS` 那层水印。
#:
#: 2× 只加在这两行上，不动 `COUNT_RECIPES`——那套阶梯是对着舰队明细列标定的，
#: 而「读得对」在那边是靠合计校验兜住的，这边没有合计可校。
TOTALS_RECIPES: tuple[tuple[int, str], ...] = ((2, "lanczos"), *COUNT_RECIPES)

#: 非 0 的数量至少要几套配方读出同一个字符串才采信。
#: 见 `_count_at`：非 0 会让判定变成「打」，需要旁证；0 只要一套。
COUNT_MIN_AGREEMENT = 2
