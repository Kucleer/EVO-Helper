"""回收报告邮件：把那一屏读成「这一趟捞回来多少」。

## ⚠️ 为什么读的是抵达信，不是返回信

用户口径（2026-09-12）：「回收报告中的货舱信息就是回收的资源，返回邮件太多了，
你去开封太浪费效率了」。

舰队标签里两种信的比例约 **2 : 1**（舰队返回 : 回收报告），而「舰队返回」的主题
**分不出攻击和回收**——必须开了读正文才知道，成本翻三倍（实测评估
`docs/回收闭环/邮件读实收-评估-2026-09-12.md`）。

## ⚠️⚠️ 三条判据都是实拍打出来的，不是设计出来的

1. **不投票，改用容量不变量挑**（2026-09-12 晚在 8 封语料上推翻了原方案）：
   `回收船数 × 每艘容量 ≈ 三样合计`。多框多配方出候选，让这条外部事实挑出
   对得上的那一组 —— 实测 **8/8，最大偏差 0.19%**。整段在 `pick_amounts`。
2. **⚠️ 「哪个框更好」是个伪问题**：窄框会把 `2.1M` 的小数点切掉读成 `21M`，
   宽框会把图标吃进来读成 `262M`，而它们**不在同一封信上同时死**
   （窄 7/8、宽 6/8、更宽 1/8）。所以把所有框的读数一起扔进候选，别挑框。
3. **单调（金属 > 晶体 > 气体）只能剪枝，不能当闸门**：用户给的这条规律 8 封
   全部成立，**但两封读错的也全部成立**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from evo_helper.domain.models import Coordinate
from evo_helper.domain.quantities import Quantity, parse_quantity

#: 三样的槽位号，与 `battle_resources.SLOT_LABELS` 同源（0 金属 / 1 晶体 / 2 气体）。
#: ⚠️ **不许把名字抄进来**——库里存的是位置，改名只改那一张表。
RECYCLE_SLOTS: tuple[int, int, int] = (0, 1, 2)

#: 容量不变量允许的相对偏差。
#:
#: 实测 8 封：好的六封 0.0%–0.2%，坏的两封 20.3% 与 5555%。3% 把这两群分得
#: 干干净净，而且离两边都远——不是卡着边界挑出来的。
CAPACITY_TOLERANCE = 0.03

#: 每艘回收船的货舱容量。**只当默认值用，判据要自校准**（见 `haul_is_consistent`）。
#:
#: ⚠️ **不许拿它当硬常量。** 货舱科技一升它就变，而写死的那天会把整条链路
#: 判成全错——症状是「突然一封都读不出来」，而真相是判据过期了。
NOMINAL_SHIP_CAPACITY = 20_000


@dataclass(frozen=True, slots=True)
class RecycleHaul:
    """一封回收报告读出来的东西。"""

    #: 邮件页眉上的时刻。**去重与归日都用它**（同 `MailRow.identity` 的口径）。
    reported_at_utc: datetime
    #: 出发星球与残骸所在的目标星球。
    origin: Coordinate
    target: Coordinate
    #: 金属 / 晶体 / 气体，顺序同 `RECYCLE_SLOTS`。
    #:
    #: ⚠️ 这三个是**近似值**（画面上写的是 `4.42M` 这种缩写），
    #: `Quantity.approximate` 带着这件事，页面要标「约」。
    amounts: tuple[Quantity, Quantity, Quantity]
    #: 回收船数。只用于容量校验，不入库。
    ships: int
    #: 页眉原文，留作出处。
    raw_time_text: str | None = None

    @property
    def total(self) -> Decimal:
        total: Decimal = Decimal(0)
        for item in self.amounts:
            total += item.amount
        return total

    @property
    def capacity_per_ship(self) -> Decimal | None:
        """这一封算出来的每艘容量。船数为 0 时 None。"""
        if self.ships <= 0:
            return None
        return self.total / Decimal(self.ships)


def amounts_are_consistent(
    total: Decimal, ships: int, *, capacity_per_ship: float | Decimal | None = None
) -> bool:
    """容量不变量本身：`船数 × 每艘容量` 对不对得上三样合计。

    ⚠️ **和 `haul_is_consistent` 分开，是因为视觉层手上没有坐标。**
    回收报告正文里那两个坐标 OCR **读得出但会读错**（实测 `[1:55:6]` 读成
    `[1:55:5]`、`4:452:13` 读成 `4:452:14`），所以整条链路**不拿它们当数据**——
    坐标一律取自被认领的那一发派遣。视觉层因此造不出 `RecycleHaul`，
    只能拿「合计 + 船数」过闸，过了才轮到认领去补坐标。
    """
    per_ship = Decimal(str(capacity_per_ship or NOMINAL_SHIP_CAPACITY))
    if ships <= 0 or per_ship <= 0:
        return False
    expected = per_ship * Decimal(ships)
    if expected <= 0:
        return False
    return abs(total - expected) / expected <= Decimal(str(CAPACITY_TOLERANCE))


def haul_is_consistent(
    haul: RecycleHaul, *, capacity_per_ship: float | Decimal | None = None
) -> bool:
    """容量不变量：`船数 × 每艘容量` 对不对得上三样合计。

    ``capacity_per_ship`` 留空时用 `NOMINAL_SHIP_CAPACITY`。
    **调用方应当传最近若干封算出来的中位数**——那样科技升级时判据自己跟着走，
    而写死的常量会在升级那天把每一封都判成读失败。

    ⚠️ **这是唯一挡得住「读出一个像样的错数」的闸门。** 实测两封坏的：

        3.07M / 1M / 280.25K   273 艘 → 合计 4.35M，容量 5.46M，差 20.3%
        413M  / 2.8M / 428.2K  368 艘 → 合计 416M，容量 7.36M，差 5555%

    两封的「金属 > 晶体 > 气体」都成立，肉眼也都像真数——只有这一条拦得住。
    """
    return amounts_are_consistent(haul.total, haul.ships, capacity_per_ship=capacity_per_ship)


def looks_monotonic(haul: RecycleHaul) -> bool:
    """金属 > 晶体 > 气体。

    ⚠️ **只是个弱校验，不许拿它当闸门。** 用户 2026-09-12 给的这条规律
    8 封全部成立——但那两封读错的**也全部成立**。留着是因为它免费，
    不满足时值得在日志里说一句；挡不住错数。
    """
    metal, crystal, gas = (item.amount for item in haul.amounts)
    return metal > crystal > gas > 0


def pick_amounts(
    candidates: tuple[Sequence[Decimal], Sequence[Decimal], Sequence[Decimal]],
    *,
    ships: int,
    capacity_per_ship: float | Decimal | None = None,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """从每格的若干候选里，挑出**合计对得上船数**的那一组。读不出时 None。

    ## ⚠️ 为什么是「挑」而不是「投票」

    #325 里这一步是多帧多配方的众数（`vote`）。**实测推翻了它**：语料
    `recycle-12092026-032646.png` 的晶体真值是 `2.1M`，而众数以 11:4 选出了 `1M`
    —— 窄框把小数点切掉，`2.1M` 读成 `21M`、`1M`，**错的那个反而票多**。
    投票的前提是「噪声随机、真值稳定」，可 ROI 切字是**系统性**的：同一个框在同
    一封信上每一帧都切掉同一位数字，投票只会把这个错误投得更结实。

    容量不变量没有这个毛病，因为它不问「哪个读数出现得多」，问的是
    **「哪一组凑得出船能装的量」**——那是一条外部事实。

    ## 实测（8 封语料，`docs/回收闭环/`）

    | 做法 | 通过容量校验 |
    |---|---|
    | 窄框单读 | 7/8 |
    | 宽框单读 | 6/8 |
    | 更宽框单读 | 1/8 |
    | **多框多配方 + 本函数挑** | **8/8**（最大偏差 0.19%） |

    ⚠️ **「哪个框更好」是个伪问题**：三个框各有各的死法（窄框切小数点、宽框把
    图标吃进来读成 `262M`），而它们**不在同一封信上同时死**。所以正确做法是
    把所有框的读数都当候选扔进来，让不变量挑，而不是挑一个框。

    ## ⚠️ 挑出来的也必须过闸

    本函数返回的是「最接近的一组」，它**总能返回点什么**。调用方必须再用
    `haul_is_consistent` 卡一次 —— 真值那一组实测偏差 ≤0.19%，而次优的那一组
    差 6–100 倍（0.36% / 2.29%），两群离得很开。

    ``capacity_per_ship`` 留空时用 `NOMINAL_SHIP_CAPACITY`，理由同
    `haul_is_consistent`：**调用方应当传最近若干封算出来的中位数**，
    否则货舱科技一升，这里就会开始挑错。
    """
    if ships <= 0:
        return None
    per_ship = Decimal(str(capacity_per_ship or NOMINAL_SHIP_CAPACITY))
    if per_ship <= 0:
        return None
    expected = per_ship * Decimal(ships)
    if expected <= 0:
        return None
    best: tuple[Decimal, tuple[Decimal, Decimal, Decimal]] | None = None
    for metal in candidates[0]:
        for crystal in candidates[1]:
            for gas in candidates[2]:
                # ⚠️ 单调只当**剪枝**用，不当闸门（见 `looks_monotonic`）：
                # 它 8 封全成立、连读错的那两封也成立，所以它筛不掉错的，
                # 只能少试几组。真正定胜负的是下面那个偏差。
                if not metal > crystal > gas > 0:
                    continue
                deviation = abs(metal + crystal + gas - expected) / expected
                if best is None or deviation < best[0]:
                    best = (deviation, (metal, crystal, gas))
    if best is None:
        return None
    return best[1]


def parse_amounts(texts: tuple[str, str, str]) -> tuple[Quantity, Quantity, Quantity] | None:
    """三格文本 → 三个 `Quantity`。**任一格读不出就整封作废。**

    ⚠️ 不许只写读出来的那几格：少一格在库里长得和「那一格是 0」一模一样
    （同 `BattleReportResourceRow` 那条「全有或全无」）。而回收报告只有三个数，
    少一个就是少三分之一的收入。
    """
    parsed = [parse_quantity(text) for text in texts]
    if any(item is None for item in parsed):
        return None
    first, second, third = parsed
    assert first is not None and second is not None and third is not None
    return (first, second, third)


__all__ = [
    "CAPACITY_TOLERANCE",
    "NOMINAL_SHIP_CAPACITY",
    "RECYCLE_SLOTS",
    "RecycleHaul",
    "amounts_are_consistent",
    "haul_is_consistent",
    "looks_monotonic",
    "parse_amounts",
    "pick_amounts",
]
