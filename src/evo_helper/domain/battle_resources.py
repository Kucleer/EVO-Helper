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

这套设计已经兑现过一次：对照表（`SLOT_LABELS`）先是空的，数量照记；名字确认之后
只改了那一行常量，库里的历史数据自动跟着对上，一条数据都不用迁。**后来的人也别把
名字写进库**——下一次改名或者改版时，能一行改完的前提就是库里存的仍是位置。

⚠️ 而那次留空是必要的，不是谨慎过头：确认下来的顺序**和「太空舱」页的显示顺序
并不一致**（`SLOT_LABELS` 上写着是哪两格对调）。当初按太空舱的序号抄进去，
就会正好踩中上一段说的那种「错得很安静」。
"""

from __future__ import annotations

from collections.abc import Sequence

from evo_helper.domain.quantities import parse_quantity
from evo_helper.domain.records import BattleResourceEntry

#: 网格形状。行优先编号，`slot = row * COLUMNS + column`。
GAINED_GRID_COLUMNS = 4
GAINED_GRID_ROWS = 3
GAINED_SLOT_COUNT = GAINED_GRID_COLUMNS * GAINED_GRID_ROWS

#: 游戏「太空舱 → 材料」页上列出的材料名，**按那一页的显示顺序**
#: （用户 2026-08-17 给的原样）。
#:
#: ⚠️ **这不是槽位顺序，而且留在这里正是为了证明它不是。** 把它和
#: `SLOT_LABELS` 并排看：太空舱是 `暗能量 / 合金碎片 / 银河素`，战报网格是
#: `暗能量 / 银河素 / 合金碎片`——中间两项对调。当初按序号抄进 `SLOT_LABELS`
#: 的话，那两种资源会整体对错，而且数字全对、页面上毫无异样。
#:
#: 这张表也**推不出**那 12 格：常规三种不在这一页上，`银河石碎片` /
#: `银河石能量` 同样不在（用户明确指出）。「9 + 3 = 12」是巧合，不是算式。
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

#: 槽位到资源名的对照表。用户口径（2026-08-17）逐格给的，顺序按
#: **先左到右、再上到下**——也就是本模块的 slot 编号。
#:
#: 这张表**只在这里出现一次**，库里存的仍是 0..11。名字改了不用动库，
#: 历史数据一起跟着对上——这就是存 slot 不存名字的全部理由。
#:
#: ## ⚠️ 三条事实，谁想「修正」这张表之前必须先读完
#:
#: 1. **前 3 项（金属 / 晶体 / 气体）是常规资源，根本不在「太空舱」页里。**
#:    独立佐证就在同一屏上：`残骸` 那一行只有 3 格，图标与 `获得资源` 的前三格
#:    完全相同——残骸本来就只出这三种。
#:
#: 2. ⚠️ **顺序与「太空舱」页的显示顺序不一致。** 太空舱是
#:    `暗能量 / 合金碎片 / 银河素 / …`，战报网格是 `暗能量 / 银河素 / 合金碎片 / …`
#:    ——**slot 4 与 slot 5 相对太空舱是对调的**。照太空舱的顺序去「修正」这张表，
#:    就会把银河素和合金碎片整体对错：数字全对，只是安在了另一种资源名下，
#:    页面上一点异样都没有。
#:
#: 3. **最后两项（银河石碎片 / 银河石能量）同样不在太空舱页里**（用户明确指出）。
#:    所以这 12 项**推导不出来**：「太空舱前 9 项 + 3 个常规 = 12」这个算式看着
#:    严丝合缝，其实是巧合。拿它去「验证」这张表只会得出错误结论。
#:
#: ⚠️ 上面第 2 条**只管 slot 4 / slot 5 那一处对调**，别顺手推广成「战报和太空舱
#: 处处不同」。名字本身是同一套：slot 6 就是太空舱页上那个「晶体矿石」
#: （用户 2026-08-17 更正：先前口述的「晶体碎片」是笔误，以太空舱为准）。
SLOT_LABELS: tuple[str | None, ...] = (
    "金属",
    "晶体",
    "气体",
    "暗能量",
    "银河素",
    "合金碎片",
    "晶体矿石",
    "能量凝胶",
    "泰坦立方",
    "收割者碎片",
    "银河石碎片",
    "银河石能量",
)


def slot_label(slot: int) -> str:
    """这一格在页面上叫什么。

    12 格眼下全都核对过了（见 `SLOT_LABELS`），所以正常情况下不会走到「第 N 格」
    那条回落。**回落仍旧留着**：将来游戏把网格加宽（`GAINED_SLOT_COUNT` 跟着变大）
    时，多出来的格子在有人逐格核对之前只有位置、没有名字——那时候按位置说话，
    比顺手编一个名字安全得多。
    """
    if not 0 <= slot < GAINED_SLOT_COUNT:
        raise IndexError(f"槽位 {slot} 不在 0..{GAINED_SLOT_COUNT - 1} 之内")
    label = SLOT_LABELS[slot] if slot < len(SLOT_LABELS) else None
    return label or f"第 {slot + 1} 格"


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
