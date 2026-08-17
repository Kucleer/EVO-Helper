"""这一仗算赢算输的**兜底**算法：按剩余舰艇数推。

⚠️ **这不再是第一判据。** 用户口径（2026-08-17）：

> 游戏算法更新，剩余舰艇算法已经不准了，可以读 victory

也就是说游戏改了战斗结算，「剩余 = 单位 − 损失单位」推出来的结论会和实际战果
对不上；而画面上那行 `VICTORY` / `FAIL` 大字是游戏自己给的结论，它才是权威。
仲裁在 `vision.pirate_reports.decide_outcome`：**横幅读得出就用横幅**，
读不出来才回落到本模块，并留一条日志说明这一条是回落来的。

本模块之所以留着，是因为横幅只在**没拖过的那一屏**上（拖到底就滚出可视区，
实测读作 `'Z ?'`），而离线入库、旧截图、以及取字面实现没有 `outcome_banner`
的那几条路都拿不到它。回落总比整条战报没有战果强。

三条规则，输入只有四个数：

    剩余 = 单位 − 损失单位          （两行都在战斗详情页上）

    本方剩余 0                      → FAIL
    对方剩余 0（被全歼）            → VICTORY
    两边都还有船                    → DRAW

## 为什么这是规则而不是读数，所以住在 domain 层

它会变——2026-08-11 它是唯一判据，2026-08-17 就退成了兜底。读数那一侧
（哪个 ROI、放大几倍、门槛多少）是另一回事，变的原因也完全不同。
两者混在一起，改口径就得动 OCR 代码，而 OCR 的回归样本又证明不了口径对不对。

## 平局这一档没有横幅样本

仓库里 7 张详情页只有 `VICTORY` 与 `FAIL` 两种横幅，平局长什么样谁也没见过。
所以 `DRAW` 目前几乎只会从本模块的算式里出来。这不影响调度：
「平局就对同一坐标再打一发」已于 2026-08-17 按用户口径移除
（见 `domain.bot_round` 模块头），`DRAW` 如今只是展示用的一档战果。

## 用现成实拍核过

    pir1-detail   我方 单位 100 损失 0    → 剩余 100（还有船）
                  对方 单位 783 损失 783  → 剩余 0（被全歼）    → VICTORY
                  同一份报告的横幅读作 `VICTORY`——两条路对得上
"""

from __future__ import annotations

#: 三个取值。存的是**游戏画面上那行大字的原文**（见
#: `domain.records.BattleReport.outcome`）：界面要显示中文是渲染层的事，
#: 库里只存这三个词之一。
OUTCOME_VICTORY = "VICTORY"
OUTCOME_FAIL = "FAIL"
OUTCOME_DRAW = "DRAW"

#: 词表顺序无关紧要，但要稳定：横幅的读数按它做吸附。
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

    ⚠️ 这是**兜底**，不是第一判据——游戏 2026-08-17 改了结算算法，这套推法
    已经会和实际战果对不上。仲裁见 `vision.pirate_reports.decide_outcome`。

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
