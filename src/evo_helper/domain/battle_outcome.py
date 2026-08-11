"""这一仗算赢算输，由**剩余舰艇数**算出来，不看画面上那行大字。

用户口径（2026-08-11）：

> 关于胜负 你可以查看本方或者对方剩余舰艇数量，如果本方剩余0则战败，如果对方被
> 全歼则胜利。如果对方未被全歼，我方也未被全歼，则为平均（平局），不需要通过
> 游戏内的其他提示

于是三条规则，输入只有四个数：

    剩余 = 单位 − 损失单位          （两行都在战斗详情页上）

    本方剩余 0                      → FAIL
    对方剩余 0（被全歼）            → VICTORY
    两边都还有船                    → DRAW

## 为什么这是规则而不是读数，所以住在 domain 层

它会变——「全歼」的判据、平局怎么算、以后要不要把防御设施单独算，都是口径问题。
读数那一侧（哪个 ROI、放大几倍、门槛多少）是另一回事，变的原因也完全不同。
两者混在一起，改口径就得动 OCR 代码，而 OCR 的回归样本又证明不了口径对不对。

## 为什么不再用 `VICTORY` / `FAIL` 那行大字当判据

那行横幅**读得出来**——R−B 通道剥出来之后七张实拍逐字节一致（见
`vision.optional.report_screens.outcome_banner`）。但用户明确说了不要依赖它，
而且这条算术规则还顺带解决了一件读横幅解决不了的事：**平局不需要样本**。
仓库里 7 张详情页只有 `VICTORY` 与 `FAIL` 两种横幅，平局长什么样谁也没见过；
按剩余数算，`DRAW` 是算出来的，不是认出来的。

横幅现在只当**交叉校验**：两边都算得出、结论却不一致时留一条 warning，
判据仍以本模块为准。

## 用现成实拍核过

    pir1-detail   我方 单位 100 损失 0    → 剩余 100（还有船）
                  对方 单位 783 损失 783  → 剩余 0（被全歼）    → VICTORY
"""

from __future__ import annotations

#: 三个取值。存的是**游戏画面上那行大字的原文**——即使现在判据不再看它，
#: 库里那一列的口径没变（见 `domain.records.BattleReport.outcome`）：
#: 界面要显示中文是渲染层的事，库里只存这三个词之一。
OUTCOME_VICTORY = "VICTORY"
OUTCOME_FAIL = "FAIL"
OUTCOME_DRAW = "DRAW"

#: 词表顺序无关紧要，但要稳定：横幅的交叉校验按它做吸附。
OUTCOME_LABELS = (OUTCOME_VICTORY, OUTCOME_FAIL, OUTCOME_DRAW)


def survivors(units: int | None, losses: int | None) -> int | None:
    """剩余 = 单位 − 损失单位。任一读不出就返回 None。

    ⚠️ **缺一个数不能拿 0 顶替。** 「损失单位」那一行要把详情页拖到底才读得到
    （见 `vision.live_reports.LiveReportReader.read_detail_only`），所以它缺席
    会很常见；把缺席当成 0 会让「没读到」直接变成「一艘没损失」，
    再经这里就变成一场胜仗。

    损失多于单位是**不可能**的读数，说明其中一个读错了。这时也返回 None：
    在两个自相矛盾的数上判胜负，等于把一次 OCR 抖动变成一条战果记录。
    """
    if units is None or losses is None:
        return None
    remaining = units - losses
    return remaining if remaining >= 0 else None


def outcome_from_survivors(attacker: int | None, defender: int | None) -> str | None:
    """按剩余数判三值；任一侧算不出就返回 None（**不猜**）。

    两边同时归零时按 `FAIL`：用户把「本方剩余 0 则战败」列在第一条，
    而同归于尽本来也不该记成胜仗——舰队没了，目标却没占到便宜。
    """
    if attacker is None or defender is None:
        return None
    if attacker == 0:
        return OUTCOME_FAIL
    if defender == 0:
        return OUTCOME_VICTORY
    return OUTCOME_DRAW


def outcome_from_totals(
    *,
    attacker_units: int | None,
    attacker_losses: int | None,
    defender_units: int | None,
    defender_losses: int | None,
) -> str | None:
    """从详情页那两行的四个数直接算胜负。四个缺任何一个都返回 None。"""
    return outcome_from_survivors(
        survivors(attacker_units, attacker_losses),
        survivors(defender_units, defender_losses),
    )


__all__ = [
    "OUTCOME_DRAW",
    "OUTCOME_FAIL",
    "OUTCOME_LABELS",
    "OUTCOME_VICTORY",
    "outcome_from_survivors",
    "outcome_from_totals",
    "survivors",
]
