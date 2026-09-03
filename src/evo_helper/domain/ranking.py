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
import statistics
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


def screens_overlap(previous: Iterable[Coordinate], current: Iterable[Coordinate]) -> bool | None:
    """相邻两屏**有没有共同坐标**。任一屏一个坐标都没读出来时答 `None`（不知道）。

    问的是「这一次拖动有没有推过一整屏、把中间几行整段跳掉」。一次拖动推进约 8 行
    而一屏可见 11–14 行（2026-08-23 生产实测 70 屏：名次从 771 走到 1343，
    平均 8.2 行/屏），所以正常情况下相邻两屏**共享 3–6 行**。共享行一个都没有，
    才说明中间可能整段没被读过。

    ## ⚠️⚠️ 为什么判据在**坐标**上，而不在名次上

    原先这里是 `rows_skipped(上屏末行名次, 本屏首行名次)`，报「漏掉 N 名」。
    它在生产上整趟喊了 12 次狼来了，**一次都不是真的**（run `91c7f9ec`，
    2026-08-23 20:35，整趟只从第 771 名走到第 1343 名、共约 570 名）：

        807 / 596 / 42 / 996 / 86 / 996 / 996 / 1166 / 996 / 248 / 996 / 997
        累计报「漏掉 8922 名」——比整趟走过的名次多出 15 倍

    那 5 个几乎一样的 996 就是根因的指纹：**名次列的 OCR 会串出高位噪声**
    （`tools.ranking_scan.progress_mark` 记着实机读到过 `[401]`、`[4781]`、
    `[1411]`），而少读/多读一个千位就凭空造出约 1000 的差。同一趟落库的
    `military_rank` 也带着同一个噪声：500 条里 13 条名次落在 5–13 段，
    而其中一条军力只有 8,710——榜首那几名是百万级，它真实名次在 1000 名以后。

    `996 = 1000 − 4` 正是「千位丢了 + 真实重叠 4 行」，`997 = 1000 − 3` 同理。

    ⚠️ **加个合理性上界救不了它。** 一次拖动最多推一屏，所以真实的漏采只有
    1–3 行的量级；而名次噪声的个位/十位串读（`853` 读成 `858`）落的正是同一个
    1–30 的区间。上界只挡得住那些**一眼就知道是错的**大数，挡不住的那一段恰好
    就是判据唯一有话可说的量级——真阳性和假阳性在那里分不开。

    ⚠️ **用户口径（2026-08-23）：「名次字段可以忽略，我们只需要使用军力进行判断」。**
    名次不可信，那么建在名次上的判据就没有可信输入。而坐标是这张榜上唯一
    load-bearing 的一列（模块开头那张表），它还有 `coordinate_of` 那道区间硬闸——
    读错的名字大多解不出合法坐标，于是只是不参与比较，而不是造出一个假数。

    ## ⚠️ 只答「有没有」，不答「几名」

    跳过去的行**压根没被读过**，所以「漏了几名」这个数在原理上就无从得知
    （原先那个数是从两个带噪声的名次减出来的，那才是它敢报 8922 的原因）。
    这里只交一个布尔量，整趟只累计**几屏**没重叠上。

    ⚠️ **`None` 不是 `False`。** 一屏坐标全读不出（离页、面板没铺开、名字列整列
    没认出来）时答案是「不知道」，不是「重叠断了」。反过来答 `False` 会让最可疑的
    那几屏（连名字都读不出的）额外挨一次假警报。

    ⚠️ **假阳性还剩一种，别把它当铁证。** 名字 OCR 错误率约 8%，共享 4 行时
    「四行全没对上」约 0.05%（每趟 70 屏≈3.5% 的趟会多出一行），而一屏只读出 9 行
    （实测最少）时共享行更少、这个概率更高。所以话要说成「重叠**可能**断了」，
    而且这是**观测不是闸门**——等推进量真提上去（`docs/军力榜采集提速-方案.md`
    步 2，那时重叠只剩 2 行）再谈要不要当场停。
    """
    before = {coordinate for coordinate in previous}
    now = {coordinate for coordinate in current}
    if not before or not now:
        return None
    return not before.isdisjoint(now)


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


