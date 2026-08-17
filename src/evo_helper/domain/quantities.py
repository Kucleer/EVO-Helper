"""游戏画面上那些「带单位的数」的**唯一**解析口。

军力榜读到的是 `64.96K`，战报的「获得资源」读到的是 `928K` / `501.1K` / `233`，
太空舱的材料页读到的是 `5.388.122`。三处长得不一样，但要回答的是同一个问题：
**这一串字符代表多大的数，以及它准不准。**

## 为什么下沉成一份

原先只有军力榜那一份（`tools.ranking_scan.parse_score`）。战报资源如果另写一份，
两份实现迟早分家——而分家那天不会有人发现：两边都「读出了数」，只是其中一边
读错了三个数量级。所以新的格式一律加在这里，两条链路共用。

## ⚠️ 点有两种含义，判据只有一条

```
5.388.122   ← 太空舱材料页：点是千分位分隔符，这是 5388122
2.51K       ← 战报 / 军力榜：点是小数点，这是 2510
```

判据是：

> **带 K/M/B 后缀 → 点是小数点；不带后缀 → 点是千分位分隔符。**

「不带后缀时点是小数点」这个解读天然不成立——这些数量全是整数，`5.388.122`
读成小数根本无从读起。反过来，带后缀时游戏只写一到两位小数（`501.1K`），
从来不写千分位（超过一千就换单位了）。

千分位那一支还要求分组**恰好三位**（`\\d{1,3}(?:\\.\\d{3})+`）：`1.349` 满足，
是 1349；`1.5` 不满足，仍按小数读作 1.5（军力榜插值会产生这种半数，见
`domain.ranking.interpolate_scores`）。

## ⚠️ 换算必须走 `Decimal`

`float("64.96") * 1000` 给出 `64959.99999999999`。这批脏值**落过库**
（`bot_targets.military_score`），页面上显示成一串小数尾巴。
`Decimal("64.96") * 1000` 得到 `64960.00`——十进制字面量按十进制乘，误差不产生。

## 近似标记不是装饰

带后缀的数是游戏**四舍五入之后**显示的，原值取不回来了：`3.7M` 的真值在
±50000 之内的某处。用户接受这个精度（口径 2026-08-17：合计 123.4K 时误差在
10K 之内即可，实测最差的 `3.7M` 相对误差只有 1.35%），**但接受误差不等于
可以把近似值显示得像精确值**。所以每个读数都带着 `approximate` 与
`uncertainty` 两个标记出门，页面照着写「约」和误差范围。

`uncertainty` 记的是**显示时的有效位数**折算出来的半个末位刻度：

===========  ==========  ==============
显示          末位刻度     `uncertainty`
===========  ==========  ==============
``928K``     ``1000``    ``500``
``501.1K``   ``100``     ``50``
``3.7M``     ``100000``  ``50000``
``233``      ``1``       ``0``
===========  ==========  ==============

有效位数不同，误差差三个数量级——所以不能对所有 K 值统一按 ±50 算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

#: 后缀与倍率。`B` 是为战报资源加的——收获值到十亿的量级只是时间问题，
#: 而缺一档后缀的下场不是报错，是整串被判为「读不出」然后静默丢掉。
SUFFIX_SCALES: dict[str | None, int] = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, None: 1}

#: 千分位分隔的整数：首组一到三位，其后每组**恰好三位**。
#: 分组必须严格三位，否则 `1.5` 会被读成 15，而它是军力榜插值的合法产物。
_GROUPED_RE = re.compile(r"\d{1,3}(?:\.\d{3})+")

#: 带可选后缀的数。裸数（无后缀、无分组）也走这一支。
_SUFFIXED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMB])?")


@dataclass(frozen=True, slots=True)
class Quantity:
    """一次读数：值、准不准、以及不准到什么程度。"""

    #: 换算之后的值。`Decimal` 而不是 `float`——理由见模块头。
    value: Decimal
    #: 画面上是缩写显示的（`928K`），真值取不回来了。
    approximate: bool
    #: 最大绝对误差（半个末位刻度）。精确值是 0。
    uncertainty: int

    @property
    def amount(self) -> int:
        """整数化的数量。

        ⚠️ **非整数在这里直接抛，不悄悄截断。** 资源数量全是整数，读出一个
        `1.5` 只可能是把别处的格式误读到了这里——截成 1 会让它看起来像一次
        正常的读数，而那正是最难查的一类错。
        """
        if self.value != self.value.to_integral_value():
            raise ValueError(f"数量不是整数，拒绝截断：{self.value}")
        return int(self.value)


def parse_quantity(text: str) -> Quantity | None:
    """把画面上的一串数字读成 `Quantity`；读不出返回 None。

    支持三种写法，判据见模块头：

    - ``928K`` / ``501.1K`` / ``3.7M`` / ``1.2B`` → 带后缀，近似
    - ``5.388.122`` → 千分位分隔，精确
    - ``233`` → 裸数，精确

    读不出就是 None，**不给兜底值**：0 和「没读出来」在下游是两件事。
    """
    compact = text.strip().upper().replace(",", "")
    if not compact:
        return None
    if _GROUPED_RE.fullmatch(compact):
        return Quantity(Decimal(compact.replace(".", "")), approximate=False, uncertainty=0)
    match = _SUFFIXED_RE.fullmatch(compact)
    if match is None:
        return None
    digits, suffix = match.group(1), match.group(2)
    scale = SUFFIX_SCALES[suffix]
    decimals = len(digits.partition(".")[2])
    # 末位刻度 = 倍率 / 10^小数位；误差取它的一半（四舍五入显示）。
    ulp = Decimal(scale) / (10**decimals)
    return Quantity(
        Decimal(digits) * scale,
        approximate=suffix is not None,
        uncertainty=int(ulp / 2) if suffix is not None else 0,
    )


__all__ = ["SUFFIX_SCALES", "Quantity", "parse_quantity"]
