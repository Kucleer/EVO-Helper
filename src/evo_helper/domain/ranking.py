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
from datetime import datetime
from typing import TypeGuard

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scan_bounds import PIRATE_POSITIONS, SYSTEMS_PER_GALAXY, TOTAL_GALAXIES

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
    #: **这一行是什么时候读到的**（aware UTC）。用户口径（2026-08-16）：
    #: 「军力榜我需要的是每条数据的更新时间」。
    #:
    #: ⚠️ **不是「什么时候入的库」。** 一趟读榜要滚几十屏、跑一个多小时，
    #: 榜首那一屏和末尾那一屏之间差得远。整趟共用一个快照时刻等于把这个差值
    #: 抹掉，而它恰恰是「这个军力值还新不新」唯一的判据。
    #:
    #: 留成可空且带默认值：OCR 那一层（`rows_from_image`）只负责认字，
    #: 读取时刻由调用方在自己的循环里给——它才知道这一屏是什么时候截的。
    #: 落库时为空则回落到快照时刻，见 `storage.military_rankings.append_snapshot`。
    observed_at_utc: datetime | None = None


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


def is_bot_coordinate(coordinate: Coordinate | None) -> TypeGuard[Coordinate]:
    """军力榜反解出的坐标是否可能是 bot。

    每个恒星系的 1--4 号位是游戏固定生成的海盗，不是可由军力榜驱动的
    bot 攻击目标。名字即使形如 ``bot_2_137_1``，也不能成为 bot 候选或派舰队依据。

    ⚠️ 这里曾写着「只保留为榜单原始记录」。用户口径（2026-08-16）推翻了它：
    海盗行**连榜单快照都不进**，见 `storage.military_rankings.append_snapshot`。
    """
    return coordinate is not None and coordinate.position not in PIRATE_POSITIONS


def is_bot_entry(coordinate: Coordinate | None, score: float | None) -> bool:
    """这一行到底算不算 bot：**名字反解出坐标 + 军力读数不是 0**，两条缺一不可。

    用户口径（2026-08-22）：「判断是否 bot 需要增加军力作匹配：id 符合 + 军力不等于 0」。
    光看名字不够——`bot_` 前缀是玩家可以改名伪装的（`AGENTS.md` 4.8），而伪装的真人
    在军事榜上军力常年是 0。所以加一条「军力不等于 0」当第二道判据。

    ⚠️ **`score is None`（军力读不出）照旧算 bot。** 用户口径（2026-08-22）只排除
    **明确的 0**。军力值本来就允许读不出（用户口径 2026-08-14：「基于榜单排序，
    我不需要精确值……甚至有排名 我接受读不出值」），拿「读不出」当排除依据会把
    大批真 bot 一起丢掉，而丢掉的后果是那些坐标从此不再派兵、页面上看不出异常。
    所以下面写的是 `score != 0`（`None != 0` 为真）而**不是**
    `score is not None and score != 0`——这不是漏了判空，是判据本身。

    ⚠️ **必须传 OCR 的原始读数，不能传插值后的值。** `tools.ranking_scan` 那条
    流水线是「读分数 → `descending_breaks` 把破坏降序的那些行的分数丢成 None →
    `interpolate_scores` 用上下邻居补一个中点」。**插值补出来的值必然非零**
    （中点落在两个非零邻居之间），所以任何一个 0 分行只要在中途丢过一次分数，
    出来就是个非零数——**那正好把这条判据要抓的信号擦掉**，而擦掉之后它看起来
    只是「一个普通的低分 bot」。丢分数的路不止一条：0 被读成空（`None`）、
    或者被读成个大数（实测有丢小数点读成 1773K 的）而撞上降序判据，两条都会
    走到插值那一步。

    这条判据**不往 `mentions_bot` 里加**：那个是检测段「到 bot 区了没有」的廉价
    早期信号，只把名字列整条 OCR 一次，**那里根本拿不到军力值**。判早了只是多读
    几屏，所以它宁可宽。`is_bot_coordinate` 也保持原样——它答的是「这个坐标能不能
    当 bot 目标」（海盗位那道闸），是这条判据的一半，被这里复用。
    """
    return is_bot_coordinate(coordinate) and score != 0


