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
from evo_helper.vision.mail_unread import (
    WARMTH_BUCKET_WIDTH,
    WARMTH_BUCKETS,
    MailRowColor,
)
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
from evo_helper.vision.scan_reading import (
    COORD_RECIPES,
    COORD_WHITELIST,
    vote_coordinate,
)

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

#: 邮件行裁片比读数 ROI 各边多留这么多像素。理由见 `ImageReportScreens.mail_row_crops`。
#: **这不是偏好项**：留少了事后分不出「本来就是白的」和「框把颜色切掉了」。
MAIL_ROW_EVIDENCE_PAD = 8

#: 未读色量在**自对齐的标题带**上，不是整行。下面这一组常量全部量于
#: 2026-09-08 的四屏信箱列表页实拍（标定视口 879 空间，27 行），**不是调出来的**。
#:
#: 为什么不量整行：黄字「攻击报告」只有约 **434** 个墨迹像素，而整行 ROI
#: （`ReportLayout.mail_row`）是 **44,200** 像素 —— 一个 R−B ≈ +220 的强信号
#: 被稀释到 1%。同一批样本上三种取法的实测对比：
#:
#: ====================  ==========  ==========  ==========  ======
#: ROI 做法              面积        未读最小    已读最大    比值
#: ====================  ==========  ==========  ==========  ======
#: 整行                  44,200 px   0.0153      0.0092      1.7×
#: 标题带，按名义行顶     1,296 px   0.0000      0.0000      分不开
#: **标题带，自对齐**     1,296 px   **0.2137**  **0.0054**  **40×**
#: ====================  ==========  ==========  ==========  ======
#:
#: 「按名义行顶」那一档为什么归零，见 `_mail_time_bands` 的 docstring。

#: 时刻格所在的列（x）。**每一行都有时刻**，而且是高对比白字 —— 整屏最好定位的
#: 东西，所以自对齐拿它当锚点。
MAIL_TIME_COLUMN = (1060, 1200)

#: 时刻格「算不算白字墨迹」的灰度门槛，以及一行里至少要有几个这样的像素。
#:
#: ⚠️ 这个门槛**只用来定位行**，不参与「暖不暖」的判断：暖色直方图仍旧统计
#: 标题带里**全部**像素（`_band_color`）。两件事分开的理由在
#: `vision.mail_unread` 的模块头 —— 挑墨迹要一个亮度门槛，而拿它去筛暖色
#: 就等于把标定的自由度提前焊死。定位是另一回事：定位错了整行都不算。
MAIL_TIME_INK_THRESHOLD = 200
MAIL_TIME_MIN_INK_PIXELS = 4

#: 一段墨迹至少这么高才算一条时刻带。时刻字高实测十来像素，而面板描边只有两三像素。
MAIL_TIME_MIN_BAND_HEIGHT = 6

#: 标题带：横向范围，以及相对**时刻带顶**的纵向偏移与高度。
#:
#: ⚠️ **x 不许往左到 792。** `x 792..812` 是信封图标上那个**红色角标**，它也是暖色，
#: 而且**每一行都有** —— 收进来会把已读那一侧整体抬起来，空档当场没了。
MAIL_TITLE_COLUMN = (818, 890)
MAIL_TITLE_BAND_DY = -26
MAIL_TITLE_BAND_HEIGHT = 18

#: 扫时刻带时，在列表区上下各多扫这么多行。
#:
#: 多扫是为了**看见列表区之外那半行**、好把它整条丢掉：滚过之后列表顶上常挂着
#: 上一行的下半截，它的时刻带完整地落在列表区上方。只扫列表区的话，那半行的
#: 时刻带会被扫描边界切成一条「顶端 = 列表顶」的假带，反而顶到第 0 行上。
MAIL_TIME_SCAN_MARGIN = 30

