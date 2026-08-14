"""军力排行榜读出来的那一屏：怎么认、怎么自查、什么时候不许信。

这里一个像素都不点、一张图都不拍。坐标住在 `game.ranking_ui`，导航住在
`game.ranking_nav`；分开是为了让下面这几条判据可以脱离游戏被钉住。

## 榜单给了什么

排行榜「玩家 / 军事评分」那一页，翻过约 638 名真人之后开始全是 bot。每一行是

    [639]   bot_4_30_12   ---   29.59K
     名次      名字        联盟   军力值

**名字直接编码坐标**：`bot_<银河>_<恒星系>_<行星>`。用户库里 435 个已扫 bot 验过，
400 个的名字反解与扫描记录一致。所以这张榜同时给出**坐标 + 军力值**，而拿到一个
bot 坐标的成本从「逐坐标导航读面板」降到「一屏读 13 行」。

## 精度预算全给名字

用户口径（2026-08-14）：「基于榜单排序，我不需要精确值……甚至有排名 我接受读不出值」。

于是三列的地位完全不同：

| 列 | 地位 | 读错的后果 |
|---|---|---|
| **名字** | 决定舰队飞去哪 | **往一个不存在或不相干的坐标派兵** |
| 名次 | 排序键 + 校验和 | 顺序乱 |
| 军力值 | 可有可无 | 无（插值或留空即可） |

实测名字 OCR 错误率约 8%（形态是数字重复：`121→1121`、`12→122`），所以
`coordinate_of` 那道区间校验是**硬闸**，不是防御性编程。

## 两个校验和，以及一个**不能用**的

榜单有序，于是自带纠错：

1. **名次必须逐行 +1** —— 永远成立。实机 2026-08-14 那一屏 14 行里，它当场抓出
   两处错读（`637→375`、`643→5`），而且因为夹在正确的邻居中间，**能修回来**。
2. **军力值必须非递增** —— 永远成立（榜单按它排序）。但它只挡得住大错：实测
   `28.67K` 读成 `28.57K`、`27.3K` 读成 `27.45K`，两处都仍然保持降序，**抓不住**。
   按用户口径这无所谓，但不要以为有了它军力值就准了。
3. ⚠️ **「恒星系号连续」不能用。** 我一度以为可以——那是在**经济评分**页上看到的：
   bot 在经济榜上全是 0 分，并列时退化成坐标序，于是 1:1:5 / 1:2:20 / 1:3:9 一路连续。
   而军事榜上 bot 有真实分数、按分数降序，坐标是乱的（30 / 100 / 183 / 160 / 360…）。
   **拿错页签就会拿到完全不同的数据**，这条记在这里免得下一个人重犯。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scan_bounds import SYSTEMS_PER_GALAXY, TOTAL_GALAXIES

#: 行星位号的上界。`domain.scan_bounds` 只定义了银河数与每银河的恒星系数，
#: 位号上界散落在扫描计划里，这里显式写出来当校验用。
POSITIONS_PER_SYSTEM = 20

#: 名次自纠时，至少要几行认同同一个偏移量才敢动手。
#:
#: 取 2 而不是 1：只有一行认同时，那一行既可能是唯一读对的、也可能是唯一读错的——
#: 两种情形在数据上一模一样，而按错的那个重算会把整屏都改成错的。宁可不改。
_MIN_ANCHORS = 2

#: bot 名字的形状：`bot_<银河>_<恒星系>_<行星>`。
#:
#: 分隔符写成 `[_\s]+` 而不是死的下划线：OCR 会把 `_` 读成空格或读丢，而
#: 三个数字的相对顺序是稳的。宽在分隔符上不会引入歧义——真正的把关是下面
#: 那道区间校验。
_BOT_NAME_RE = re.compile(r"bot[_\s]+(\d+)[_\s]+(\d+)[_\s]+(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class RankingRow:
    """榜单上的一行，**照原样记下来，判定留给别处**。

    `coordinate` 为 None 有两种来源，而它们在这里合流是对的：名字读不出来，
    或者读出来了但反解出的坐标越界。两种都不构成「我知道这是哪颗星球」，
    而这一层唯一的产物就是那个知识。
    """

    rank: int | None
    name: str
    score: float | None
    coordinate: Coordinate | None


def coordinate_of(name: str) -> Coordinate | None:
    """从 bot 名字反解坐标；不是 bot、或反解出来越界，一律 None。

    ⚠️ **这道区间校验是硬闸。** 榜单里名字是坐标的**唯一**来源（没有独立的坐标列），
    而实测名字 OCR 错误率约 8%，形态是数字重复：

        2:121:7   名字读作 bot_2_1121_7    ← 多了个 1，1121 > 499
        2:123:12  名字读作 bot_2_123_122   ← 多了个 2，122 > 20

    两个都被区间挡住。挡不住的是「合法但错」——`bot_2_121_7` 读成 `bot_2_127_7`
    完全合法。所以榜单发现的新坐标在库里要标成「未验证」，别直接当成事实
    （用户 2026-08-14 允许直接攻击、打不动就记下来，那是另一条口径，
    但**记录仍要分得清这个坐标是扫出来的还是榜上抄来的**）。
    """
    match = _BOT_NAME_RE.search(name or "")
    if match is None:
        return None
    galaxy, system, position = (int(part) for part in match.groups())
    if not 1 <= galaxy <= TOTAL_GALAXIES:
        return None
    if not 1 <= system <= SYSTEMS_PER_GALAXY:
        return None
    if not 1 <= position <= POSITIONS_PER_SYSTEM:
        return None
    return Coordinate(galaxy, system, position)


def repair_ranks(ranks: Sequence[int | None]) -> list[int | None]:
    """按「逐行 +1」把读错的名次修回来。修不回来的留 None。

    榜单的名次是**严格连续**的，所以任何一行的真值都能从它的邻居推出来——
    这是这一屏「读对没读对」的免费校验和，不花一次额外的 OCR。

    实机 2026-08-14 那一屏（14 行）当场抓出两处：

        [34] [375] [638] [639] [640] [641] [642] [5] [644] …
              ↑ 应是 637                          ↑ 应是 643

    做法是取**「名次 − 下标」这个偏移量的众数**：连续序列上每一行的偏移量都一样，
    所以认同人数最多的那个偏移就是真相，跟它对不上的行按它重算。

    ⚠️ **至少要两行认同才算数**（`_MIN_ANCHORS`）。只有一行认同时，那一行既可能是
    唯一读对的，也可能是唯一读错的——两种情形在数据上一模一样，而按错的那个重算
    会把整屏都改成错的。这时什么都不改，原样交出去。

    ⚠️ 不用「与相邻行差 1」当锚：读不出来的行（None）会把两个正确的行隔开，
    它们的差变成 2，于是一屏里有一个空位就找不到锚了——实测 `[700, None, 702]`
    正是这个形状。偏移量众数对空位免疫。

    ⚠️ 只修**名次**。名字读错没有任何校验和能兜（见 `coordinate_of`），
    军力值读错则无所谓（用户口径）。别把这套推理套到那两列上。
    """
    known = list(ranks)
    offsets = Counter(value - index for index, value in enumerate(known) if value is not None)
    if not offsets:
        return known
    offset, agreed = offsets.most_common(1)[0]
    if agreed < _MIN_ANCHORS:
        return known
    return [offset + index for index in range(len(known))]


def descending_breaks(scores: Sequence[float | None]) -> list[int]:
    """军力值里破坏降序的那几行的下标。**只报，不改。**

    榜单按军力值降序排，所以「比上一行大」一定是读错了。但这条只挡得住大错：
    实测 `28.67K` 读成 `28.57K`、`27.3K` 读成 `27.45K`，两处都仍然保持降序，
    **它一个都抓不住**。

    所以它的用途是「这一屏是不是整个读偏了」，不是「每个数都对」。
    按用户口径（2026-08-14）军力值本来就不需要精确，读不出可以在上下之间插值。
    """
    breaks: list[int] = []
    last: float | None = None
    for index, score in enumerate(scores):
        if score is None:
            continue
        if last is not None and score > last:
            breaks.append(index)
        else:
            last = score
    return breaks


def interpolate_scores(scores: Sequence[float | None]) -> list[float | None]:
    """读不出来的军力值，取上下两个已知值的中点。两侧都没有就留 None。

    用户口径（2026-08-14）：「例如第 650-660 名，你只读出了 650 名和 660 名的军力，
    中间你可以直接插值」。

    **取中点而不是随机**：随机让同一张图跑两遍得出两个结果，事后对不上账。
    而**插出来的值必须在别处标成「估算」**——这个仓有一条硬规矩：猜出来的数
    不许长得像量出来的（`None` 不是 `0` 那条）。这个函数只负责算，标记是调用方的事。
    """
    out = list(scores)
    for index, value in enumerate(out):
        if value is not None:
            continue
        before = _nearest(out, index, step=-1)
        after = _nearest(out, index, step=1)
        if before is not None and after is not None:
            out[index] = (before + after) / 2
    return out


def _nearest(scores: Sequence[float | None], index: int, *, step: int) -> float | None:
    cursor = index + step
    while 0 <= cursor < len(scores):
        value = scores[cursor]
        if value is not None:
            return value
        cursor += step
    return None


def bot_rows(rows: Iterable[RankingRow]) -> list[RankingRow]:
    """只留下反解出坐标的那些行。

    榜单前面约 638 名是真人，中间还夹着 `[638] GoudanLi --- 0` 这种 0 分的真人。
    判据不是名次也不是分数，而是**名字反解得出坐标**——名次会随玩家增减往后挪，
    而写死「639 之后是 bot」在下一次刷新之后就是错的。
    """
    return [row for row in rows if row.coordinate is not None]


__all__ = [
    "POSITIONS_PER_SYSTEM",
    "RankingRow",
    "bot_rows",
    "coordinate_of",
    "descending_breaks",
    "interpolate_scores",
    "repair_ranks",
]
