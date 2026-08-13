"""游戏里那种带 `K` 的舰队数量文本，读成艘数。

这个模块原先是 `domain.fleet_tier` 的一部分。分档（`FleetTier` / `TierThresholds`
/ `tier_for`）在 2026-08-13 随「bot 不再分档、一律 BBB」整套删掉了，而**解析这件
事和分档无关**：它现在的消费者全在读战报的那一侧——`vision.live_reports`、
`vision.pirate_reports`、`vision.optional.report_screens` 拿它把「单位」「损失单位」
那四个数读出来，而那四个数是 `domain.battle_outcome` 算胜负的唯一输入。
所以它跟着搬到一个按用途命名的模块，而不是留在一个已经名不副实的模块里。
"""

from __future__ import annotations

import re

#: 数量文本：`517`、`5.36K`、`1.09K`。K 是千。
_COUNT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([Kk])?$")


def parse_fleet_count(text: str) -> int | None:
    """把 `5.36K` / `517` 解析成艘数；认不出返回 None。

    `K` 是游戏自己的四舍五入显示，`5.36K` 的真实值在 5355–5364 之间。
    这里取 5360——胜负判定按「剩余 = 单位 − 损失」算，差这几艘不改变结论。

    ⚠️ **`M` 是故意不认的，不是漏了。** 读到 `1.5M` 返回 None，而 None 在
    `domain.battle_outcome.survivors` 那边的处置是**整份拒收、不判胜负**；
    认了它就等于凭一个从未在实机上见过的后缀去记一条战果。
    识别侧的白名单本来也只放行 `0123456789.K`
    （`vision.optional.report_screens.UNIT_WHITELIST`），`M` 根本进不来；
    真有一天游戏开始显示 `M`，要改的是那条白名单和这里，两处一起改一次。

    ⚠️ 这个函数**不是** 2026-08-11 那次量级错的成因。2:48:12 的守方单位
    实为 `1.22K`，`parse_fleet_count("1.22K")` 给出 1220（正确）；入库的
    122000 来自 `parse_fleet_count("122K")`——小数点在 OCR 那一层就掉了。
    修在选票那一层（`vision.fleet_counts.pick_count`），不在这里。
    """
    match = _COUNT_RE.match(text.strip())
    if match is None:
        return None
    value = float(match.group(1))
    return round(value * 1000) if match.group(2) else round(value)


__all__ = ["parse_fleet_count"]
