"""Measured ROI geometry for the live report screens.

Every value here was measured on the ``evo-20260807-live`` capture batch
(1920x879 viewport). Geometry is viewport-specific by construction, so
:func:`layout_for_viewport` refuses any other size rather than scaling a guess:
a shifted crop silently truncates OCR text, and a truncated fleet column looks
exactly like a smaller fleet.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

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
class ResourceGrid:
    """「获得资源」那 12 格**数字**的 ROI（图标不裁，位置即类型）。

    量于 2026-08-17，对着 `var/logs/vp-detail.png`（标定视口 1920×879 的未滚动
    详情页实拍）。做法是把面板按亮度切成墨迹带，逐条量外接框：

    - 数字左沿：``770 / 883 / 996 / 1109`` —— 列距**恰好 113**，四列等距
    - 图标左沿：``736 / 847 / 967 / 1080`` —— 数字要裁到下一个图标之前为止
    - 数字行：``513–521 / 543–551 / 573–581`` —— 行距 30，字高只有 9 像素

    所以每格取 ``(左沿 − 3, 行顶 − 5)`` 到 ``(左沿 + 76, 行顶 + 15)``：
    宽度 76 是「下一个图标左沿减三」留出来的上限，`501.1K` 这样六个字符实测约
    48 像素，还有富余；高度给字高留了两边各几像素。

    ⚠️ **数字宽度不能再放宽。** 再宽就吃到下一格的图标，而图标的亮边会被
    tesseract 当成字符——读出来的不是空，是一个混进了噪声的数。

    ⚠️ **这一块只在未滚动那一屏上**（和 `report_panel`、VS 块同一屏）。
    拖到底之后它整个滚出可视区。
    """

    #: 第 0 格数字 ROI 的左沿。
    first_number_left: int
    #: 数字 ROI 的宽度。
    number_width: int
    #: 相邻两列数字左沿的距离。
    column_pitch: int
    columns: int
    #: 第 0 行数字 ROI 的上沿。
    first_row_top: int
    #: 数字 ROI 的高度。
    number_height: int
    #: 相邻两行数字上沿的距离。
    row_pitch: int
    rows: int

    @property
    def slots(self) -> int:
        return self.columns * self.rows

    def cell(self, slot: int) -> Region:
        """第 ``slot`` 格数字的 ROI。编号**行优先**：第一行左起 0/1/2/3。"""
        if not 0 <= slot < self.slots:
            raise IndexError(f"槽位 {slot} 不在 0..{self.slots - 1} 之内")
        row, column = divmod(slot, self.columns)
        left = self.first_number_left + column * self.column_pitch
        top = self.first_row_top + row * self.row_pitch
        return Region(left, top, left + self.number_width, top + self.number_height)


@dataclass(frozen=True)
class ReportLayout:
    viewport: tuple[int, int]
    ocr_upscale: int
    mail_first_row: Region
    mail_row_pitch: int
    mail_visible_rows: int
    report_header: Region
    #: 页眉右上角那行时间的**窄单行 ROI**，与坐标同一套路子（见下面那组注释）。
    #:
    #: `report_header` 是一块两行、带中文、按列读的宽 ROI，主题行读得很干净，
    #: 而右上角那行时间在同一次读里被糊成 `'wi'`——`REPORT_TIME_RE` 自然搜不到，
    #: 整份报告卡在「report header has no readable time」上入不了库。
    #: 实机（2026-08-11）五份 bot 探路战报连着栽在这里。
    #:
    #: 单独裁出来、psm 7、纯数字白名单，五张现场图全部读对；顺带在两份更早的
    #: 海盗战报截图（另一个 ui_version）上也读对了。
    report_time: Region
    #: 安全提示邮件的正文。此类邮件没有 VS / 舰队区，关键信息只在这块正文。
    security_message: Region
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
    #: 存档用的整块面板 ROI（`storage.report_screenshots`）。
    #:
    #: 这一块不喂 OCR，它是给人看的：攻击日志上点开来确认「这一发到底打的是谁、
    #: 打成了什么」。所以判据和上面那些窄 ROI 相反——**宁可多截一点，也别切掉数据**。
    #:
    #: 量于 2026-08-17，对着 `var/logs/vp-detail.png`（标定视口 1920×879 的未滚动
    #: 详情页实拍）。面板内容的左右边界是 x 728 与 1195，这里取 700–1220 各留
    #: 二十余像素余量；上下取 105–800，覆盖从面板顶栏（发件人 / 主题 / 报告时间，
    #: 版面 `report_header` 是 125–195）一路到面板下沿（实拍约 770）。
    #: 裁出来 520×695，WEBP q90 实测 38.8 KB。
    #:
    #: ⚠️ **必须在未滚动那一屏上裁。** 「战报」横幅、VS 块（双方名称与两侧坐标）
    #: 只在这一屏上；拖到底之后它们滚出可视区（模块 `vision.pirate_reports` 的
    #: 头部记着同一条）。代价是「损失单位」那一行在这一屏被面板下沿切掉半行——
    #: 那是游戏自己的排版，不是 ROI 切的，两样东西本来就不在同一屏上。
    #: 取舍按用户口径（2026-08-17）：这张图先要能认出**这是谁的战报**。
    #:
    #: 余量是刻意留的：面板高度会随内容变（舰队回收百分比、战斗详情行数），
    #: 贴着量出来的边裁，换一份内容更长的战报就会切掉数据。
    report_panel: Region
    #: 「获得资源」那 12 格数字的 ROI，见 `ResourceGrid`。
    resource_grid: ResourceGrid

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
    report_time=Region(1010, 126, 1205, 162),
    security_message=Region(720, 205, 1205, 420),
    detail_versus=Region(720, 370, 1200, 460),
    replay_versus=Region(720, 150, 1200, 240),
    detail_attacker_coordinate=Region(760, 428, 900, 452),
    detail_defender_coordinate=Region(1020, 428, 1160, 452),
    replay_attacker_coordinate=Region(760, 210, 900, 234),
    replay_defender_coordinate=Region(1020, 210, 1160, 234),
    attacker_column=ColumnBand(720, 960),
    defender_column=ColumnBand(960, 1210),
    participating_rows=(405, 750),
    report_panel=Region(700, 105, 1220, 800),
    resource_grid=ResourceGrid(
        first_number_left=767,
        number_width=76,
        column_pitch=113,
        columns=4,
        first_row_top=508,
        number_height=20,
        row_pitch=30,
        rows=3,
    ),
)


def crop_to_viewport(image: Any) -> Any:
    """把整窗截图裁成版面标定用的游戏视口。

    `capture_window(client_only=True)` 交出来的是 **操作系统意义上的** client 区，
    而 Chrome `--app` 窗口把自己那条 38px 标题栏也画在 client 区里，于是实拍是
    1920×917。版面 ROI 当年是对着裁掉标题栏的 1920×879 量的（`var/logs/vp-*.png`
    就是那批），两者差的正好是 `APP_TITLE_BAR_PX`。

    ⚠️ **不要反过来去改 `capture_window`。** 点击坐标（`system_navigator`、
    `game.pirate_ui`）全部是在含标题栏的 917 空间里量的，截图与点击由此自洽；
    动了截图，整条点击链路会整体偏移 38px。差异只在版面这一侧，就只在这里补。

    高度对不上任何一种已知形态时直接抛错，不猜。
    """
    from evo_helper.game.game_window import APP_TITLE_BAR_PX

    width, height = image.width, image.height
    if (width, height) == LAYOUT_VIEWPORT:
        return image
    if (width, height - APP_TITLE_BAR_PX) == LAYOUT_VIEWPORT:
        return image.crop((0, APP_TITLE_BAR_PX, width, height))
    raise ValueError(
        f"截图 {width}x{height} 既不是标定视口 {LAYOUT_VIEWPORT[0]}x{LAYOUT_VIEWPORT[1]}，"
        f"也不是它加上 {APP_TITLE_BAR_PX}px 标题栏；采集设置漂了，先修采集"
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


#: 分节横幅是一条横贯面板的亮带。判据用亮度而不是文字——横幅上的
#: 「第1回合【剩余战舰】」实测 OCR 只读出 `ee`，靠文字定位不住。
BANNER_BRIGHTNESS_RATIO = 1.55
BANNER_MIN_HEIGHT = 18

#: 亮带与内容之间留一点余量，免得把横幅自身的像素读进行里。
SECTION_PADDING = 3


def banner_bands(
    profile: Sequence[float],
    *,
    top: int,
    ratio: float = BANNER_BRIGHTNESS_RATIO,
    min_height: int = BANNER_MIN_HEIGHT,
) -> list[tuple[int, int]]:
    """从逐行亮度里找出分节横幅，返回每条亮带的 ``(起, 止)``。

    `profile[i]` 是视口第 ``top + i`` 行在面板中列的平均亮度。基线取中位数——
    面板大部分行是暗背景，中位数不受横幅本身影响。

    只认够高的亮带：行内选中高亮、面板描边也会亮，但它们都薄。
    """
    if not profile:
        return []
    baseline = median(profile)
    threshold = baseline * ratio
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(profile):
        if value > threshold:
            start = index if start is None else start
            continue
        if start is not None:
            if index - start >= min_height:
                bands.append((top + start, top + index - 1))
            start = None
    if start is not None and len(profile) - start >= min_height:
        bands.append((top + start, top + len(profile) - 1))
    return bands


def sections_from_banners(
    bands: Sequence[tuple[int, int]], *, bottom: int, padding: int = SECTION_PADDING
) -> list[tuple[int, int]]:
    """把横幅位置换算成各分节的行区间。

    第 i 节 = 第 i 条横幅之下、第 i+1 条横幅之上。最后一节到 ``bottom`` 为止。

    这是「参战战舰」与「第N回合」不能写死的原因：回放内容会滚动，
    写死的下界会**穿透到下一节**——实测 `participating_rows` 的 750
    把「第1回合【剩余战舰】」也框了进去，于是同一批数量被读了两遍。
    """
    sections: list[tuple[int, int]] = []
    for index, (_start, end) in enumerate(bands):
        following = bands[index + 1][0] if index + 1 < len(bands) else bottom
        section_top = end + padding
        section_bottom = following - padding
        if section_bottom > section_top:
            sections.append((section_top, section_bottom))
    return sections