#: 相邻两行的军力值最多允许跌掉几倍。超过就是读错了，不是榜单真的这么陡。
#:
#: 榜单按军力降序，而**相邻名次的差极小**：语料 15 屏实测跨 12 行只从 10,610
#: 跌到 10,470（1.3%），整段 570 名也只从 10.6K 跌到 9.5K（约 10%）。
#:
#: 而**丢首位**那类读错正好是 10 倍：2026-08-23 生产实测有整三屏把 `11.75K` /
#: `11.41K` / `11.11K` 读成 `1.75K` / `1.41K` / `1.11K`——屏内 1,750 → 1,600
#: 自成完好的降序，`descending_breaks` 一个都没抓到，而上一屏末尾是 13,200、
#: 下一屏开头是 10,980。
#:
#: 取 5 是留在两者之间：真实跌幅到不了 5 倍，丢首位必然是 10 倍。
#:
#: ⚠️ **不要往 2 附近调。** 真人段与 bot 段交界处可能有真实的断崖，
#: 而误判的代价是把一批好读数丢成估算值。
SCORE_CLIFF_FACTOR = 5.0

#: 一屏首尾之间**最多**差几倍，超过就认为首尾自己读错了、这一屏的区间不作数。
#:
#: 取 2 的依据（2026-09-02 生产实测，近 3 天 1,099 组被丢行）：真实的一屏跨度
#: **中位 0.31%、P90 4%**，与代码里早就量过的「跨 12 行只差 1.3%」一致；而端点被
#: 数量级读错时跨度会直接跳到 99%。2 倍留在两者中间，宽到真实跌幅永远够不着，
#: 窄到一个丢首位的端点（10 倍）必然出界。
#:
#: ⚠️ **它只用来判「区间作不作数」，不用来判单个读数。** 单个读数的数量级仍由
#: `SCORE_CLIFF_FACTOR` 那两条管——两套阈值各管一件事，别合并。
SCREEN_SPREAD_LIMIT = 2.0

#: 这一版判据的**版本指纹**。改了判据就必须改它。
#:
#: ⚠️ 用途是**只靠库里的日志判断生产跑没跑上这一版**。仓库里有过教训：#266 那道
#: 支线落地时一行新日志都没留，用户问「生产跑的是哪个版本」时只能答「看不出」；
#: 而 #260 与 #262 两版的 `criterion` 一字不差，最准的那条指纹分不出它们。
SCORE_RULE_VERSION = "curve/3"

#: 一个读数偏离曲线多远就算读错。**这个数是量出来的，不是挑的。**
#:
#: 2026-09-03 留一验证（生产数据，5 趟扫描 458 个判定点，参照 = 下面那个局部拟合）：
#:
#:     偏差 0.0–0.5%   88.7%   ← 真值紧贴曲线
#:     偏差 0.5–2.0%    5.1%
#:     偏差 2.0–5.0%    1.9%
#:     偏差 5.0%+       4.3%   ← 误读
#:
#:     中位 0.112%   P90 1.97%   P95 7.80%
#:
#: 3% 之上标出约 5–9% 的行（不同轮次质量差别很大），其中 **≥80% 是确凿误读** ——
#: 「把某一位换成别的数字就落回估计值」，可验证，不是判断。
#:
#: ## ⚠️ 为什么从 35% 收到 3%：成本的天平翻过来了
#:
#: `curve/1` 那一版容差 35%，理由是「15–30% 那 6 个点是真值，收紧会误伤」。
#: **那个结论建立在粗糙的参照上**（±60 名窗口中位数，自身中位误差 0.84%、P95 26%）。
#: 换成局部拟合之后参照精度好了一个数量级（中位 0.112%），于是：
#:
#:     误杀一个好值   该行被换成估算值，而估算中位误差 0.1%   ← 几乎为零
#:     漏过一个误读   库里躺着偏 14–24% 的军力值，直接进选靶排序
#:
#: 生产实测证据：`curve/1` 放行过 5,970（真值 6,970）、6,330（真值 8,330）、
#: 15,280（真值 19,280）——偏离 14–24%，全在 35% 之下。
CURVE_TOLERANCE = 0.03

#: 局部拟合取几个点：目标名次**上下各一半**。
#:
#: 取 4 的依据（2026-09-03 留一验证，857 个点）：
#:
#:     两点插值            中位 0.060%  P90 0.63%  P99 15.49%
#:     最近 4 点（本选）    中位 0.075%  P90 0.57%  P99  8.30%
#:     最近 6 点           中位 0.085%  P90 0.53%  P99 16.62%
#:     最近 12 点          中位 0.107%  P90 0.59%  P99 16.52%
#:     ±60 名窗口中位数     中位 0.843%  P90 4.18%  P99 17.12%
#:
#: 4 个点在中位数上几乎追平两点插值，而 **P99 从 15.5% 砍到 8.3%** ——多两个点正好
#: 兜住「撞上一个读错的端点」。再多就开始被曲率拖累：榜单不是直线，窗口一宽，
#: 直线拟合本身就偏了（12 点那一档中位误差反而涨回 0.107%）。
CURVE_FIT_POINTS = 4

