"""读一封「回收报告」详情页：页眉时刻 + 三样资源 + 回收船数。

## ⚠️ 它不是战报，读法也不该照战报那一套

没有 VS 块、没有战损、没有参战舰队 —— 只有页眉一行时刻、三个资源格、一行船数。
所以这个模块与 `vision.protection_bounce` 同形（同样「读不齐就整封拒收」），
而不是与 `vision.pirate_reports` 同形。

## ⚠️⚠️ 正文里那两个坐标**不读**

详情页正文写着「你的回收舰队（[1:55:6] …）已抵达 [1:27:19] …轨道」，看着是现成的
出发点与目标。**但实测它会读错**（2026-09-12 晚，8 封语料）：

    真值 [1:55:6]     读成 [1:55:5]
    真值 4:452:13     读成 4:452:14
    8 封里两个坐标都读全的只有 4 封

按琥珀色掩码抠出来之后画面是干净的（背景那层漂浮文字全滤掉了），错的不是分离，
是**字形本身在这个字号下就分不开**。而「读出一个像样的错坐标」比读不出危险得多：
它会把这一趟的实收记到别人头上。

⇒ **坐标一律取自被认领的那一发回收派遣**，这个模块只交出时刻、三样和船数。
认领怎么做在 `storage.repository` 那一侧。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from evo_helper.domain.quantities import Quantity
from evo_helper.domain.recycle_mail import amounts_are_consistent, pick_amounts
from evo_helper.vision.parsers import GAME_DISPLAY_ZONE, parse_report_timestamp

_TIME_TEXT_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\b")


class RecycleMailScreens(Protocol):
    def report_header(self) -> str: ...

    def recycle_amount_candidates(
        self,
    ) -> tuple[Sequence[Quantity], Sequence[Quantity], Sequence[Quantity]]: ...

    def recycle_ship_count(self) -> int | None: ...


class RecycleMailUnreadable(ValueError):
    """这一封读不齐或过不了容量闸；**不猜、不存半份**，下一趟它还在信箱里。"""


@dataclass(frozen=True)
class RecycleMailReading:
    """读通了的一封回收报告。

    ⚠️ `reported_at_utc` 是**舰队抵达残骸区的那一刻**（邮件自己盖的章），
    不是我们翻到它的时刻。认领就靠它对上派遣的 `expected_report_at_utc`。
    """

    raw_time_text: str
    reported_at_utc: datetime
    #: 金属 / 晶体 / 气体，顺序同 `domain.recycle_mail.RECYCLE_SLOTS`。
    #: 画面上写的是 `4.42M` 这种缩写，所以一律 `approximate=True`。
    amounts: tuple[Quantity, Quantity, Quantity]
    #: 回收船数。只用于容量校验，不入库。
    ships: int


def read_recycle_mail(
    screens: RecycleMailScreens, *, capacity_per_ship: float | Decimal | None = None
) -> RecycleMailReading:
    """把详情页读成一条实收。读不齐、或过不了容量闸，都抛。

    ## 两步，缺一不可

    1. `pick_amounts` 从每格的**多框多配方候选**里挑出合计对得上船数的那一组；
    2. `amounts_are_consistent` 再卡一次 —— 因为第 1 步**总会返回点什么**
       （它只是挑最接近的），不卡就会把一组垃圾当实收入库。

    ⚠️ **船数读不出就整封作废，不许退回投票。** 船数是这条不变量唯一的锚；
    没有它就没有判据，而 #325 那套众数实测会挑错（整段在 `pick_amounts`）。

    ``capacity_per_ship`` 留空时用标称值。**调用方应当传最近若干封算出来的
    中位数**，否则货舱科技一升，这里会开始整封整封地拒收，症状是「突然一封都
    读不出来」而真相是判据过期了。
    """
    header = screens.report_header()
    reported_at = parse_report_timestamp(header, GAME_DISPLAY_ZONE)
    if reported_at is None:
        raise RecycleMailUnreadable("邮件页眉没有可用的时间")
    time_text = _TIME_TEXT_RE.search(header)
    if time_text is None:
        raise RecycleMailUnreadable("邮件页眉没有可用的时间原文")
    ships = screens.recycle_ship_count()
    if not ships or ships <= 0:
        raise RecycleMailUnreadable("回收船数读不出；没有船数就没有容量判据")
    candidates = screens.recycle_amount_candidates()
    # ⚠️ 挑选只看数值，但**返回的必须是原来那个 `Quantity`**：画面上写的是 `4.42M`
    # 这种缩写，`approximate` 与 `uncertainty` 带着「这是个约数」这件事一路进库，
    # 页面据此标「约」。在这里重造一个 `uncertainty=0` 的会把约数说成精确值。
    by_value = [{item.value: item for item in cell} for cell in candidates]
    picked = pick_amounts(
        (sorted(by_value[0]), sorted(by_value[1]), sorted(by_value[2])),
        ships=ships,
        capacity_per_ship=capacity_per_ship,
    )
    if picked is None:
        counts = "/".join(str(len(cell)) for cell in candidates)
        raise RecycleMailUnreadable(f"三格挑不出一组读数（候选数 {counts}，船 {ships}）")
    amounts = (by_value[0][picked[0]], by_value[1][picked[1]], by_value[2][picked[2]])
    total = sum((item.value for item in amounts), Decimal(0))
    if not amounts_are_consistent(total, ships, capacity_per_ship=capacity_per_ship):
        raise RecycleMailUnreadable(f"挑出来的一组过不了容量闸：合计 {total} vs 船 {ships}；不猜")
    return RecycleMailReading(
        raw_time_text=time_text.group(0),
        reported_at_utc=reported_at,
        amounts=amounts,
        ships=ships,
    )


__all__ = [
    "RecycleMailReading",
    "RecycleMailScreens",
    "RecycleMailUnreadable",
    "read_recycle_mail",
]