def mentions_bot(text: str) -> bool:
    """这段文字里有没有出现**一个 bot 名字的形状**（`bot_银河_恒星系_行星`）。

    用途：翻过真人段时，把名字列整条读一次，问「到 bot 区了没有」。
    到了才开始逐格细读三列——真人段一格都不用细读。

    ⚠️ **不能用子串 `bot` 判。** 榜上有真人叫 `goodbot`（实机 2026-08-15 第 7 名），
    还有 `Bot_1_1_1` 这种大小写变体。这里复用 `coordinate_of` 那条正则的形状：
    要求 `bot` 后面跟三组数字，`goodbot` 不匹配。

    ⚠️ 只看**形状**，不校验区间——区间校验是 `coordinate_of` 的事，
    那一步会把 `bot_2_1121_7` 这种挡掉。这里宁可宽一点：判早了只是多读几屏，
    判晚了会一直翻不到头。
    """
    return _BOT_NAME_RE.search(text or "") is not None


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


def rows_skipped(previous_last_rank: int | None, current_first_rank: int | None) -> int | None:
    """上一屏末行名次 vs 本屏首行名次：中间**漏掉了几名**。读不出名次返回 `None`。

    采集段一直没有这道判据——它靠的是「一次拖动推进得比一屏少」这个**隐含前提**，
    而那个前提从来没被校验过。2026-08-23 实机两趟各十屏实测：

        推进 7–10 行，可见 11–14 行，余量 3–6 行；两趟合计漏掉 **0** 名。

    ⚠️ **别拿「推进多少」当方差依据。** 那个数来自 `progress_mark`（名次中位数），
    它自己带噪声：第一趟里出现过 `+4` 紧跟 `+12`，而两者之和正好是 2×8——
    是中间那一屏的名次被读串了，不是列表真的先慢后快。

    ⚠️ **漏掉的名次不会在任何别的地方露头。** 采集段只按坐标去重，跳过去的那几名
    压根没被读过，所以「采到的 bot 数」看起来完全正常——和刚修掉的那个整屏漏采
    是同一类静默失败。所以这个数必须**记进日志**（`is_bot_entry` 那种事后判据
    救不了没读过的行）。

    ⚠️ **`None` 不是 0。** 名次读不出（榜首三名是奖章图标、或者 OCR 没认出来）
    时答案是「不知道」，不是「没漏」。判据要能区分这两件事，否则读不出名次的那几屏
    会伪装成「重叠完好」。

    ⚠️ 名次**只是校验和**（`repair_ranks` 能从邻居补），所以这个数是**观测**，
    不是闸门：单次 OCR 认错一个数字就中断整趟采集，代价比漏几名大得多。
    要不要据此停下来，等推进量真的提上去（`docs/军力榜采集提速-方案.md` 步 2）
    再定——那时候重叠余量只剩 2 行，判据才该从「记下来」变成「当场停」。
    """
    if previous_last_rank is None or current_first_rank is None:
        return None
    return max(0, current_first_rank - previous_last_rank - 1)


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

    ⚠️ 用户口径（2026-08-22）给「是不是 bot」加了第二条「军力不等于 0」，
    它住在 `is_bot_entry`，**没有加到这里**：那条判据要的是 OCR 的**原始读数**，
    而这个函数只拿到一批已经成型的 `RankingRow`——分数可能在路上被降序判据丢过、
    再被插值补成非零（见 `is_bot_entry` 的注释）。要用新判据就在读到原始分数的
    那一层用。
    """
    return [row for row in rows if is_bot_coordinate(row.coordinate)]


# -- 盲滚距离的自动标定 --------------------------------------------------------
#
# ⚠️ **单位是「行」，不是「屏」**（2026-08-22 改口径，见
# `docs/superpowers/specs/2026-08-22-ranking-blind-scroll-wheel-design.md` 第二节）。
# 「屏」只是慢拖的副产品（1 屏 ≈ 8.3 行），而盲滚段改用滚轮之后连「屏」这个概念都
# 没有了；名次天然就是行，所以标定、余量、日志正文一律用行，屏退化成显示单位。
#
# 屏那一套（`bot_area_reached_message` / `bot_area_scrolls` /
# `calibrated_blind_scrolls`）**留着但已被取代**：库里存着一整年屏版样本，
# 读得出来才谈得上过渡。新代码一律用下面的行版。
#
# 下面这段推理是屏版时期写的，**换成行之后每一条都仍然成立**（只是数乘了 8.3），
# 所以原样搬过来：
#
# 每一趟采集都会量出「翻了 N 行到达 bot 区」，而盲滚距离长期是写死的 40 屏——
# 也就是说系统每天量出八次答案，一次都没反馈回去。生产实测（2026-08-17 同一天
# 六趟）：77 / 78 / 73 / 74 / 72 / 78，而盲拖 40，中间 32–38 屏全在逐屏 OCR
# 检测**必定还是真人**的那一段。按每天 8 趟算，一天白花 250–300 次检测。
#
# ⚠️ **反馈回去的必须是最近 K 次的最小值，不是最近一次，更不是平均值。**
# 上面六次里 78 与 72 差 6 屏：拿 78 去设盲拖就会拖过 bot 起点 6 屏，而拖过头的
# 后果见 `game.ranking_ui` 里那条——该采的那一段被整段跳过去，**采回来的数
# 静悄悄少一截，页面上看不出来**。玩家只增不减所以这个数长期只涨，但噪声让它
# 短期内看起来会跌，取最小值正好吃住这一点。


#: 那句话的固定前缀。查历史样本时在 SQL 里按它做前缀匹配，**必须和
#: `bot_area_reached_message` 拼出来的开头一模一样**，所以由它拼、不各写一遍。
#:
#: ⚠️ **行版与屏版共用这一个前缀**，是有意的：查库时一次前缀匹配就把两套正文
#: 都捞回来，再由各自的解析器挑走自己那一套（另一套会被解析成 `None` 丢掉）。
#: 代价是查询的 `limit` 得留够——过渡期里捞回来的一批可能大半是屏版历史。
BOT_AREA_REACHED_PREFIX = "翻了 "

_BOT_AREA_REACHED_SUFFIX = " 屏到达 bot 区"

_BOT_AREA_REACHED_ROWS_SUFFIX = " 行到达 bot 区"


def bot_area_reached_message(scrolls: int) -> str:
    """「翻了 N **屏**到达 bot 区」这句话的**唯一出处**。

    ⚠️ **已被 `bot_area_reached_rows_message` 取代（口径改行，2026-08-22），
    等调用点切完再删。** 新代码要发的是行版正文——这个函数只为存量调用点和
    库里那一年屏版样本留着。别拿它去发盲滚的实测：滚轮没有「屏」这个概念，
    发出来的数会被行版解析器整条丢掉，而丢掉是静默的。

    ⚠️ 它同时是一句给人看的日志和一条给机器读回来的实测记录
    （`bot_area_scrolls` 从 `system_log` 里反解它）。所以措辞不许随手改：
    改了就等于把库里全部历史样本一次性作废，而作废之后自动标定会静悄悄退回
    写死的默认值——页面上、日志里都看不出任何异常。
    """
    return f"{BOT_AREA_REACHED_PREFIX}{scrolls}{_BOT_AREA_REACHED_SUFFIX}"


def bot_area_reached_rows_message(rows: int) -> str:
    """「翻了 N **行**到达 bot 区」这句话的**唯一出处**。

    ⚠️ 它同时是一句给人看的日志和一条给机器读回来的实测记录
    （`bot_area_rows` 从 `system_log` 里反解它）。所以措辞不许随手改：
    改了就等于把已经攒下的样本一次性作废，而作废之后自动标定会静悄悄退回
    写死的默认值——页面上、日志里都看不出任何异常。

    ⚠️ **单位那个字（「行」）是唯一区分它和屏版正文的东西。** 两套正文前缀一样、
    形状一样，只差这一个字，而解析是按整句锚定的——把它写成「屏」就等于把这条
    实测发到屏版那一套里去，两边都得不到样本。
    """
    return f"{BOT_AREA_REACHED_PREFIX}{rows}{_BOT_AREA_REACHED_ROWS_SUFFIX}"


#: 反解屏版那句话。锚在两端，免得把「…之后翻了 3 屏到达 bot 区」这种复述也当成样本。
_BOT_AREA_REACHED = re.compile(
    f"^{re.escape(BOT_AREA_REACHED_PREFIX)}(\\d+){re.escape(_BOT_AREA_REACHED_SUFFIX)}$"
)

#: 反解行版那句话。
#:
#: ⚠️ **和屏版是两条独立的正则，不许合成一条「屏|行」。** 库里存着一整年
#: 「翻了 N 屏到达 bot 区」，合起来匹配就会把「78 屏」当成 78 行喂给自标定——
#: 那是把 8.3 倍的量纲错读成 1 倍，而算出来的盲滚只是「小得离谱」，不会报错。
_BOT_AREA_REACHED_ROWS = re.compile(
    f"^{re.escape(BOT_AREA_REACHED_PREFIX)}(\\d+){re.escape(_BOT_AREA_REACHED_ROWS_SUFFIX)}$"
)


def bot_area_scrolls(message: str) -> int | None:
    """一条日志正文里的实测**屏**数；不是那句话就返回 None。

    ⚠️ **已被 `bot_area_rows` 取代（口径改行，2026-08-22），等调用点切完再删。**
    留着是因为库里那一年屏版样本还得读得出来，不是给新代码用的。
    """
    match = _BOT_AREA_REACHED.match(message.strip())
    return int(match.group(1)) if match is not None else None


def bot_area_rows(message: str) -> int | None:
    """一条日志正文里的实测**行**数；不是那句话就返回 None。

    ⚠️ **屏版正文在这里一律返回 None**，这是有意的、也是被一条用例钉死的：
    库里存着一整年「翻了 N 屏到达 bot 区」，而 78 屏 ≈ 647 行——当成 78 行的话
    自标定会给出一个荒谬的小值。小值本身安全（只是白花检测段那 4.6 秒/屏），
    但它是撞上的而不是算出来的，换个方向的噪声就未必安全了。屏版样本要读，
    走 `bot_area_scrolls`，且**不参与行版标定**。
    """
    match = _BOT_AREA_REACHED_ROWS.match(message.strip())
    return int(match.group(1)) if match is not None else None


def calibrated_blind_scrolls(
    measurements: Sequence[int], *, sample_size: int, margin: int
) -> int | None:
    """按最近 `sample_size` 次实测定盲拖屏数：`min(样本) - margin`。

    ⚠️ **已被 `calibrated_blind_rows` 取代（口径改行，2026-08-22），
    等调用点切完再删。** 判据本身一条没变，只是单位从屏换成行。

    `measurements` 按**新到旧**排列，多给的会被截掉——只看最近那一段是因为这个
    数随玩家增长往上漂，陈年样本只会把盲拖压得越来越保守（安全但白花检测）。

    **样本不够就返回 `None`**，意思是「这次不给答案，用写死的默认值」。返回
    `None` 而不是自己回落成某个数字：默认值只有 `game.ranking_ui.BLIND_SCROLLS`
    一处，在这里再写一遍，日后调默认值就会漏掉这一处。

    `margin` 是余量，不是保险丝上的裕度而是**判据的一部分**：实测噪声跨度
    6 屏（72–78），余量必须大于它，否则算出来的盲拖会落进噪声区间里。

    结果钳到 0：样本比余量还小（榜单极短）时，答案是「一屏都别盲拖」，
    而不是一个负数。
    """
    if sample_size < 1:
        raise ValueError("sample_size 必须至少为 1")
    recent = list(measurements)[:sample_size]
    if len(recent) < sample_size:
        return None
    return max(0, min(recent) - margin)


def calibrated_blind_rows(
    measurements: Sequence[int], *, sample_size: int, margin: int
) -> int | None:
    """按最近 `sample_size` 次实测定盲滚**行数**：`min(样本) - margin`。

    与 `calibrated_blind_scrolls` 同形——下面每条理由都是从那边搬过来的，
    换成行之后一条都没失效（只是数乘了约 8.3）。

    `measurements` 按**新到旧**排列，多给的会被截掉。**只看最近那一段**是因为
    这个数随玩家增长往上漂，陈年样本只会把盲滚压得越来越保守：安全，但少走的
    距离由检测段接手，而检测段约 4.6 秒/屏。

    ⚠️ **反馈回去的必须是最近 K 次的最小值**，不是最近一次，更不是平均值。
    实测同一天六趟的跨度就有 6 屏（约 50 行）：拿偏大的那次去设盲滚就会滚过
    bot 起点，而滚过头的后果是**采回来的数静悄悄少一截，页面上看不出来**。

    **样本不够就返回 `None`**，意思是「这次不给答案，用写死的默认值」。返回
    `None` 而不是在这里自己回落成某个数字：默认值只有
    `game.ranking_ui.BLIND_SCROLL_ROWS` 一处，在这里再写一遍，日后调默认值
    就会漏掉这一处——而漏掉之后两个默认值会各自生效，谁也不知道用的是哪个。

    `margin` 由调用方传进来（取值住在 `game.ranking_ui`），它不是保险丝上的
    裕度而是**判据的一部分**：余量必须大于实测噪声的跨度，否则算出来的盲滚
    会落进噪声区间里，也就是「某些趟必然滚过头」。

    结果钳到 0：样本比余量还小（榜单极短）时，答案是「一行都别盲滚」，
    而不是一个负数。
    """
    if sample_size < 1:
        raise ValueError("sample_size 必须至少为 1")
    recent = list(measurements)[:sample_size]
    if len(recent) < sample_size:
        return None
    return max(0, min(recent) - margin)


__all__ = [
    "BOT_AREA_REACHED_PREFIX",
    "POSITIONS_PER_SYSTEM",
    "RankingRow",
    "bot_area_reached_message",
    "bot_area_reached_rows_message",
    "bot_area_rows",
    "bot_area_scrolls",
    "bot_rows",
    "calibrated_blind_rows",
    "calibrated_blind_scrolls",
    "coordinate_of",
    "descending_breaks",
    "rows_skipped",
    "interpolate_scores",
    "is_bot_coordinate",
    "is_bot_entry",
    "mentions_bot",
    "repair_ranks",
]
