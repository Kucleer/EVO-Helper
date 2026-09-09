"""现场缩略图的编码：存 webp，而且要说清自己是 webp。

## 为什么动它

2026-09-09 量的生产库：`system_log` 16.4 万行、245 MB，其中 payload 121 MB。
payload 超过 10 KB 的行只有 831 条（0.507%），却占掉 payload 体积的 **89.7%**
（108 MB）——全是 `thumbnail_png_base64` 这一个键，480×229 的 PNG，一张 79–124 KB。

同一批图（40 张真实缩略图）换成 webp 之后：PNG 平均 84.5 KB → q60 平均 4.9 KB。
待办二早就写着该这么做（「**转 webp**，别直接塞 PNG」），先例是
`battle_report_screenshots`。

## 老行不许变成打不开的

库里已经有 846 条是 PNG 时代写下的，payload 里**没有**格式那个键。所以：

- 键名 `thumbnail_png_base64` **不改**（虽然现在装的是 webp）——它是读取侧找图
  的唯一线索，改名等于让那 846 条整批消失，而且这个名字还写在查询层的
  「有没有图」判断里；
- 新行多写一个 `thumbnail_image_format`。读取侧「没这个键就按 PNG 认」，
  老行就照旧回放。理由与 `battle_report_screenshots.image_format` 一字不差：
  接口靠它填 `Content-Type`，猜错就是浏览器直接下载而不显示。
"""

from __future__ import annotations

import base64
import io
import json

from PIL import Image, ImageDraw

from evo_helper.tools.scan_coordinates import (
    EVIDENCE_THUMBNAIL_FORMAT,
    EVIDENCE_THUMBNAIL_QUALITY,
    thumbnail_base64,
    thumbnail_evidence,
)
from evo_helper.web.display import payload_image

#: webp 文件头：`RIFF` + 4 字节长度 + `WEBP`。判编码只认字节，不认键名。
WEBP_MAGIC = (b"RIFF", b"WEBP")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _frame() -> Image.Image:
    """一张 1920×917 的假整帧，画得像游戏画面（星空底 + 面板 + 几个圆）。

    纯色或纯噪声都会把压缩比带偏：纯色两种编码都几乎为 0，纯噪声 PNG 会大得
    离谱。这里要的是「和实机同一个量级」的对比。
    """
    image = Image.new("RGB", (1920, 917), (8, 12, 30))
    draw = ImageDraw.Draw(image)
    for x in range(0, 1920, 7):
        draw.line([(x, 0), (x + 40, 917)], fill=(20 + x % 60, 30, 70 + x % 90))
    draw.rectangle([600, 20, 1300, 120], fill=(30, 60, 110), outline=(200, 220, 255), width=3)
    for x in range(640, 1280, 90):
        draw.ellipse([x, 400, x + 70, 470], fill=(180, 190, 210))
    return image


def test_the_thumbnail_is_really_a_webp() -> None:
    """判编码只认字节。键名还叫 `..._png_base64`，光看名字会得出相反的结论。"""
    raw = base64.b64decode(thumbnail_base64(_frame()))

    assert raw[:4] == WEBP_MAGIC[0]
    assert raw[8:12] == WEBP_MAGIC[1]
    assert EVIDENCE_THUMBNAIL_FORMAT == "webp"


def test_the_webp_is_a_fraction_of_the_png() -> None:
    """省下来的体积就是这次改动的全部理由，所以拿同一帧当场量一遍。

    实测（生产库 40 张真图）webp q60 是 PNG 的 5.8%；这张合成帧是 8.3%。
    断言留到三分之一，是给 Pillow 版本间的编码器差异留余量——一旦哪天这一条红了，
    说明省不到东西了，那就该重新挑质量档，而不是把断言放宽。
    """
    frame = _frame()
    scaled = frame.resize((480, round(frame.height * 480 / frame.width)))
    as_png = io.BytesIO()
    scaled.save(as_png, format="PNG")
    as_webp = io.BytesIO()
    scaled.save(as_webp, format="WEBP", quality=EVIDENCE_THUMBNAIL_QUALITY)

    assert len(as_webp.getvalue()) * 3 < len(as_png.getvalue())


def test_the_payload_carries_the_encoding_next_to_the_picture() -> None:
    """图和它的编码要成对出现，不然将来换编码时这一批又会变成打不开的。"""
    evidence = thumbnail_evidence(_frame())

    assert set(evidence) == {"thumbnail_png_base64", "thumbnail_image_format"}
    assert evidence["thumbnail_image_format"] == "webp"


def test_the_key_name_stays_put() -> None:
    """⚠️ 键名是**故意不改**的，尽管它现在名不副实。

    163k 行历史数据、查询层的「有没有图」判断、显示层的「摘出来另行渲染」，
    认的都是这一个名字。改名换不来任何东西，只会让老行整批看不见图。
    """
    assert "thumbnail_png_base64" in thumbnail_evidence(_frame())


def test_a_frame_that_cannot_be_encoded_claims_no_format() -> None:
    """截不到图时只留空值键，**不许声明编码**——不然读取侧会拿它去填
    `Content-Type`，而那边根本没有图。

    「键在、值空」这个形状是 `tools.screen_diagnostics` 抓不到画面时的既有写法，
    读取侧把它当没有图；一个点不开的「现场图」链接比不显示更糟。
    """
    evidence = thumbnail_evidence(object())  # 没有 .width / .convert，编码必然失败

    assert evidence == {"thumbnail_png_base64": ""}


def test_the_png_rows_written_before_this_change_still_replay() -> None:
    """⚠️ 库里那 846 条老行必须照旧打得开。

    老行的形状就是「只有 `thumbnail_png_base64`、没有格式键」。这里拿真的 PNG
    走一遍现在的读取路径（`web.display.payload_image`），断言它仍然认得出这是
    一张图、并且原样把那串 base64 交出去。

    ⚠️ 这条**不管 `Content-Type` 填得对不对**——那是读取侧那一半（页面/接口），
    判据是「没有格式键就按 PNG 认」。这里守的是更基本的一件事：老行不许变成
    「库里还在、页面上打不开」。
    """
    buffer = io.BytesIO()
    _frame().resize((480, 229)).save(buffer, format="PNG")
    legacy_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    assert buffer.getvalue()[:8] == PNG_MAGIC, "这条用例的前提：老行装的是 PNG"

    legacy_row = json.dumps({"nav_text": "", "thumbnail_png_base64": legacy_base64})
    rendered = payload_image(legacy_row)

    assert rendered.startswith("data:image/")
    assert legacy_base64 in rendered
