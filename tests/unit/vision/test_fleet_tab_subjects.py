"""信箱「报告 → 舰队」标签里那四种信的主题分类。

## ⚠️ 这里守的不是「读它们」，恰恰是「别读它们」

`MailRow.may_be` 对 `UNKNOWN` **一律放行**（那条偏向是为战报那一趟定的：漏开一封
= 少一份战报，多开一封 = 多花八秒）。可读回收报告那一趟走的是同一道闸，而它的
预算只有个位数 —— 任何一种主题在分类器里认不出来，那一趟就会把它整封开掉。

实拍（2026-09-13）确认这个标签里**恰好四种**信：

    舰队返回 · 回收报告 · 矮星系统战报 · 部署报告

⚠️ 其中只有「回收报告」是要读的，另外三种加进分类器**纯粹是为了在列表页免费拒掉**。

## ⚠️ 「矮星系统战报」原先是碰巧对的

它含「战报」二字，靠分类器最后那条兜底落到 `SYSTEM`，而战报那一趟不要 `SYSTEM`
——行为正确，但没有任何东西守着。兜底一改它就跟着变，而症状会是「星门战报开始
吃开封预算」，在日志上和「信箱里信变多了」长得一样。
"""

from __future__ import annotations

import pytest

from evo_helper.tools.pirate_loop import mail_row_from_text
from evo_helper.vision.parsers import ReportKind, classify_report_subject

#: 舰队标签里的四种主题 → 各自该落哪一档。实拍原文（含 OCR 常见的前缀垃圾）。
FLEET_TAB_SUBJECTS = (
    ("舰队返回", ReportKind.FLEET_RETURN),
    ("SS  舰队返回", ReportKind.FLEET_RETURN),
    ("回收报告", ReportKind.RECYCLE),
    ("OA 回收报告 >从", ReportKind.RECYCLE),
    ("矮星系统战报", ReportKind.STARGATE),
    ("pec 矮星系统战报", ReportKind.STARGATE),
    ("部署报告", ReportKind.DEPLOY),
)


@pytest.mark.parametrize(("subject", "expected"), FLEET_TAB_SUBJECTS)
def test_every_fleet_tab_subject_is_recognised(subject: str, expected: ReportKind) -> None:
    """四种都要认出来，**一种都不许落 `UNKNOWN`**。"""
    assert classify_report_subject(subject) is expected


@pytest.mark.parametrize(("subject", "expected"), FLEET_TAB_SUBJECTS)
def test_only_the_recycle_report_is_worth_opening_on_that_trip(
    subject: str, expected: ReportKind
) -> None:
    """⚠️ **本文件的重点。**

    读回收报告那一趟只要 `RECYCLE`。另外三种必须在列表页就被主题闸拒掉 ——
    它们认不出来（落 `UNKNOWN`）时 `may_be` 会放行，那一趟个位数的预算
    就会被白开吃光。
    """
    row = mail_row_from_text(0, f"{subject}\n12/09/2026 03:26:46")

    assert row.may_be(ReportKind.RECYCLE) is (expected is ReportKind.RECYCLE)


def test_the_stargate_report_no_longer_rides_on_the_generic_battle_report_fallback() -> None:
    """「矮星系统战报」要走自己那一条，不是靠「含『战报』二字」的兜底。

    ⚠️ 兜底仍然存在、也仍然该存在（别的「…战报」还得有地方去），
    这一条只保证星门那封不再依赖它。
    """
    assert classify_report_subject("矮星系统战报") is ReportKind.STARGATE
    # 兜底本身没被拆掉
    assert classify_report_subject("某种没见过的战报") is ReportKind.SYSTEM


def test_the_attack_report_is_untouched() -> None:
    """⚠️ 新加两条子串判定**不许碰到攻击报告那一条**。

    「攻击报告」是唯一能认领派遣的那一档，判错它的后果是关掉错误的那一发攻击。
    """
    assert classify_report_subject("攻击报告") is ReportKind.ATTACK
    assert classify_report_subject("海盗攻击报告") is ReportKind.PIRATE
