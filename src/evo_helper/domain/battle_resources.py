"""战报「获得资源」那 12 格：网格常量、解析、以及位置到名字的对照表。

战报详情页**未滚动**那一屏上，VS 块底下就是这一块：

```
获得资源:
  [图标] 928K    [图标] 501.1K  [图标] 342.9K  [图标] 7.7K
  [图标] 0       [图标] 1.2K    [图标] 233     [图标] 0
  [图标] 66      [图标] 4       [图标] 0       [图标] 0
```

四列三行，**行优先**编号 0..11（第一行左起 0/1/2/3，第二行 4/5/6/7，第三行
8/9/10/11）。

## ⚠️ 网格固定是**用户确认过的前提**，不是这里推出来的

用户口径（2026-08-17）：格子位置固定。实证是把一份有非零收获的 VICTORY 战报
和一份全 0 的 FAIL 战报叠着比——**12 格的位置逐个对齐，只有值不同**；
值为 0 的格子照样占位显示 `0`，不会被压缩掉。

所以这里**不做图标模板匹配**，位置即类型。

⚠️ **一旦游戏改版、格子发生位移，症状是「数字都对、只是安在了别的资源名下」——
错得很安静。** 不会报错、不会读空，页面上每一格都有一个像模像样的数。
真要判断有没有位移，只有一条路：打开一份收获非零的战报，对着图标逐格核。

## ⚠️ 存 `slot` 不存资源名

位置是**观测到的事实**，名字是**解释**。解释错了以后还能靠 slot 重新映射；
把名字硬编进库里，原始观测就找不回来了。所以 `battle_report_resources`
存的是 0..11，翻译是页面渲染时才发生的事。

下面那张对照表眼下是空的（见 `SLOT_LABELS`），这正是这套设计在兑现它的价值：
名字没定下来不妨碍数量先记着，定下来那天改一行常量、历史数据一起跟着对。
"""

from __future__ import annotations

from collections.abc import Sequence

from evo_helper.domain.quantities import parse_quantity
from evo_helper.domain.records import BattleResourceEntry

#: 网格形状。行优先编号，`slot = row * COLUMNS + column`。
GAINED_GRID_COLUMNS = 4
GAINED_GRID_ROWS = 3
GAINED_SLOT_COUNT = GAINED_GRID_COLUMNS * GAINED_GRID_ROWS

#: 游戏「太空舱 → 材料」页上列出的材料名（用户 2026-08-17 给的原样）。
#:
#: ⚠️ **这不是槽位顺序**，只是把名字记在手边，省得下一个人再去翻界面。
#: 材料页的排列与战报网格的排列没有任何已证实的关系，按序号往 `SLOT_LABELS`
#: 里抄就是在编造——那正是本模块头「错得很安静」那条说的事。
MATERIAL_NAMES: tuple[str, ...] = (
    "暗能量",
    "合金碎片",
    "银河素",
    "晶体矿石",
    "能量凝胶",
    "泰坦立方",
    "收割者碎片",
    "湮灭之星核心",
    "弹头许可证",
    "贸易许可证",
)

#: 槽位到资源名的对照表。**全部留空 = 尚未逐格核对过。**
#:
#: 填它的办法只有一个：打开一份收获非零的战报，对着 12 个图标逐格与
#: 「太空舱 → 材料」页核，核一格填一格。**不要按 `MATERIAL_NAMES` 的顺序整体
#: 抄进来**——那是另一张页面的排版，两者对不对得上没人验过，而对不上的后果是
#: 每一格都显示着一个像模像样却属于别的资源的数。
#:
#: 空着不影响记录：数量按槽位入库，名字定下来那天改这一行，库里的历史数据
#: 一起跟着对上（这就是存 slot 不存名字的全部理由）。
SLOT_LABELS: tuple[str | None, ...] = (None,) * GAINED_SLOT_COUNT


def slot_label(slot: int) -> str:
    """这一格在页面上叫什么。没核对过就叫「第 N 格」，**不编名字**。"""
    if not 0 <= slot < GAINED_SLOT_COUNT:
        raise IndexError(f"槽位 {slot} 不在 0..{GAINED_SLOT_COUNT - 1} 之内")
    return SLOT_LABELS[slot] or f"第 {slot + 1} 格"


def parse_resource_grid(texts: Sequence[str]) -> tuple[BattleResourceEntry, ...] | None:
    """把 12 格的 OCR 原文解析成条目；**任一格读不出就整块作废**，返回 None。

    ## 为什么是全有或全无

    库里**只存非零的行**（`storage.models.BattleReportResourceRow`），也就是
    「没有这一格 = 这一格是 0」。这条语义只有在「12 格全读到了」的前提下才成立：
    只读到 8 格就入库，剩下 4 格会被后来的人当成 0——一次读不全就此变成四个
    凭空捏造的零，而且不留任何痕迹。

    所以读不全就一格都不存。代价是偶尔丢一份战报的收获数据，而那是**能看出来**
    的（那份战报一行资源都没有）；相反的取舍丢的是可信度，而且看不出来。

    ## 全 0 的战报会返回空元组

    空元组和 `None` 是两件事：空元组是「12 格都读到了，都是 0」，`None` 是
    「没读全」。调用方必须分得开——前者照常入库（0 行明细），后者一行都不写。
    """
    if len(texts) != GAINED_SLOT_COUNT:
        raise ValueError(f"获得资源必须是 {GAINED_SLOT_COUNT} 格，给了 {len(texts)} 格")
    entries: list[BattleResourceEntry] = []
    for slot, text in enumerate(texts):
        quantity = parse_quantity(text)
        if quantity is None:
            return None
        try:
            amount = quantity.amount
        except ValueError:
            # 非整数只可能是读串了（资源数量全是整数）。当成没读出来处理。
            return None
        if amount == 0:
            continue
        entries.append(
            BattleResourceEntry(
                slot=slot,
                amount=amount,
                approximate=quantity.approximate,
                uncertainty=quantity.uncertainty,
            )
        )
    return tuple(entries)


__all__ = [
    "GAINED_GRID_COLUMNS",
    "GAINED_GRID_ROWS",
    "GAINED_SLOT_COUNT",
    "MATERIAL_NAMES",
    "SLOT_LABELS",
    "parse_resource_grid",
    "slot_label",
]