#: 那几个点的名次跨度上限。超过就不表态。
#:
#: ⚠️ **这一条不是可选的。** 没有它，名次稀疏处会拿隔着几百名的两个点连一条直线：
#: 实测有名次 273 与 821 被当成邻居（军力 10,000 vs 23,780），估出来的东西毫无意义。
#: 而「直线」这个假设只在小跨度上成立——同一屏跨 12 行只差 1.3%。
CURVE_RANK_SPAN = 60

#: 拟合窗口**内部**允许的最大名次空洞。超过就不表态。
#:
#: ⚠️ **这不是 `CURVE_RANK_SPAN` 的重复。** 跨度管的是窗口多宽，这一条管的是窗口多密：
#: 四个点里三个挤在一块、第四个隔着一个大洞，跨度看着很小，而斜率中位数会被那个
#: 紧密小簇**重重地带跑**（三个点两两组合就是 3 对，占了 6 对中的一半）。
#:
#: 2026-09-03 拿当时榜上 638 个实测点做前向留一验证，按窗口内最大空洞分组：
#:
#:     空洞 ≤ 2   470 个点   中位 0.20%   超 3% 的  7.0%
#:     空洞 3–4   111 个点   中位 0.23%   超 3% 的  9.9%
#:     空洞 5–8    21 个点   中位 1.92%   超 3% 的 47.6%     ← 塔了
#:     空洞 >16     9 个点   中位 9.82%   超 3% 的 66.7%
#:
#: （前两档那七八个百分点大半不是估算误差，是库里本来就漏过去的错读——它们连 10%
#: 都超了。）取 4：它只拦住 5.6% 的情形，而那 5.6% 里将近一半的参照是错的。
#:
#: ⚠⚠ **这一条是被一条现有用例逗出来的**，不是想出来的：
#: `test_the_anchor_is_carried_from_one_screen_to_the_next` 里连两屏被丢光，历史于是
#: 剩下「三个旧点 + 一个新点」，曲线据此去否决了两行正确读数。那正是级联误伤
#: 换了个机制又回来了，而不是用例该改。
CURVE_MAX_GAP = 4


