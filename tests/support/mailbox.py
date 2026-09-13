"""合成的信箱列表页，供「未读色」与「主题读不出」两组用例共用。

⚠️ **住在 `support` 下，不是某个测试文件里** —— 理由整段在 `support.attack_config`
（`tests.*` 根本不是一个可导入的包，跨测试包 import 本机绿、CI 收集失败）。
这张图现在有两拨用户：`unit/vision/test_mail_unread.py` 量颜色，
`unit/vision/test_mail_row_alignment.py` 量**框**。

⚠️ **合成图验的是通路与几何，不是「量得准」。** 颜色那一侧的阈值只有实拍能标定
（`tools.mail_unread_probe`），框那一侧的实测对比在
`vision.optional.report_screens.mail_row_aligned` 的 docstring 上——两处结论都
不许引这张图。
"""

from __future__ import annotations

from typing import Any

#: 半透明面板的底色，偏蓝（R < B）。`ImageChops.subtract` 在 0 处截断，
#: 所以它落在最低那一桶——这正是「白字和底色都不暖」成立的原因。
PANEL_BACKGROUND = (40, 44, 52)

#: 未读标题的黄橙色与已读标题的白色。**这两个值是照着实拍图肉眼描述画的，
#: 不是量出来的**：它们只用来验通路，任何「实机上分得开吗」的结论都不能引它。
UNREAD_TITLE = (236, 176, 48)
READ_TITLE = (240, 240, 244)

#: 信封上那个**红色角标**。合成图里必须有它：它每一行都在、也是暖色，而标题带的
#: 左界（`MAIL_TITLE_COLUMN`）刻意躲开了它。少画一块，「x 不许往左」那条边界
#: 就变成了没人守的注释。
ENVELOPE_BADGE = (200, 60, 52)

#: 合成图里时刻带顶相对名义行顶的基准偏移，`drift` 在它之上叠加。
#:
#: ⚠️ 24 是**实拍上真出现过的那一档**（`var/logs/sample-mailbox-221612.png`
#: 六行量到 +23/+24），而它正好落在「主题被 ROI 上沿切掉」那一侧（< 30）。
#: 换句话说**默认这张图就是出事的那一屏**；要画对得上网格的那一屏，
#: 传 `drift=+24` 往上推到 48。
BASE_TIME_BAND_OFFSET = 24


def build_mail_list_frame(
    unread_rows: set[int], *, drift: int = 0, time_column: bool = True
) -> Any:
    """一张标定视口大小的合成信箱列表页。

    按实拍量到的几何画三样东西：右侧的**时刻格**（自对齐的锚点）、标题带，
    以及信封上的红色角标。`drift` 把整个列表内容整体挪开名义行顶，模拟
    滚轮滚过之后**离网格**的那几屏——判据必须对它免疫。
    """
    from PIL import Image, ImageDraw

    from evo_helper.vision.optional.report_screens import (
        MAIL_TIME_COLUMN,
        MAIL_TITLE_BAND_DY,
        MAIL_TITLE_BAND_HEIGHT,
        MAIL_TITLE_COLUMN,
    )
    from evo_helper.vision.report_layout import LAYOUT_VIEWPORT, LIVE_LAYOUT

    image = Image.new("RGB", LAYOUT_VIEWPORT, PANEL_BACKGROUND)
    draw = ImageDraw.Draw(image)
    for index in range(LIVE_LAYOUT.mail_visible_rows):
        region = LIVE_LAYOUT.mail_row(index)
        # 时刻带的顶端落在这一名义行里，这就是归位判据认的东西。
        time_top = region.top + BASE_TIME_BAND_OFFSET + drift
        if time_column:
            draw.rectangle(
                (MAIL_TIME_COLUMN[0] + 4, time_top, MAIL_TIME_COLUMN[0] + 90, time_top + 12),
                fill=READ_TITLE,
            )
        title_top = time_top + MAIL_TITLE_BAND_DY
        colour = UNREAD_TITLE if index in unread_rows else READ_TITLE
        draw.rectangle(
            (
                MAIL_TITLE_COLUMN[0],
                title_top,
                MAIL_TITLE_COLUMN[1] - 1,
                title_top + MAIL_TITLE_BAND_HEIGHT - 1,
            ),
            fill=colour,
        )
        draw.rectangle((792, title_top, 812, title_top + 12), fill=ENVELOPE_BADGE)
    return image


def build_mail_list_screens(unread_rows: set[int], **kwargs: Any) -> Any:
    """把上面那张图包成 `ImageReportScreens`（用的是生产那份 `LIVE_LAYOUT`）。"""
    from evo_helper.vision.optional.report_screens import ImageReportScreens
    from evo_helper.vision.report_layout import LIVE_LAYOUT

    return ImageReportScreens(build_mail_list_frame(unread_rows, **kwargs), LIVE_LAYOUT)
