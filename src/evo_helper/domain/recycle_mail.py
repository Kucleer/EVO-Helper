"""回收报告邮件：把那一屏读成「这一趟捞回来多少」。

## ⚠️ 为什么读的是抵达信，不是返回信

用户口径（2026-09-12）：「回收报告中的货舱信息就是回收的资源，返回邮件太多了，
你去开封太浪费效率了」。

舰队标签里两种信的比例约 **2 : 1**（舰队返回 : 回收报告），而「舰队返回」的主题
**分不出攻击和回收**——必须开了读正文才知道，成本翻三倍（实测评估
`docs/回收闭环/邮件读实收-评估-2026-09-12.md`）。

## ⚠️⚠️ 三条判据都是实拍打出来的，不是设计出来的

1. **多帧投票**：详情页背后有一层漂浮文字（`-17003` / `COMMAND OFFICERS`），
   它会压进资源格**而且在动**——实测同一封信 5 帧之间变了 255→1157 个像素。
   单帧 OCR 在 8 格里错 2 格。
2. **框宁可宽不可紧**：收紧的后果不是读不出，是**读出一个像样的错数**
   （`529.7K` → `29.7K`）。前缀垃圾交给正则滤，缺首位数字没人救得回来。
3. **容量不变量**：`回收船数 × 每艘容量 ≈ 三样合计`。实测好的六封偏差 ≤0.2%，
   而两封读错的偏差 20% 与 5555%——**一刀切得干干净净**。

⚠️ 用户给的「金属 > 晶体 > 气体」8 封全部成立，**但两封读错的也全部成立**，
所以它只能当弱校验，不能当闸门。
"""

from __future__ import annotations

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
    per_ship = Decimal(str(capacity_per_ship or NOMINAL_SHIP_CAPACITY))
    if haul.ships <= 0 or per_ship <= 0:
        return False
    expected = per_ship * Decimal(haul.ships)
    if expected <= 0:
        return False
    return abs(haul.total - expected) / expected <= Decimal(str(CAPACITY_TOLERANCE))


def looks_monotonic(haul: RecycleHaul) -> bool:
    """金属 > 晶体 > 气体。

    ⚠️ **只是个弱校验，不许拿它当闸门。** 用户 2026-09-12 给的这条规律
    8 封全部成立——但那两封读错的**也全部成立**。留着是因为它免费，
    不满足时值得在日志里说一句；挡不住错数。
    """
    metal, crystal, gas = (item.amount for item in haul.amounts)
    return metal > crystal > gas > 0


def vote(readings: list[str]) -> str | None:
    """多帧多配方的众数。全都读不出时 None。

    ⚠️ **投票的前提是噪声在动而数字不动**：背景那层漂浮文字每帧都不一样，
    所以它在各帧里读出的垃圾各不相同，而真正的数字每帧都一样。
    一帧一配方的话这个前提用不上，实测 8 格错 2 格。
    """
    counts: dict[str, int] = {}
    for item in readings:
        text = item.strip().upper()
        if text:
            counts[text] = counts.get(text, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda pair: (pair[1], pair[0]))[0]


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
    "haul_is_consistent",
    "looks_monotonic",
    "parse_amounts",
    "vote",
]
