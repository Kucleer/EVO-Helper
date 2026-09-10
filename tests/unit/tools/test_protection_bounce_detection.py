"""列表行认不出「到达时撞保护期」，所以开封之后必须拿正文再判一次。

## ⚠️ 这一组的全部依据是 2026-09-09 的实拍，不是假想的屏幕

那一封（`[4:277:14]`、邮件时刻 12:27:13）在两处长这样：

    列表行     攻击报告 / System / 09/09/2026 12:27:13     ← 没有正文预览
    详情页     标题栏「消息」；主题: 攻击报告
               正文  [4:277:14]（bot_4_277_14's Planet）处于保护状态，我方舰队已返航。

⇒ **它的主题和普通战报一字不差。** 而 `classify_report_subject` 判这一种要求
整句话都在（`PROTECTION_BOUNCE_RE`：坐标 + 「处于保护状态」+ 「已返航」），
列表行上根本没有那句话，于是它被判成 `ATTACK`、进战报解析、没有 VS 块、
读不出、**静默丢掉**，那一发派遣从此永远挂在「到点还没战报」上。

生产库实测（2026-09-09）：近 12 天 **153 发**这样挂着。它们让
`_stop_after_known()` 几乎从不触发（`MAX_REPORT_AGE` 6 小时的窗口里平均挂
**3.59 发**、单子为空只占 **19.8%**），每趟信箱因此开满 `MAIL_MAX_OPENS`。

⚠️ 这个缺陷藏了这么久，是因为**整套 `protection_bounce` 是照假想的屏幕写的**：
原有用例把 `MailRow.subject` 直接设成正文、`kind` 直接设成 `PROTECTION_BOUNCE`，
于是列表行那一步从来没被验过。本文件只用实拍里的原文。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from evo_helper.tools.pirate_loop import MailRow, PirateLoop
from evo_helper.vision.parsers import ReportKind

#: ⚠️ 实拍原文，一字不改（含全角括号与全角逗号）。
REAL_BODY = "[4:277:14]（bot_4_277_14's Planet）处于保护状态，我方舰队已返航。"

#: ⚠️ 实拍的列表行文字：**只有主题和发件人**，没有正文预览。
REAL_ROW_SUBJECT = "攻击报告 System"

#: ⚠️ `GAME_DISPLAY_ZONE` 就是 UTC —— 页眉上写的 `12:27:13` 直接是 UTC，
#: 不是 UTC+8。第一版我按 UTC+8 减了 8 小时，用例当场变红。
MAIL_AT = datetime(2026, 9, 9, 12, 27, 13, tzinfo=UTC)


def _row(kind: ReportKind, subject: str = REAL_ROW_SUBJECT) -> MailRow:
    return MailRow(
        index=2,
        subject=subject,
        raw_time_text="09/09/2026 12:27:13",
        reported_at_utc=MAIL_AT,
        kind=kind,
    )


class _Detail:
    """详情页替身。`security_message` 是 `read_protection_bounce` 取正文的那一块。"""

    def __init__(self, body: Any = REAL_BODY) -> None:
        self._body = body
        self.asked = 0

    def security_message(self) -> Any:
        self.asked += 1
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _loop() -> PirateLoop:
    # 这一层到处是 `__new__` 造的替身：本组只验分流判据，不碰仓储与日志。
    return PirateLoop.__new__(PirateLoop)


def test_the_real_list_row_does_not_classify_as_a_bounce() -> None:
    """⚠️ **先把病因钉住**：实拍的列表行文字判出来是 `ATTACK`，不是返航。

    这一条不是在验我们的代码「对」，而是在验**那个假设是错的**。
    它要是哪天变绿失败了（游戏真在列表行上放了正文预览），
    详情页那道兜底就可以退休了。
    """
    from evo_helper.vision.parsers import classify_report_subject

    assert classify_report_subject(REAL_ROW_SUBJECT) is ReportKind.ATTACK


def test_the_detail_page_catches_what_the_list_row_missed() -> None:
    """列表行说是战报，正文说是返航 —— 以正文为准。"""
    loop = _loop()
    detail = _Detail()
    routed: list[str] = []
    loop._ingest_protection_bounce = lambda row, page: routed.append("bounce")  # type: ignore[method-assign]

    handled = loop._ingest_non_report_mail(_row(ReportKind.ATTACK), detail)

    assert handled is True, "认出来了就该由这一步吃掉，不许再落到战报解析上"
    assert routed == ["bounce"]


def test_a_row_already_classified_as_a_bounce_still_works() -> None:
    """原来那条路一个字没让 —— 列表行真认出来时照旧走它。"""
    loop = _loop()
    detail = _Detail()
    routed: list[str] = []
    loop._ingest_protection_bounce = lambda row, page: routed.append("bounce")  # type: ignore[method-assign]

    handled = loop._ingest_non_report_mail(_row(ReportKind.PROTECTION_BOUNCE), detail)

    assert handled is True
    assert routed == ["bounce"]
    assert detail.asked == 0, "列表行已经认出来了，不必再读一遍正文"


def test_a_real_battle_report_is_not_stolen() -> None:
    """⚠️ 普通战报**必须**照旧落到战报解析上。

    这一条是这次改动最贵的失效方向：把真战报误判成返航，那一份的战果就没了，
    而它在账上和「读不出来」长得一样。
    """
    loop = _loop()
    detail = _Detail("VS\n我方 400 − 0\n敌方 6290 − 6290\n战斗详情")

    handled = loop._ingest_non_report_mail(_row(ReportKind.ATTACK), detail)

    assert handled is False


def test_an_unreadable_detail_page_falls_through_to_the_report_path() -> None:
    """⚠️ 正文读不出时**不许在这里吞掉**。

    战报那条路读不出会留现场图；在这里悄悄返回 True 才是最坏的 ——
    下一趟它还在信箱里，而我们不知道为什么。
    """
    loop = _loop()

    for body in (RuntimeError("详情页还没铺开"), "", "   ", None, 12345):
        detail = _Detail(body)
        assert loop._ingest_non_report_mail(_row(ReportKind.ATTACK), detail) is False, repr(body)


def test_detection_is_loose_but_reading_stays_strict() -> None:
    """探测读到两个坐标也算「像」，但真正的读取仍会拒收。

    ⚠️ 两件事**刻意分开**：探测放宽是为了不让「拒收一封」变成「整类认不出」；
    读取仍严是因为认错目标会把保护期记到别人头上，那个坐标从此被无故排除。
    """
    from evo_helper.vision.protection_bounce import (
        ProtectionBounceUnreadable,
        read_protection_bounce,
    )

    two = (
        "[4:277:14]（A's Planet）处于保护状态，我方舰队已返航。"
        "[9:250:8]（B's Planet）处于保护状态，我方舰队已返航。"
    )
    loop = _loop()
    detail = _Detail(two)
    routed: list[str] = []
    loop._ingest_protection_bounce = lambda row, page: routed.append("bounce")  # type: ignore[method-assign]

    # 探测：像
    assert loop._ingest_non_report_mail(_row(ReportKind.ATTACK), detail) is True
    assert routed == ["bounce"]

    # 读取：拒收
    class _Full:
        def report_header(self) -> str:
            return "发件人: System\n主题: 攻击报告\n09/09/2026 12:27:13"

        def security_message(self) -> str:
            return two

    try:
        read_protection_bounce(_Full())
    except ProtectionBounceUnreadable as error:
        assert "2" in str(error)
    else:  # pragma: no cover - 走到这里说明拒收判据没了
        raise AssertionError("两个坐标必须拒收，不许猜是哪一个")


def test_the_real_body_reads_all_the_way_through() -> None:
    """端到端：实拍那一封的原文，走现在的读取路径要读出坐标与时刻。"""
    from evo_helper.vision.protection_bounce import read_protection_bounce

    class _Full:
        def report_header(self) -> str:
            # ⚠️ 实拍的页眉：主题是「攻击报告」，不是原有用例里写的「舰队返航」。
            return "发件人: System\n主题: 攻击报告\n09/09/2026 12:27:13"

        def security_message(self) -> str:
            return REAL_BODY

    reading = read_protection_bounce(_Full())

    assert (
        reading.target.galaxy,
        reading.target.system,
        reading.target.position,
    ) == (4, 277, 14)
    assert reading.reported_at_utc == MAIL_AT
    assert reading.raw_time_text == "09/09/2026 12:27:13"