def curve_reference(
    history: Sequence[tuple[int, float]],
    rank: int,
    *,
    points: int = CURVE_FIT_POINTS,
    span: int = CURVE_RANK_SPAN,
    max_gap: int = CURVE_MAX_GAP,
    require_both_sides: bool = True,
) -> float | None:
    """这一名次**该是**多少军力：拿最近的几个已知点做局部稳健拟合。交不出就 `None`。

    用户口径（2026-09-02 / 09-03，逐字）：

        「军力的降序速度是可预估范围内的，实际上仅需要几个锚点就可以对其整个长链路」
        「实际上曲线上，有很多个点可以用来修正曲线，实际上精度应该是非常高的」

    精度确实非常高：留一验证中位误差 **0.112%** ——20,000 军力上偏 20 出头。

    ## 做法：Theil–Sen，不是最小二乘

    取目标名次**上下各 `points // 2` 个**已知点，算两两之间的斜率、取**中位数**，
    再用它反推截距、同样取中位数。

    ⚠️ **斜率取中位数而不是让残差最小**，因为窗口里可能混着误读：最小二乘会被一个
    坏点整体拽偏，而中位数需要一半以上的点都坏才会被带跑。

    ⚠️⚠️ **这一条的证据是分布层面的，不是逐例的**（诚实交代，别当成有用例钉着）：
    857 个点上留一验证，最小二乘中位误差 0.607%、Theil–Sen 0.441%。而**单个例子造
    不出差别** —— 四个点里只坏一个时，下面那一步的截距中位数会把偏掉的斜率兜回来，
    两种算法给出同样的答案（2026-09-03 变异验证证实：把中位数换成平均，全部用例
    照绿）。所以改这一行不会有用例变红，**但它会让整体精度退回 0.6%**。

    ## ⚠⚠ `require_both_sides` 分开两个调用处境，不是一个可选项

    两侧各有点时目标夹在中间，插值而不外推，精度最好（留一验证中位 0.08%）。
    可是**能不能凑齐两侧，取决于谁在问**：

    - **边扫边判那一段**（`trusted_scores`）是严格的前向单程：一屏判完才进历史，
      所以历史里**只有名次更小的点**，「上方」永远凑不齐。而生产实测每滚推进
      15.6 个名次、读出 12 行——**屏与屏之间没有重叠**，一点都借不到。
    - **整趟收尾补数那一步**（`tools.ranking_scan._backfill_from_the_curve`）历史是全的，
      两侧要求在那里正好拦住「往扫描末端外推」——那几行下方本来就没点。

    → 所以**判定传 `False`，补数维持 `True`**。

    ⚠⚠ **这一条是拿生产事故换来的。** #274 把两侧要求当成无条件的，于是曲线判据
    在生产上**一次都没生效过**，而 CI 全绿、日志还报「89.5% 有曲线参照」（那个数是
    事后补算的，拿到了判据当时没有的历史）。2026-09-03 拿生产一整趟的真实名次
    前向重放，覆盖率：

        两侧各 2 点        判 750 行，拿到参照   0 =  0.0%
        最近 4 点不限侧    判 750 行，拿到参照 717 = 95.6%

    单侧确实是外推，但 `span` 把它的跃度封住了，而量出来的代价很小（同一趟 573 个
    实测点，只用名次更小的历史做留一验证）：

        单侧最近 4 点    中位 0.12%   P90 0.67%
        两侧各 2 点      中位 0.08%   P90 0.44%

    两者差不到 0.05 个百分点，而 `CURVE_TOLERANCE` 是 3% —— 差这么远的两个量级里，
    「拿不拿得到参照」才是要害，精度那一点差别无关痛涒。

    —— 而不管哪一条路，拟合窗口的**跨度始终 ≤ `span`**：直线假设只在小跨度上
    成立（实测同一屏跨 12 行只差 1.3%）。

    ## 与 `curve/1` 那一版（±60 名窗口中位数）的分别

    那一版参照的是「这一段大概什么水平」，在斜坡上会给整个窗口同一个值，靠近两端
    系统性偏低/偏高（中位误差 0.843%）。这一版参照的是「这个名次上该是多少」，
    **跟着斜率走**。差别在斜坡上尤其大：相邻已知点跨 ≥50 名时，窗口中位数误差中位
    1.76%、最大 19.6%，而顺着斜率插值是 0.03% / 0.2%。

    交不出参照就交 `None`（点不够、或跨度超限），调用方退回原来那几条判据 ——
    **放宽只在证据齐全时发生**。
    """
    if require_both_sides:
        half = max(points // 2, 1)
        below = sorted((p for p in history if p[0] < rank and p[1] > 0), key=lambda p: rank - p[0])
        above = sorted((p for p in history if p[0] > rank and p[1] > 0), key=lambda p: p[0] - rank)
        if len(below) < half or len(above) < half:
            return None
        near = below[:half] + above[:half]
    else:
        # 不限侧时按「离目标名次多远」取最近的几个，两侧有点就自然都用上。
        nearby = [p for p in history if p[1] > 0 and abs(p[0] - rank) <= span]
        near = sorted(nearby, key=lambda p: abs(p[0] - rank))[:points]
        if len(near) < points:
            return None
    known_ranks = sorted(r for r, _ in near)
    if known_ranks[-1] - known_ranks[0] > span:
        return None
    # 窗口里有大洞就不表态——洞那一边的斜率无从得知，而剩下那个紧密小簇
    # 会把斜率中位数带到它自己那一段上去。数据在 `CURVE_MAX_GAP` 上。
    if any(b - a > max_gap for a, b in zip(known_ranks, known_ranks[1:])):
        return None
    slopes = [
        (near[j][1] - near[i][1]) / (near[j][0] - near[i][0])
        for i in range(len(near))
        for j in range(i + 1, len(near))
        if near[j][0] != near[i][0]
    ]
    if not slopes:
        return None
    slope = statistics.median(slopes)
    reference = statistics.median([score - slope * (known - rank) for known, score in near])
    # ⚠⚠ **参照自己必须站得住：恒为正，且与窗口自身水平同一个数量级。**
    #
    # 没这一道闸时，拟合会交出**负数**，而负参照让 `abs(score / reference - 1)`
    # 恒大于任何容差 —— 整屏全丢。而全丢之后一个点也进不了历史，下一屏照旧
    # 拿同一批坏点去拟合、外推得更远、参照更荒——**这是个吸收态**，与
    # `judge_scores` 里「0 永远不许当基准」那一段是同一类缺陷。
    #
    # 2026-09-03 生产实况（#275 上线后第一趟，20:29–20:30 连着 10 屏）：
    #
    #     参照 -357 → -3,110 → -13,750 → -37,862 → -48,203 → -49,304
    #     历史卡在 426 → 461 → 462 点几乎不长，锚点反复变成 None
    #
    # 那一趟 302 个被丢行里 **101 行（33%）出自这 10 屏**，不是真误读。
    #
    # 闸本身不花钱：同一批 493 个健康拟合上，它**一个都没否掉**（留下的误差
    # 中位 0.16% / P90 1.08%），而要跌到 -48,000 得跑到窗口水平的 -530%。
    #
    # 倍数用现成的 `SCORE_CLIFF_FACTOR`：它管的就是「多一位 / 丢一位」，同一件事。
    level = statistics.median([score for _, score in near])
    if (
        reference <= 0
        or reference * SCORE_CLIFF_FACTOR < level
        or reference > level * SCORE_CLIFF_FACTOR
    ):
        return None
    return reference


#: 「这一屏是被哪条判据拦的」——三条的处置完全不同，混成一句「不可信」等于没说。
DROP_OFF_CURVE = "偏离曲线"
DROP_OUT_OF_BRACKET = "出界"
DROP_OUT_OF_ORDER = "破坏降序"
DROP_TOO_BIG = "比基准大一个数量级"
DROP_TOO_SMALL = "比基准小一个数量级"


def screen_bracket(
    scores: Sequence[float | None],
    *,
    spread_limit: float = SCREEN_SPREAD_LIMIT,
) -> tuple[float, float] | None:
    """这一屏首尾读得出、且自身站得住时，交出可信区间 `(下界, 上界)`；否则 `None`。

    用户口径（2026-09-02，逐字）：

        「只要该屏内首尾能读出，并且与上一屏保持递减，我能接受全部的估算值」
        「所有这几行 首尾在的话，不应该被丢弃啊」

    ## ⚠️⚠️ 这是为了止住**屏内**的级联误伤

    `trusted_scores` 原先只有一条降序判据：**比屏内上一个可信值大就丢**。它防得住
    「读大了」，防不住「读小了却仍然递减」——那种错读会**顺利通过判据、当上基准**，
    然后把它后面每一行正确的值都判成逆序：

        真值    9770  9760  9750  9740  9730
        读成    9770  3760  9750  9740  9730      ← 只有第 2 行读错（9 → 3）
        判据    ✓     ✓     ✗     ✗     ✗        ← 基准掉到 3760，后面全陪葬

    2026-09-02 生产实测（近 3 天，被丢 ≥2 行的 1,099 组）：

        整组自身完美降序   867 组   78.9%    ← 被丢的那些行彼此严格递减
        只有 1 处逆序      192 组   17.5%
        2 处以上逆序        40 组    3.7%

        落在「完美降序」组里的行：3,827 / 5,090 = **75.2%**
        那些组的组内跨度：中位 **0.31%**

    **一批彼此严格递减、总跨度 0.3% 的读数不可能是读错了**，它们是被误伤的。
    同一批数据里另外两个指标也指向级联：被丢的值 71.5% 其实 ≤ 锚点（是真值），
    而末行被丢的次数是首行的 **7 倍**（越靠后越容易被前面的错读连累）。

    ## 区间怎么算，以及为什么这四条缺一不可

    榜单降序、且 `targets_from_rows` 只在采集段被调用（那时早已滚进 bot 段，
    屏内值差得极近），所以**首尾两个读数就框住了这一屏的全部合法取值**。

    1. **至少两个正读数**——一个点框不出区间。
    2. **末 ≤ 首**：首尾自己就不递减的话，至少有一个端点是错的，区间不作数。
    3. **首 ≤ 末 × `spread_limit`**：跨度过大说明端点本身被数量级读错了。
       ⚠️ 没有这一条，一个丢首位的末行（真值 9,740 读成 1,740）会把下界拉到
       1,740，于是整屏连同真正的错读一起放行——**比不做这个改动还糟**。

    判不出区间就交 `None`，调用方退回原来那条逐行降序判据——**放宽只在证据齐全时
    发生**，证据不齐时行为逐字不变。

    ## ⚠️⚠️ 这里**故意不看锚点**，别再加回来

    用户口径那句话有两半：「屏内首尾能读出」**并且**「与上一屏保持递减」。第一半就是
    上面三条；第二半**已经由 `trusted_scores` 的 `too_big` / `too_small` 守着**——
    它们拿锚点当基准逐行判，而这个区间只替换 `out_of_order` 那一条，一个字都没碰它们。

    我写第一版时在这里加了第四条「首 ≤ 锚点 × `cliff_factor`」，**变异验证证明它是
    死代码**：把它删掉，59 条用例全绿。构造不出只有它拦得住的情形——整屏偏高时
    `too_big` 会逐行挡掉，区间作不作数结果一样。

    而卡得更严（「首 ≤ 锚点」）是**有害的**，有用例钉着
    （`test_a_slightly_low_anchor_does_not_disable_the_bracket`）：锚点自己可能被上一屏
    误判压低（实测「锚点 25190、被丢的值 29130」），拿一个偏低的锚点去卡本屏首行，
    就是把 `trusted_scores` 里整段讲的**跨屏级联**换个地方再犯一次。
    """
    known = [score for score in scores if score is not None and score > 0]
    if len(known) < 2:
        return None
    head, tail = known[0], known[-1]
    if tail > head:
        return None
    if head > tail * spread_limit:
        return None
    return tail, head


def trusted_scores(
    scores: Sequence[float | None],
    *,
    anchor: float | None = None,
    cliff_factor: float = SCORE_CLIFF_FACTOR,
    ranks: Sequence[int | None] | None = None,
    history: list[tuple[int, float]] | None = None,
) -> list[float | None]:
    """把不可信的军力读数换成 `None`。判据整段在 `judge_scores` 上。

    要知道**每一行为什么**被丢，用 `score_drop_reasons`——它和这里走同一次遍历。
    """
    return judge_scores(
        scores, anchor=anchor, cliff_factor=cliff_factor, ranks=ranks, history=history
    ).trusted


@dataclass(frozen=True)
class Judgement:
    """一屏读数判完之后的全部结果，三份名单一一对应。

    ⚠️ **三份必须出自同一次遍历。** 拆成几个函数各走一遍就是「同一件事几份实现」，
    而判据会往 `history` 里追采信点——第二遍看到的历史比第一遍多，于是它会
    走到**第一遍根本到不了的分支**，交出一套看上去很像真的假话。
    #275 之前日志就是这么坏的（整段在 `references` 上）。
    """

    #: 可信的读数；被丢的那几位是 `None`。
    trusted: list[float | None]
    #: 与 `trusted` 一一对应：可信的那几位是 `None`，被丢的那几位是丢它的理由。
    reasons: list[str | None]
    #: 与 `trusted` 一一对应：判这一行时**真正用上的**曲线参照；`None` = 那一行没参照、
    #: 退回了区间 / 逐行降序 / 断崖那几条。
    #:
    #: ⚠⚠ **日志只允许报这份，不允许事后自己再算一遍。** #274 上线后日志报
    #: 「89.5% 有曲线参照」，而真实覆盖率是 **0%**：那个数是在判完之后拿
    #: `curve_reference(history, 首行名次)` 补算的，而那时 `history` 里已经多了**这一屏
    #: 自己**，于是“上方”凑齐了。同一个坑让理由也全错：一行本来是被「出界」拦的，
    #: 补算时曲线反而采信它，于是日志把它标成了「渲染不出」。
    #:
    #: 而这个数本身就是能在第一天抳住那次回归的信号——它会一直是 0。
    references: list[float | None]


def judge_scores(
    scores: Sequence[float | None],
    *,
    anchor: float | None = None,
    cliff_factor: float = SCORE_CLIFF_FACTOR,
    ranks: Sequence[int | None] | None = None,
    history: list[tuple[int, float]] | None = None,
) -> Judgement:
    """判一屏读数，交出 `Judgement`（可信值、理由、真正用上的曲线参照）。

    ⚠️ **值和理由必须出自同一次遍历。** 拆成两个函数各走一遍就是「同一件事两份
    实现」，两边迟早分家——而分家之后日志会**理直气壮地说错话**，比不记更糟。

    三条拒收理由：

    - `out_of_order`：比**屏内**上一个可信值大 —— 榜单降序，这一定是读错了。
    - `too_big` / `too_small`：比基准差出 `cliff_factor` 倍 —— 多一位或丢一位。

    ⚠️ **锚点只喂给倍数判据，不喂给降序判据**，理由整段在 `last` 那一行上。

    ## ⚠️⚠️ `anchor` 是上一屏最后一个可信值，它是这道判据的要害

    原先只有 `descending_breaks`，而它是**按屏**跑的——于是**每屏的第一行没有
    任何约束**，跨屏的断层也完全看不见。2026-08-23 生产实测两种漏网，都是 10 倍，
    而且方向相反：

        93,670  落在它那一屏的**第一行**，屏内没有前一个可比，直接落库
                （真值约 9,670；小数点被读成了一个数字，语料里见过 `9.87K` → `93.87K`）
        1,750 / 1,412 / 1,112  是**整三屏**偏小 10 倍，屏内自成完好的降序

    两者都是 10 的整数倍，所以 `tools.ranking_scan.renderable_score` 也放过了。
    把上一屏的锚点带进来，两个方向一并挡住。

    ⚠️ **锚点只跟着可信值走。** 一个被判为不可信的读数不许当下一个的基准，
    否则一次读错会把它后面整段拖着一起判错。

    ⚠️ **只判「不可信」，不猜真值。** 丢了首位的那些看起来像 `1.75K`，
    真值是 `11.75K` 还是 `1.75K` 这一层答不了——乘 10 补回去就是在猜，
    而这个仓有硬规矩：猜出来的数不许长得像量出来的。所以交 `None`，
    由 `interpolate_scores` 用上下邻居补中点、并由调用方标成估算。
    """
    # ⚠️ **没有锚点时用这一屏的中位数起头，不能用第一个读数。**
    #
    # 整趟的第一屏没有上一屏可依，而拿第一个读数当基准会让一个偏大的首行
    # 把它后面整屏拖着一起判错：`[93670, 9650, 9640]` 里 93,670 当上基准之后，
    # 9,650 和 9,640 都成了「跌掉 10 倍」——比不加这道判据还差。
    #
    # 中位数抗单点异常（一个坏值动不了它），而它只用来起头：第一个可信值一出现，
    # 基准就交给它。
    #
    # ⚠️ 这条成立的前提是**同一屏内军力值差得很近**（实测跨 12 行只差 1.3%），
    # 而这个前提只在采集段成立——榜首那几屏能从 5.97M 跌到十万级。
    # `targets_from_rows` 只在采集段被调用（那时早已滚进 bot 段），所以够用。
    # ⚠️⚠️ **0 永远不许当基准。**
    #
    # 榜上真有 0 分行（`[638] GoudanLi --- 0`），而 0 当上基准会造成一个**吸收态**：
    # `basis = 0` 之后任何正读数都撞上 `too_big`（`score > 0 * 5`）被丢，
    # 而 0 自己因为 `basis > 0` 那半句不成立反而被采信；于是锚点交出 0，
    # 下一屏所有正值都「大于锚点」被全丢，整屏不可信又让锚点**沿用** 0 ——
    # 此后每一屏全空，而且不报错。症状正是这道判据本该治的那类静默失败。
    #
    # 所以基准只跟着**正的**可信值走。0 仍然可以被采信（没有正基准时），
    # 它只是不参与定基准。
    #
    # ⚠️ 这不影响「军力不等于 0」那条 bot 判据：`targets_from_rows` 喂给
    # `is_bot_entry` 的是**原始读数** `row.score`，不是这里的结果。
    positive = [score for score in scores if score is not None and score > 0]
    # ⚠️⚠️ **降序基准不吃锚点，只在屏内相邻两行之间用。**
    #
    # 屏内相邻两行确实必须降序（榜单就是这么排的）。但**跨屏**隔着重叠区和读数
    # 噪声，拿严格降序去卡上一屏交下来的锚点，就是在惩罚好数据：
    #
    # 2026-08-24 生产实测（206 屏丢了 235 行的分数）：
    #
    #     锚点 29760   被丢的值 29660 · 29640 · 29630 · 29590   ← 全都比锚点小、降序完好
    #     锚点 25190   被丢的值 29130 · 29110 · 29110 · 29110   ← 锚点比被丢的值还低 4000
    #
    # 成因是**级联**：上一屏有几行被误判丢掉 → 它的最大值偏低 → 这个偏低的锚点传
    # 给下一屏 → 下一屏本来正确的高值撞上「破坏降序」被丢 → 它的最大值又偏低……
    # 而自愈闸只在「整屏一个都没采信」时才重置锚点，这种「每屏丢几行」永远触发不到。
    #
    # 跨屏那一步交给下面 `basis` 的**倍数判据**就够了，#251 要挡的两种漏网它都挡得住：
    #
    #     93,670 落在屏首      比上一屏最大值（约 9,700）大 5 倍以上 → `too_big`
    #     整屏偏小 10 倍       比锚点小 5 倍以上                     → `too_small`
    #     锚点被读低 4,000     只差 1.16 倍，两条都不触发 → 放行（正是要的）
    #
    # ⚠️ **不要因为「降序是硬事实」就把锚点加回 `last`。** 硬事实是「相邻两行降序」，
    # 而锚点和本屏首行之间隔着一整个重叠区，它们不是相邻两行。
    last: float | None = None
    # **断崖基准**两个方向都按倍数判，所以没有锚点时可以拿中位数起头：
    # 它抗单点异常（一个坏值动不了它），够挡住整趟第一屏那个偏大的首行。
    # 取正值的中位数——理由见上面那段。
    basis = anchor if anchor else (statistics.median(positive) if positive else None)
    # 与 `trusted` 一一对应：可信的那几位是 None，被丢的那几位是丢它的理由。
    # ⚠️ **判据和理由必须出自同一次遍历。** 另写一个函数去复算理由就是「同一件事两份
    # 实现」，两边迟早分家——而分家之后日志会理直气壮地说错话。
    reasons: list[str | None] = []

    # ⚠️ 首尾框得住这一屏时，**逐行降序那条判据换成区间判据**，理由整段在
    # `screen_bracket` 上：逐行判会被一个「读小了却仍然递减」的错读带跑基准，
    # 把它后面每一行正确的值都判成逆序（实测被丢的行里 75.2% 是这么来的）。
    # 框不住（首尾自己就不站得住）就交 `None`，下面原样走逐行判据。
    bracket = screen_bracket(scores)

    trusted: list[float | None] = []
    references: list[float | None] = []
    for index, score in enumerate(scores):
        if score is None:
            trusted.append(None)
            reasons.append(None)
            references.append(None)
            continue
        # ⚠️⚠️ **有曲线参照时，只听曲线的。**
        #
        # 下面那三条（区间 / 逐行降序 / 断崖）参照的都是**单个**值——旁边那一行、
        # 本屏首尾、或上一屏交下来的锚点——而那个单点自己可能就是错的。曲线参照取
        # 的是名次相近的多个历史点的中位数，少数坏点动不了它（整段账在
        # `curve_reference`）。两套一起跑等于让那个坏的单点仍有否决权，
        # 前面两版栽的正是这个。
        rank = ranks[index] if ranks is not None and index < len(ranks) else None
        # ⚠⚠ **`require_both_sides=False` 不是把判据放宽，是这条分支能不能走到的前提。**
        # 这里是严格的前向单程（下面采信一个值才 `history.append`），所以判到某一行时
        # 历史里只有名次更小的点——要求两侧等于让这整条分支永远不执行。
        # 整段在 `curve_reference` 上；那里也记了为何补数那一步得反过来。
        reference = (
            curve_reference(history, rank, require_both_sides=False)
            if history is not None and rank is not None
            else None
        )
        references.append(reference)
        if reference is not None:
            why = DROP_OFF_CURVE if abs(score / reference - 1) > CURVE_TOLERANCE else None
        else:
            if bracket is not None:
                low, high = bracket
                # ⚠️ 区间是闭的：首尾两行自己必须留得住，否则锚点就没了来源。
                order_reason = None if low <= score <= high else DROP_OUT_OF_BRACKET
            else:
                order_reason = DROP_OUT_OF_ORDER if (last is not None and score > last) else None
            too_big = basis is not None and score > basis * cliff_factor
            too_small = basis is not None and score * cliff_factor < basis
            # ⚠️ **理由按这个次序取第一个命中的，而不是拼成一串。** 一行同时撞上两条
            # 时处置只看最要紧那条：数量级错了就是读错了，跟它在不在区间里无关。
            why = DROP_TOO_BIG if too_big else DROP_TOO_SMALL if too_small else order_reason
        if why is not None:
            trusted.append(None)
            reasons.append(why)
            continue
        trusted.append(score)
        reasons.append(None)
        if score > 0:
            last = score
            basis = score
            # ⚠️ **只有被采信的点才进历史。** 一个判错的读数进了历史就会把中位数
            # 往错的方向拽，而这条链路是自我强化的——同 `trusted_scores` 里那条
            # 「锚点只跟着可信值走」。
            if history is not None and rank is not None:
                history.append((rank, score))
    return Judgement(trusted=trusted, reasons=reasons, references=references)


def score_drop_reasons(
    scores: Sequence[float | None],
    *,
    anchor: float | None = None,
    cliff_factor: float = SCORE_CLIFF_FACTOR,
    ranks: Sequence[int | None] | None = None,
    history: list[tuple[int, float]] | None = None,
) -> list[str | None]:
    """每一行**为什么**被丢：可信的位置是 `None`，被丢的位置是理由。

    ⚠️ 它和 `trusted_scores` **走同一次遍历**（内部共用 `judge_scores`），不是复算一遍。
    日志据此说「这一行是被哪条判据拦的」——三条判据的处置完全不同：

    - `出界` / `破坏降序` → 识别层读错了低位，上下邻居插值补得回来
    - `比基准大/小一个数量级` → 丢首位或多一位，是 ROI / 小数点那类缺陷

    混成一句「不可信」等于没说，而这正是 2026-09-02 查这件事时最费劲的一步。
    """
    return judge_scores(
        scores, anchor=anchor, cliff_factor=cliff_factor, ranks=ranks, history=history
    ).reasons


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
    "CURVE_FIT_POINTS",
    "CURVE_MAX_GAP",
    "CURVE_RANK_SPAN",
    "CURVE_TOLERANCE",
    "SCORE_RULE_VERSION",
    "curve_reference",
    "SCREEN_SPREAD_LIMIT",
    "score_drop_reasons",
    "screen_bracket",
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
    "SCORE_CLIFF_FACTOR",
    "descending_breaks",
    "screens_overlap",
    "trusted_scores",
    "interpolate_scores",
    "is_bot_coordinate",
    "is_bot_entry",
    "Judgement",
    "judge_scores",
    "mentions_bot",
    "repair_ranks",
]