#: 一行邮件的三段文字**各自的墨迹**相对**时刻带顶**的纵向范围。
#:
#: 实测 2026-09-12 离线量在七屏实拍（`var/logs/sample-mail*.png` 四屏、
#: `var/mail-list.png` 一屏，共 42 行）上：主题 41/42 行落在 −30..−18
#: （唯一的例外是被面板上沿切掉的那半行），发件人与时刻 42/42 行一致。
#:
#: ⚠️ **这三段加起来只有 39 像素（−30..+9），而名义行 ROI 高 85、行距 86。**
#: 也就是说一行里**有 46 像素是空的**，而那 46 像素全在文字下方 —— 文字挤在
#: 行的上三分之一。`mail_rows()` 读的名义 ROI 因此**没有向上的余量**：列表一离
#: 网格（`_mail_time_bands` 里那几个 +10..+82 的实测偏移），主题就被 ROI 上沿
#: 横着切掉。整段实测见 `mail_row_aligned`。
MAIL_SUBJECT_INK_DY = (-30, -18)
MAIL_SENDER_INK_DY = (-18, 3)
MAIL_TIME_INK_DY = (0, 9)

#: 自对齐重读一行时，ROI 上沿取「这一行自己的时刻带顶 − 这个数」。
#:
#: 安全窗口由上面那三段算出来，**不是调出来的**（ROI 高 85、行距 86）：
#:
#: - 自己的主题不许被上沿切掉：``dy ≥ 30``（留 2 像素余量 ⇒ 32）
#: - 自己的时刻不许被下沿切掉：``dy + 9 ≤ 85`` ⇒ ``dy ≤ 76``
#: - 下一行的主题不许挤进来：``dy + (86 − 30) ≥ 85`` ⇒ ``dy ≥ 29``
#: - 上一行的时刻不许挤进来：``dy − (86 − 9) ≤ 0`` ⇒ ``dy ≤ 77``
#:
#: 取 48：落在 ``[32, 76]`` 里，两头各留 16 / 28 像素，而且正好是列表**停在顶部
#: 不动时**实测的偏移（`var/mail-list.png` 六行量到 +48/+49）—— 也就是说自对齐
#: 读出来的那一屏，和今天「碰巧对上了网格」的那一屏是同一个框。
MAIL_ROW_ALIGNED_DY = 48

