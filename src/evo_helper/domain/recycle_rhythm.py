"""回收节奏的误差累加（error diffusion）——整数十分位，绝不用浮点。

用户口径（2026-09-10）：滑块 r ∈ {0.0, 0.1, ..., 1.0}；拉到 1 每发攻击返回后都回收，
拉到 0.6 每 10 次回收 6 次。**全局一个值。**

⚠️⚠️ **必须用整数十分位，不能用浮点。** 浮点误差累加实测复现：

    r      浮点前 10 发命中    应该
    0.1          0              1     ← 拉到 0.1 前十发一次都不收
    0.3          2              3
    0.6          5              6

每一档都少一次，而且不收敛。原因：`0.2 + 0.6 + 0.6 − 1 = 0.39999…`。
`acc` 要落库、跨重启累积，浮点误差只会越滚越大。

整数版：

    R   = 滑块 × 10   存 0–10 的整数
    acc              存 0–9  的整数
    阈值 10

    每有一发攻击的航线释放：  acc += R
                             若 acc >= 10:  acc -= 10，选中

R = 6 时的序列：`6 12✓ 8 14✓ 10✓ 6 12✓ 8 14✓ 10✓` → **10 发里正好 6 发**，
而且是均匀铺开的。

⚠️ **`acc -= 10` 发生在「决定去看」的那一刻，不是「派成功」的那一刻。**
到了没残骸也不回退（用户确认）：滑块管的是**尝试**的节奏，不是成功次数。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 阈值：acc 达到这个数就选中一次。
THRESHOLD = 10


@dataclass(frozen=True)
class AccStep:
    """一步误差累加的结果。"""

    acc_before: int
    acc_after: int
    selected: bool


def step_acc(acc_before: int, rate_tenths: int) -> AccStep:
    """一步误差累加：`acc += R`，够 10 就减 10 并选中。

    ⚠️ `rate_tenths` 必须是 0–10 的整数；`acc_before` 必须是 0–9 的整数。
    调用方负责保证（从库里读出来时已经夹过）。
    """
    if rate_tenths <= 0:
        return AccStep(acc_before=acc_before, acc_after=acc_before, selected=False)
    acc = acc_before + rate_tenths
    if acc >= THRESHOLD:
        return AccStep(acc_before=acc_before, acc_after=acc - THRESHOLD, selected=True)
    return AccStep(acc_before=acc_before, acc_after=acc, selected=False)


def simulate(rate_tenths: int, n: int) -> int:
    """连跑 n 发，返回命中次数。用例钉死：r=0.6 × 100 = 60；r=0.1 × 100 = 10。"""
    acc = 0
    hits = 0
    for _ in range(n):
        result = step_acc(acc, rate_tenths)
        acc = result.acc_after
        if result.selected:
            hits += 1
    return hits
