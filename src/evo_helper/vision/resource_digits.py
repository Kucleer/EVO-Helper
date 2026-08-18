"""「获得资源」那 12 格的数字识别：**字模匹配，不走 tesseract**。

## 为什么这一块不能交给 tesseract

这一格的字高只有 **9 像素**（一个 `0` 的墨迹是 7×9）。2026-08-18 用 34 份实拍
战报（`tests/fixtures/vision/battle_report_panels/`，全部 408 格逐格人工核过真值）
量下来，tesseract 在这个尺寸上**既读不全、又读得不对**：

- 逐格「裁到墨迹 + 放大 + 补黑边 + psm 7 + 两套配方谈拢」这套老配方：
  34 份里只有 10 份 12 格齐全，而那 10 份里**只有 5 份逐格正确**。
- 把 45 套配方（psm 7/8/13 × lanczos/bicubic/nearest × 4/6/8/10/16 倍）全跑一遍
  再取多数票，最好也就是「读全 24 份、其中只有 5 份全对」——**读全的份数涨了，
  错的份数涨得更快**。
- 孤零零一个 `0` 是最难的一格：任何倍数、任何 psm、任何预处理下，tesseract 给的
  都是 `''` / `0` / `5` / `2` 之间的一次抛硬币。而 slot 4 / 10 / 11 常年就是一个 `0`。

⚠️ **代价不是「少一份数据」，是「多一份假数据」。** 生产库里那 5 份存下了收获的
战报，逐格核对之后有 **2 份是错的**（`486.2K` 存成 `466200`、`272K` 存成 `72000`）——
入库之后没有任何办法回头分辨，因为一个读错的 `466.2K` 和真的 `466.2K` 长得一模一样。

## 为什么字模匹配可行

游戏这一块用的是**固定位图字体**：34 份实拍、408 格、1178 个字形，行带高度
**无一例外都是 9 像素**，同一个字符的墨迹逐像素几乎重合。所以这里不做通用 OCR，
只做一件事：把这十几个已知字形认出来。

做法分三步（都在 `read_resource_cell` 里）：

1. **定行带**：亮度过 `INK_THRESHOLD` 的算墨迹，取墨迹的上下界。高度必须正好
   `GLYPH_HEIGHT`——不是就整格作废（见下面「行带高度是判据」）。
2. **归一化**：`(亮度 − INK_THRESHOLD) / (255 − INK_THRESHOLD)` 截断到 0..1。
   门槛以下一律压成 0，于是那层 `-TOTAL CREWS` / `-17003` 幽灵文字（实测最高 102）
   和透出来的星球亮边整个消失，只剩笔画自己的灰阶。
3. **动态规划切字**：`best[x]` 是「把前 x 列解释完」的最优对数似然，转移是「在这里
   放某个字模」或「跨过一列全空的缝」。**不预先按空列切段**——`711.5K` 里那两个
   `1` 之间没有空列，按段切会把它们并成一个字形（实测 34 份里就有一份栽在这上面）。

## ⚠️ 行带高度是判据，不是巧合

408 格全是 9，一格都没例外。所以「高度不是 9」意味着**版面动了**（网格位移、
面板滚了、或者 ROI 吃进了隔壁的东西），这时候读出来的任何数字都不可信。
拒收比读出一个像模像样的错数安全得多——后者进了库就再也认不出来。

## ⚠️ 字模是从实拍统计出来的，不是画出来的

`RESOURCE_GLYPHS` 里每个字模是**同一字符在语料里所有实例的逐像素灰度均值**，
量化成 0..9 十档（量化前后准确率只差一格，见下面的实测数字）。
它是**观测的记录**，改它等于篡改观测——真要改，只能是重新跑一遍统计。
生成脚本的做法写在 PR 里：按人工真值把每格切成字形、按字符归堆、逐像素取均值。

**语料里没出现过的字符没有字模**——目前是 `B`（十亿）。真收到十亿量级的数字时，
似然会掉到 `MATCH_FLOOR` 以下，那一格返回空串、整块作废。**这是对的**：宁可
丢一份收获，也不要把 `1.2B` 认成 `1.2K`。

## 实测（34 份实拍、408 格，全部人工核过真值）

===================================  ==========  ==========
读法                                 12 格齐全    逐格全对
===================================  ==========  ==========
老配方（tesseract，两套谈拢）           10          5
45 套配方多数票                         24          5
**字模匹配（本模块）**                  **34**      **29**
===================================  ==========  ==========

留一法（被读的那份不参与统计字模）结果一致，说明字模学到的是字体本身，
不是这批样本。

⚠️ **剩下 5 格仍然读错**（`251K`→`291K`、`466`→`468`、`28`→`29`、`573K`→`579K`、
`563K`→`569K`），全部是 `3`/`9`、`6`/`8`、`5`/`9` 这几对形近字。408 格里错 5 格
（1.2%），比老配方好一个量级，但**不是零**。要再往下压只能提高截图质量：库里存的
是 WEBP q90 的有损图，实机读的是原始像素，本模块在原始像素上只会更准——但那一半
必须实机验证，离线证不了。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

#: 字形高度。行带量出来不是这个数就整格作废，理由见模块头。
GLYPH_HEIGHT = 9

#: 「算不算墨迹」的亮度门槛。
#:
#: ⚠️ **这不是偏好项。** 实测 12 格：数字笔画是纯白（255），而格子里其余部分
#: （`-TOTAL CREWS` / `personnel` 幽灵文字、透出来的星球亮边）**最高只到 102**。
#: 中间隔着一个数量级的空档，门槛落在哪都一样。它同时是归一化的零点：
#: 门槛以下的像素一律压成 0，幽灵文字因此整个消失。
INK_THRESHOLD = 150

#: 墨迹左右各多带几列真实背景像素。
#:
#: ⚠️ **不能省。** 字模是按字形的外接框统计的，可最后一个字形右边如果一列都不留，
#: 8 列宽的 `K` 就没地方落脚，DP 会拿 7 列宽的数字去顶——实测 `251K` 读成 `2512`、
#: `380K` 读成 `3802`、`272K` 读成 `2728`。留两列之后这一类错误全部消失。
BAND_PAD = 2

#: 灰度似然的标准差。像素差按 `exp(−Δ²/2σ²)` 折算成似然。
#:
#: 这不是调出来的：0.25 / 0.35 / 0.5 三档在 408 格上输出**完全一致**——字形之间
#: 的差别远大于同一字形的抖动，σ 只影响绝对分值，不影响排序。取中间那档。
MATCH_SIGMA = 0.35

#: 一格「像不像这套字体」的下限（每像素平均似然）。
#:
#: 实测 408 格的最低值是 **0.787**，最高 0.992。取 0.70 是给它留一档余量，
#: 而不是卡在样本边缘——这个门槛防的不是「认错了哪个数字」（那种情况似然照样很高），
#: 是**字模表里根本没有的字符**（眼下就是 `B`）：那时候 DP 会硬凑一串，
#: 似然会明显塌下来，这一格就该作废。
MATCH_FLOOR = 0.70

#: 字模表：字符 → 逐行的灰度模板，每个像素一位十进制（0=背景，9=纯白笔画）。
#:
#: ⚠️ **这是观测记录，不是画出来的图。** 每一格是「该字符在 34 份实拍语料里所有
#: 实例的灰度均值」，量化成十档。要改它只有一条路：重新跑一遍统计（做法见模块头），
#: **不要手工修像素**——手工改过的字模再也说不清它代表哪一批观测。
RESOURCE_GLYPHS: dict[str, tuple[str, ...]] = {
    ".": (
        "00",
        "00",
        "00",
        "00",
        "00",
        "00",
        "00",
        "00",
        "63",
    ),
    "0": (
        "05566400",
        "59999950",
        "68101860",
        "68000860",
        "68000860",
        "68000770",
        "68100870",
        "69757970",
        "18999820",
    ),
    "1": (
        "3650",
        "5881",
        "0481",
        "0381",
        "0381",
        "0381",
        "0381",
        "0381",
        "0381",
    ),
    "2": (
        "1666640",
        "0333683",
        "0000185",
        "0233694",
        "4998861",
        "6810000",
        "6700000",
        "6832210",
        "6998982",
    ),
    "3": (
        "2566640",
        "0112685",
        "0000285",
        "0011485",
        "0688996",
        "0000385",
        "0000185",
        "0112585",
        "4899971",
    ),
    "4": (
        "0015200",
        "0068100",
        "0185000",
        "0582371",
        "1860482",
        "3986896",
        "1334795",
        "0000481",
        "0000381",
    ),
    "5": (
        "3666651",
        "5831110",
        "5800000",
        "5864410",
        "4888982",
        "0000384",
        "0000184",
        "1112683",
        "6899960",
    ),
    "6": (
        "04566510",
        "48511100",
        "68000000",
        "68311000",
        "69878820",
        "68101860",
        "68100770",
        "58403860",
        "17888820",
    ),
    "7": (
        "3565652",
        "0111594",
        "0000481",
        "0001850",
        "0004820",
        "0017600",
        "0038200",
        "0076000",
        "0382000",
    ),
    "8": (
        "03555300",
        "38626830",
        "48202840",
        "38525830",
        "18888810",
        "58201850",
        "58100770",
        "58403860",
        "17988820",
    ),
    "9": (
        "0466630",
        "5842682",
        "5700283",
        "5810283",
        "3887893",
        "0011483",
        "0000283",
        "0112683",
        "3899860",
    ),
    "K": (
        "461004730",
        "581038600",
        "581177100",
        "584683000",
        "598971000",
        "582584000",
        "581078200",
        "581028710",
        "581003850",
    ),
    "M": (
        "3770000475",
        "4993000897",
        "4998005997",
        "4989318897",
        "3937878487",
        "3911996077",
        "4800781077",
        "3900000077",
        "3900000077",
    ),
}

_NEG = float("-inf")


def _templates() -> dict[str, list[list[float]]]:
    table: dict[str, list[list[float]]] = {}
    for char, rows in RESOURCE_GLYPHS.items():
        if len(rows) != GLYPH_HEIGHT:
            raise ValueError(f"字模 {char!r} 有 {len(rows)} 行，应当是 {GLYPH_HEIGHT} 行")
        width = len(rows[0])
        if any(len(row) != width for row in rows):
            raise ValueError(f"字模 {char!r} 的各行宽度不一致")
        table[char] = [[int(pixel) / 9.0 for pixel in row] for row in rows]
    return table


_TEMPLATES = _templates()


def ink_band(luminance: Sequence[Sequence[int]]) -> list[list[float]] | None:
    """裁出墨迹所在的那一条，并把亮度归一化到 0..1。没有墨迹、或高度不对返回 None。

    左右各多带 `BAND_PAD` 列**真实像素**（不是补零）：那几列本来就是背景，
    归一化之后自然是 0，而字模需要它们才有落脚的地方（见 `BAND_PAD`）。
    """
    if not luminance or not luminance[0]:
        return None
    height = len(luminance)
    width = len(luminance[0])
    rows = [y for y in range(height) if any(luminance[y][x] > INK_THRESHOLD for x in range(width))]
    if not rows:
        return None
    top, bottom = rows[0], rows[-1]
    if bottom - top + 1 != GLYPH_HEIGHT:
        # 版面动了。读出来的任何数字都不可信，整格作废。
        return None
    columns = [
        x
        for x in range(width)
        if any(luminance[y][x] > INK_THRESHOLD for y in range(top, bottom + 1))
    ]
    if not columns:
        return None
    left = max(columns[0] - BAND_PAD, 0)
    right = min(columns[-1] + BAND_PAD, width - 1)
    span = 255.0 - INK_THRESHOLD
    return [
        [
            min(1.0, max(0.0, (luminance[y][x] - INK_THRESHOLD) / span))
            for x in range(left, right + 1)
        ]
        for y in range(top, bottom + 1)
    ]


def _place(strip: list[list[float]], template: list[list[float]], left: int) -> float:
    """把一个字模放在第 `left` 列，算它的对数似然。放不下返回 `-inf`。"""
    width = len(strip[0])
    span = len(template[0])
    if left + span > width + 1:
        return _NEG
    total = 0.0
    scale = 2.0 * MATCH_SIGMA * MATCH_SIGMA
    for y, row in enumerate(strip):
        line = template[y]
        for x in range(span):
            column = left + x
            value = row[column] if column < width else 0.0
            delta = value - line[x]
            total -= delta * delta / scale
    return total


def decode_band(strip: list[list[float]]) -> tuple[str, float]:
    """在归一化好的条带上跑 DP，返回（读出来的串, 每像素平均似然）。

    ⚠️ **不按空列预先切段。** `711.5K` 里两个 `1` 之间没有背景列，切段会把它们
    并成一个字形，接着被当成某个 7 像素宽的数字——数字全对、只是少了一位，
    而这种错误在库里看不出来。DP 是逐列决定的，粘在一起的字形照样分得开。
    """
    height = len(strip)
    width = len(strip[0])
    best = [_NEG] * (width + 1)
    trail: list[tuple[int, str] | None] = [None] * (width + 1)
    best[0] = 0.0
    for x in range(width):
        if best[x] == _NEG:
            continue
        if all(strip[y][x] == 0.0 for y in range(height)):
            # 字与字之间的缝：跨过去不要钱。
            if best[x] > best[x + 1]:
                best[x + 1] = best[x]
                trail[x + 1] = (x, "")
            continue
        for char, template in _TEMPLATES.items():
            score = _place(strip, template, x)
            if score == _NEG:
                continue
            end = min(x + len(template[0]), width)
            if best[x] + score > best[end]:
                best[end] = best[x] + score
                trail[end] = (x, char)
    if best[width] == _NEG:
        return "", 0.0
    out: list[str] = []
    x = width
    while x > 0:
        step = trail[x]
        if step is None:
            return "", 0.0
        previous, char = step
        if char:
            out.append(char)
        x = previous
    likelihood = math.exp(best[width] / (height * width))
    return "".join(reversed(out)), likelihood


def read_resource_cell(luminance: Sequence[Sequence[int]]) -> str:
    """把一格的灰度像素读成数字原文；读不出返回空串。

    ⚠️ **空串是「没读出来」，不是 0。** 这一屏上值为 0 的格子照样画着一个 `0`；
    一格都认不出来说明格子挪了位或者字体换了，那时候补一个 0 是在编数据。
    整块作废由 `domain.battle_resources.parse_resource_grid` 决定，这一层不做。
    """
    strip = ink_band(luminance)
    if strip is None:
        return ""
    text, likelihood = decode_band(strip)
    if not text or likelihood < MATCH_FLOOR:
        return ""
    return text


__all__ = [
    "BAND_PAD",
    "GLYPH_HEIGHT",
    "INK_THRESHOLD",
    "MATCH_FLOOR",
    "MATCH_SIGMA",
    "RESOURCE_GLYPHS",
    "decode_band",
    "ink_band",
    "read_resource_cell",
]