#: 「获得资源」那 12 格**不走 tesseract**，走 `vision.resource_digits` 的字模匹配。
#:
#: ⚠️ **这一段原先是四套 tesseract 配方 + 两套谈拢，2026-08-18 整段换掉了。**
#: 换掉的理由不是「读不全」，是「读得不对」：34 份实拍（408 格逐格人工核过真值）上，
#: 老配方只有 10 份 12 格齐全，而那 10 份里只有 5 份逐格正确——生产库里已经因此
#: 存进过两个错数。完整的实测对比写在 `vision.resource_digits` 的模块头。
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
        #: 时刻带定位结果的缓存。**一个实例只读一屏**，所以这个答案是常量。
        #:
        #: 缓存不是省时间的优化，是省**一整趟信箱**的：定位要在纯 Python 里逐行
        #: 数墨迹（列表区 ~560 行 × 140 列），而现在问它的地方有四处（未读色、
        #: 标定探针、离网格量、自对齐重读）。不缓存的话，加一处取证就等于给每一屏
        #: 再加一遍全扫描，而一趟要翻八屏。
        self._time_bands: dict[int, int] | None = None

    # -- ReportScreens ---------------------------------------------------

    def mail_rows(self) -> list[str]:
        return [
            self._read(self._layout.mail_row(index), OCR_PSM_COLUMN)
            for index in range(self._layout.mail_visible_rows)
        ]

    def mail_row_colors(self) -> tuple[MailRowColor | None, ...]:
        """每一行标题带的颜色读数，供「未读 / 已读」判据用。见 `vision.mail_unread`。

        一行一个读数，**下标就是 `mail_rows()` 的下标**（点击坐标也按它算）。
        **和 `mail_rows()` 读的是同一帧**——它们必须是，否则「第 2 行是未读」会被
        安到另一屏的第 2 行上（`_report_screens` 头上「每次重新建」那条注释防的正是
        这件事，只是方向相反）。同一个实例保证了这一点。

        ⚠️ **定位不到时刻带的行交回 `None`（判不出），绝不回落到名义行顶。**
        回落等于在离网格的那几屏上给出一个确定的错答案（实测归零，两侧分不开），
        而 `None` 在策略层的意思是「按改动之前的行为办」。整段在 `_mail_time_bands`。

        代价：一次灰度遍历（定位）加每行两次全通道遍历（`ImageChops` 与
        `convert("L")` 都在 C 层跑），而标题带只有 72×18——**比一次窄 ROI 的 OCR
        还便宜**，而它省下的是一封 ≈20 秒的开封。所以不做「按需才测」的懒加载：
        那样会引入「这一屏测过没有」的状态，而这个类的全部约定就是「一个实例只读一屏」。
        """
        bands = self._mail_time_bands()
        return tuple(
            self._title_color(bands.get(index)) for index in range(self._layout.mail_visible_rows)
        )

    def _mail_time_bands(self) -> dict[int, int]:
        """每一名义行**自己那条时刻带的顶端**；定位不到的行不在字典里。

        ## ⚠️ 为什么颜色不能按名义行顶取

        滚轮滚过之后列表**离网格**：名义行顶和真实行内容错开十几到几十像素
        （四屏实测偏移 +11 / +23 / +38 / +82）。标题带只有 18 像素高，错开
        十几像素就整条落到行距的空白上——实测**对齐那一屏取到 0.233、离网格的
        两屏取到 0.000**，于是「未读最小占比」被拉到 0，两侧再也分不开。

        ⚠️⚠️ **原先这里写着「`mail_rows()` 的 OCR 不受这件事影响，因为它的 ROI
        有 85px 高、装得下这点漂移」——2026-09-12 的离线实测推翻了这句话。**
        85px 的高度不是余量：一行的文字只占 `MAIL_SUBJECT_INK_DY` 到
        `MAIL_TIME_INK_DY` 那 39 像素，全挤在行的上三分之一，剩下 46 像素的空白
        在**文字下方**。于是**向上一个像素的余量都没有**：同一批七屏 42 行上，
        偏移 ≥ 30 的 18 行认出 17 行，偏移 < 30 的 24 行只认出 5 行——而那 5 行
        （偏移 +10 / +11）读到的还是**下一行**的主题，只因为同屏主题都一样才
        没露馅。
        文字这一侧受的影响不比颜色小，只是失效形态不同——颜色归零看得见，
        主题读成噪声则一路装成「认不出的邮件」滑过主题闸。整段与那次的补法在
        `mail_row_aligned`。

        ## 锚点取时刻格

        每一行都有时刻，白字高对比（`MAIL_TIME_COLUMN`），是整屏最好定位的东西；
        标题带随即取「时刻带顶 − 26」（`MAIL_TITLE_BAND_DY`，四屏 27 行一致）。

        ## 带子怎么归到行上

        判据是**时刻带的顶端落在哪一名义行的纵向范围里**，而不是「标题带落在哪里」：
        对齐那一屏第 0 行的标题带是 203..221，比名义行顶（205）还高 2 像素，
        按标题带归位会把一行真未读整条丢掉。

        这条归位同时办了两件必须办的事：

        1. **把列表区之外的半行整条丢掉。** 实拍 `sample-mailunread-03` 顶上那条
           时刻带在 y=203，它属于已经滚出列表的上一行；照它算出来的标题带是
           177..195，已经出了列表区，量到 **0.0054** —— 那是全部已读样本里唯一的
           非零值（捞到的是页签上的橙色角标）。它的顶端落在第 0 行之上，所以不归任何行。
        2. **让颜色和 OCR 落在同一行上。** 归位用的是名义行的范围，而 OCR 读的
           就是那个范围，所以两边挑中的是同一行。

        一行匹配到两条带（不该发生，除非扫到了列表之外的亮东西）时**交回「定位不到」**：
        两条带里挑一条就是在猜，而猜错的方向是「给出一个确定的错答案」。

        ⚠️ **残余风险，写明不藏**：离网格很深时（实测 +82），一行的名义 ROI 里
        装着的是它自己的标题、却可能是**上一行**的时刻文字（那一行的时刻带贴着
        ROI 下沿被切掉）。那是名义行 ROI 本身的老毛病（点击坐标也按名义行算），
        不是这里引入的；日常那趟每次进信箱都先拖回顶部正是为了压住它。
        """
        if self._time_bands is not None:
            return self._time_bands
        grey = self._image.convert("L")
        pixels = grey.load()
        layout = self._layout
        last = layout.mail_row(layout.mail_visible_rows - 1)
        scan_top = max(0, layout.mail_first_row.top - MAIL_TIME_SCAN_MARGIN)
        scan_bottom = min(self._image.height, last.bottom + MAIL_TIME_SCAN_MARGIN)
        left, right = MAIL_TIME_COLUMN
        tops: list[int] = []
        band: list[int] | None = None
        for y in range(scan_top, scan_bottom):
            inked = sum(1 for x in range(left, right) if pixels[x, y] >= MAIL_TIME_INK_THRESHOLD)
            if inked >= MAIL_TIME_MIN_INK_PIXELS:
                band = [y, y] if band is None else [band[0], y]
                continue
            if band is not None:
                if band[1] - band[0] >= MAIL_TIME_MIN_BAND_HEIGHT:
                    tops.append(band[0])
                band = None
        if band is not None and band[1] - band[0] >= MAIL_TIME_MIN_BAND_HEIGHT:
            tops.append(band[0])

        located: dict[int, int] = {}
        for index in range(layout.mail_visible_rows):
            row = layout.mail_row(index)
            # 上界取闭区间：名义行距（86）比行高（85）大一像素，开区间会漏掉
            # 正好落在那道缝上的带子。相邻行的范围仍然不重叠。
            inside = [top for top in tops if row.top <= top <= row.bottom]
            if len(inside) == 1:
                located[index] = inside[0]
        self._time_bands = located
        return located

    def _title_color(self, time_band_top: int | None) -> MailRowColor | None:
        """一条时刻带对应的标题带颜色。带子没定位到、或标题带出画就交回 `None`。"""
        if time_band_top is None:
            return None
        top = time_band_top + MAIL_TITLE_BAND_DY
        bottom = top + MAIL_TITLE_BAND_HEIGHT
        if top < 0 or bottom > self._image.height:
            return None
        left, right = MAIL_TITLE_COLUMN
        return self._band_color(Region(left, top, right, bottom))

    def mail_title_band_offsets(self) -> tuple[int | None, ...]:
        """每一名义行的**离网格量**：这一行的时刻带顶 − 名义行顶。定位不到就 `None`。

        下标就是 `mail_rows()` 的下标，和它读的是同一帧（同一个实例）。

        这是「主题读不读得出」唯一的判别量：一行的文字只占时刻带顶的
        −30..+9（`MAIL_SUBJECT_INK_DY` 那三段），名义 ROI 是 0..85，所以

        - 偏移 < 30 ⇒ 主题被 ROI 上沿横着切掉 ⇒ 读成噪声 ⇒ 归 `UNKNOWN`；
        - 偏移 ≲ 20 ⇒ 主题整条在 ROI 之外，ROI 里装的是**下一行**的主题
          ⇒ 读得出、但读的是别人的（同屏主题都一样时这件事看不出来）。

        ⚠️ **它只描述，不裁决。** 交出去是给取证与日志用的；拿它去决定
        「这一行开不开」等于把一个观测量当成判据，而主题读不出时**正确的方向
        是把主题读准，不是收紧闸门**（整段在 `tools.pirate_loop.MailRow.unread`
        上方那条「读不出绝不能往不开那一侧倒」）。
        """
        bands = self._mail_time_bands()
        layout = self._layout
        return tuple(
            None if (top := bands.get(index)) is None else top - layout.mail_row(index).top
            for index in range(layout.mail_visible_rows)
        )

    def mail_row_aligned(self, index: int) -> str | None:
        """把整行 ROI **按这一行自己的时刻带对齐**之后再读一遍。定位不到交回 `None`。

        ⚠️ **现在只给取证用，不参与任何判断。** `mail_rows()` 一个字都没改。
        它在这里的用途是回答实机上那个问不出来的问题：一行主题读不出的时候，
        **同一帧、同一套配方**、只把 ROI 的上沿换成自对齐，到底读不读得出来。
        没有这一问，日志里只剩一串噪声字符串，而噪声既可能是「框切错了」，
        也可能是「那几个像素本来就糊」——两者的处置完全相反。

        框宽（520）、放大（`ocr_upscale`）、不二值化、`--psm 6` 全部原样照抄
        `mail_rows()`，**唯一的差别是 ROI 的纵向原点**。这是刻意的：一次只换
        一个变量，交回来的读数才说得清是哪一个变量在起作用。

        离线实测（2026-09-12，七屏 42 行，真值人眼核过，判据用
        `classify_report_subject`）：

        ==========================  ==========  ==========
        ROI 纵向原点                主题认出    时刻读出
        ==========================  ==========  ==========
        名义行顶（今天生产用的）      22/42       35/42
        自对齐，``dy = 48``          **32/42**   41/42
        ==========================  ==========  ==========

        ⚠️ **`dy` 不是调出来的**：39 / 45 / 48 / 54 / 60 五档跑下来是
        33 / 32 / 32 / 31 / 31，差一行的抖动而已——它落在
        `MAIL_ROW_ALIGNED_DY` 那个算出来的窗口里就行，不必标定。

        剩下那 10 行**不是对齐能救的**（列表上下沿真的只露了半行、以及背景
        透上来的幽灵字），配方那一侧还值不值得动，要等实机取证的裁片，
        见 `tools.pirate_loop.PirateLoop._record_unreadable_subject_evidence`。

        实拍不进 Git，所以这张表在仓库里复算不了——量它的那批图在主仓
        `var/logs/sample-mail*.png` 与 `var/mail-list.png`。
        """
        bands = self._mail_time_bands()
        top = bands.get(index)
        if top is None:
            return None
        row = self._layout.mail_row(index)
        return self._read(row.shifted(top - MAIL_ROW_ALIGNED_DY - row.top), OCR_PSM_COLUMN)

    def mail_row_crops(self, pad: int = MAIL_ROW_EVIDENCE_PAD) -> tuple[Any, ...]:
        """每一行的**原分辨率**裁片，只给标定探针与诊断证据用（不喂 OCR）。

        裁的是**整个名义行再各边加一圈**，而不是读数用的那条 18px 标题带：
        读数带是自对齐算出来的，而「自对齐算错了没有」正是事后最要查的一件事——
        只存那条带子，等于把要复核的东西提前裁掉了。整行裁片里既有标题也有时刻格，
        照 `_mail_time_bands` 的做法能整条复算一遍。

        ⚠️ **比读数用的 ROI 大一圈**（`pad`）：贴着 ROI 裁的话，事后分不出
        「这一行本来就是白的」和「框把带颜色的那一截切掉了」。整条教训在
        `tools.scan_coordinates.crop_png_base64` 与 `pirate_loop._value_box_evidence`
        上——那一次是缩略图糊掉了字，这里换成框切掉了色，失效方式一样。

        裁片**不做任何预处理**（不缩放、不转灰、不二值化）：标定要调的正是
        「暖到哪一档算暖」，先处理一道等于把那个自由度提前焊死。
        """
        width, height = self._image.width, self._image.height
        crops: list[Any] = []
        for index in range(self._layout.mail_visible_rows):
            left, top, right, bottom = self._layout.mail_row(index).as_box()
            box = (
                max(0, left - pad),
                max(0, top - pad),
                min(width, right + pad),
                min(height, bottom + pad),
            )
            crops.append(self._image.crop(box).convert("RGB"))
        return tuple(crops)

    def _band_color(self, region: Region) -> MailRowColor:
        """一条带子的暖色直方图与平均亮度。**带内全部像素，不先挑墨迹。**

        挑墨迹要一个亮度门槛，而那个门槛和颜色阈值一样没标定过；用它就等于
        在测量这一步先做一次未标定的判断。理由整段在 `vision.mail_unread` 的模块头。
        （定位那一步用的 `MAIL_TIME_INK_THRESHOLD` 不算：它决定的是「量哪一块」，
        不是「这一块暖不暖」。）

        暖色用 `R − B` 而不是转 HSV 取色相：`ImageChops.subtract` 天然在 0 处截断
        （白字与偏蓝的面板底色因此落在最低那一桶），而这个量在本仓已经标定过一次
        ——胜负横幅就是量在 R−B 上的（`OUTCOME_INK_THRESHOLD`：横幅峰值 155/192，
        幽灵文字与面板背景不超过 10）。同一块面板、同一套配色，换个量没有好处。
        """
        from PIL import ImageChops

        crop = self._image.crop(region.as_box()).convert("RGB")
        pixels = crop.width * crop.height
        if pixels <= 0:  # pragma: no cover - ROI 落在画面外时才可能
            return MailRowColor(pixels=0, warmth_buckets=(0,) * WARMTH_BUCKETS, mean_luminance=0)
        red, _green, blue = crop.split()
        warmth = ImageChops.subtract(red, blue).histogram()
        buckets = tuple(
            sum(warmth[index * WARMTH_BUCKET_WIDTH : (index + 1) * WARMTH_BUCKET_WIDTH])
            for index in range(WARMTH_BUCKETS)
        )
        grey = crop.convert("L").histogram()
        mean = round(sum(value * count for value, count in enumerate(grey)) / pixels)
        return MailRowColor(pixels=pixels, warmth_buckets=buckets, mean_luminance=mean)

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
        """四套配方**各读一遍**，取票数最多的那个三元组。

        单套配方在这一行上不够。实测同一屏、同一形状的两个 ROI，守方读出
        `[2:137:14]`，攻方却只读出 `]`——于是 `parse_versus_block` 判成「单边战报」
        并整份拒收，而战报本身是好的。

        两条对策与坐标扫描器那边同源（`vision.scan_reading.COORD_RECIPES`）：
        方括号从白名单里去掉（`]` 会被读成数字，反过来也会吃掉相邻字符），
        放大兼用最近邻（LANCZOS 会把细笔画之间的缝插值糊掉）。

        ⚠️ **不再「第一套读出三元组就采信」。** 那个规则的失效方式是**读错但读得
        像模像样**：第一套配方给出一个合法三元组，后面三套的一致反对被整个丢掉。
        实机（生产库 2026-08-18）第二颗出发星 `9:250:8` 的 7 份战报**全部**栽在
        这里——`[9` 在 7× LANCZOS 下糊成一个 `3`，读出 `3:250:8` / `39:250:8`，
        而 7n / 3n / 3l 三套都读对。7 份战报因此一份都认不上派遣。

        投票的实测依据（13 份存档面板 × 攻守两侧 = 26 次读数，2026-08-19）：

        - 与「第一套即采信」的结论**只在那 2 次上不同**，而那 2 次正是读错的。
        - 26 次里**没有一次平票**；也没有一次因为投票而丢掉原本读得出的坐标
          （只有一套读得出时，1 票就是唯一最高票，仍然采信——上面那条「守方读出、
          攻方只读出 `]`」的救援因此原样保留）。

        ⚠️ **平票返回空串**，也就是「没读出来」，整份战报被 `parse_versus_block`
        拒收、下一趟重读。这里**不学 `fleet_counts._plurality` 的「平票取小」**：
        那边平票的两个值是同一个数量的两种读法，取小是保守；这边平票的两个值是
        两颗**不同的星球**，取哪个都是在猜，而认错出发点会把战果记到别人头上。
        """
        reads = [
            self._read(
                region,
                OCR_PSM_LINE,
                language="eng",
                whitelist=COORD_WHITELIST,
                scale=scale,
                resample=resample,
            ).strip()
            for scale, resample in COORD_RECIPES
        ]
        return vote_coordinate(reads)

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
