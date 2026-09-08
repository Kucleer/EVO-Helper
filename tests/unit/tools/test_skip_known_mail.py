"""开封之前按时刻跳过已入库的邮件 —— 五个前提，缺一不可。

## 在省什么

每趟进信箱都把每封战报点开、读完、解析完，才发现「库里已有，不重复入库」。
实测**每两封就有一封是这样白开的**（2026-09-07 全天 513 封里 253 封；
2026-09-08 换完回顶之后 39 封里 19 封 —— 比例没变，它是另一条链路），
一封约 20 秒。

⚠️ 顺带抢回的是**开封预算**：`MAIL_MAX_OPENS` = 8，而实机每趟都撞
「开了 8 封到上限」。那 8 封里有 4 封花在已经读过的邮件上，真正要读的那几封
被挤到下一趟。

## ⚠️⚠️ 为什么这一件是六件里最危险的

它动的是**战报入库那条路**，而那条路上有一颗记录在案的雷
（`_ingest_report_row` 的注释：2026-08-11 那四发 —— 报告确实在库里，
**却没接到该接的那一发派遣上**，于是「库里已有 ⇒ 往下都读过了」这个假定
把它们永久钉在「待战报」）。

所以这份用例是**按前提逐条钉**的，不是按功能钉的。四轮 review 把前提逼到五条
（方案 `docs/任务耗时优化/方案.md` §3.7）：

1. 只对攻击战报生效
2. 时刻读不出的照旧开
3. 库里该秒已有份数 >= 这一屏该秒的行数才跳
4. 补录那一档不跳
5. 早停仍然走 `_stop_after_known()` 本身
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from evo_helper.tools import pirate_loop
from evo_helper.tools.pirate_loop import LoopOptions, MailRow, PirateLoop
from evo_helper.vision.parsers import ReportKind

#: 一屏六行的时刻，倒序（信箱就是这么排的）。
TIMES = [
    "08/09/2026 09:00:00",
    "08/09/2026 08:50:00",
    "08/09/2026 08:40:00",
    "08/09/2026 08:30:00",
    "08/09/2026 08:20:00",
    "08/09/2026 08:10:00",
]


def _row(index: int, *, text: str | None, kind: ReportKind = ReportKind.PIRATE) -> MailRow:
    when = None
    if text is not None:
        when = datetime.strptime(text, "%d/%m/%Y %H:%M:%S").replace(tzinfo=UTC)
    return MailRow(
        index=index,
        subject="攻击报告",
        raw_time_text=text,
        reported_at_utc=when,
        kind=kind,
    )


@dataclass
class _Library:
    """库里每个时刻有几份。`asked` 记下被问过哪些时刻。"""

    counts: dict[str, int]
    asked: list[datetime]

    def count_reports_at(self, moment: datetime) -> int:
        self.asked.append(moment)
        key = moment.strftime("%d/%m/%Y %H:%M:%S")
        return self.counts.get(key, 0)


def _loop(library: _Library, *, outstanding: int = 0) -> Any:
    """一个只装了「查库 + 问单子」的 `PirateLoop`。

    ⚠️ `_ensure_run` 打桩成直接交回假仓储：真的那个会**建一行 mission_run**，
    而这一层只是读。实机上扫信箱时 run 早就存在（`_ensure_run` 有缓存），
    所以打桩没有掩盖真实开销。
    """
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=True, attack=True)
    loop._ensure_run = lambda: (library, None)  # type: ignore[assignment, method-assign]
    # ⚠️ 假派遣要带 `.target`：`_stop_after_known` 会把单子上的目标写进日志
    # （`_targets_note`），拿裸字符串顶替会在那一步炸 —— 而那一步正是这条路
    # 唯一的现场。
    pending = [SimpleNamespace(target=f"4:100:{index}") for index in range(outstanding)]
    loop._due_dispatches = lambda _now: list(pending)  # type: ignore[assignment, method-assign]
    return loop


# -- 前提① 只对攻击战报生效 ---------------------------------------------------


def test_only_battle_reports_are_ever_skipped() -> None:
    """⚠️⚠️ **安全告警与保护期返航不许被顺带跳掉。**

    同一趟扫的是三类邮件（`wanted=(RECONCILE_KIND, *NON_REPORT_MAIL_KINDS)`）。
    它们跟战报的时刻撞车时被跳掉的后果是**丢保护期记录或丢告警** ——
    而那两件都不会报错，只会让同一个目标被反复打、反复弹回来。

    查过历史：这两类**一封都没入库过**（`planet_scout_alerts` 0 行、
    「撞保护期」这四个字在全部日志里没出现过），所以不做分流、只加一个 `is`
    判断，**未知类型一律落在「照开」那一侧**。
    """
    library = _Library(counts={TIMES[0]: 9}, asked=[])
    loop = _loop(library)

    for kind in (ReportKind.PLANET_SCOUTED, ReportKind.PROTECTION_BOUNCE):
        row = _row(0, text=TIMES[0], kind=kind)
        assert loop._already_in_library(row, TIMES) is False, kind

    # 而且**一次库都不该查** —— 类型闸在前，省下的不只是判断
    assert library.asked == []


def test_the_battle_report_itself_is_skipped() -> None:
    """同样那一秒、同样库里 9 份，攻击战报就该跳。"""
    library = _Library(counts={TIMES[0]: 9}, asked=[])
    loop = _loop(library)

    assert loop._already_in_library(_row(0, text=TIMES[0]), TIMES) is True


# -- 前提② 时刻读不出的照旧开 -------------------------------------------------


def test_an_unreadable_time_is_always_opened() -> None:
    """⚠️ **时刻读不出就照开。这不是保守，是判据的一部分。**

    时间那一格实拍上 93.5% 读得出（186 行里 174 行），读不出的那几行**正是
    最该看一眼的**——而拿「读不出」当「跳过」的依据会把它们永久埋掉。
    """
    library = _Library(counts={}, asked=[])
    loop = _loop(library)

    assert loop._already_in_library(_row(0, text=None), TIMES) is False
    assert library.asked == [], "读不出时刻还去查库，等于拿 None 当键"


# -- 前提③ 比同屏同秒行数，不是「有没有」 -------------------------------------


def test_one_stored_report_does_not_skip_two_rows_at_the_same_second() -> None:
    """⚠️⚠️ **本文件最重要的一条。** 同秒两行、库里一份 → 两行都要开。

    2026-08-25 20:18:44 那一秒真有两份战报（`2:490:16` 与 `1:439:11`，
    两个不同目标），同一趟里隔 25 秒先后入库。第二封被评估时库里已经有一份 ——
    **「库里有就跳」会让它这一趟跳过、下一趟同样跳过**，那一发派遣永远停在
    「待战报」。

    而第一轮 review 提的「比待补派遣数」的保守版也救不了它：那一刻该秒已有
    1 份、待补 1 发，`1 >= 1` 成立，照跳（2026-08-26 的日志实证）。

    所以比的是**信箱这一屏同一秒有几行**。
    """
    same_second = [TIMES[0], TIMES[0], *TIMES[2:]]
    library = _Library(counts={TIMES[0]: 1}, asked=[])
    loop = _loop(library)

    for index in (0, 1):
        row = _row(index, text=TIMES[0])
        assert loop._already_in_library(row, same_second) is False, index


def test_two_stored_reports_do_skip_two_rows_at_the_same_second() -> None:
    """反过来：库里两份盖住了眼前这两行，就该跳。

    这一条和上一条一起，钉的是「比的是份数够不够，不是有没有」——
    只留上一条的话，一个「永远返回 False」的实现也能绿。
    """
    same_second = [TIMES[0], TIMES[0], *TIMES[2:]]
    library = _Library(counts={TIMES[0]: 2}, asked=[])
    loop = _loop(library)

    assert loop._already_in_library(_row(0, text=TIMES[0]), same_second) is True


def test_a_row_missing_from_this_screen_still_needs_one_stored_report() -> None:
    """⚠️ 这一屏读不到自己那一行时（时刻列糊了半屏），下界仍是 1 份。

    `max(on_screen, 1)`：算出来 0 行的话「库里 0 份 >= 0 行」会成立，
    于是**库里什么都没有也跳过** —— 那是最坏的失效形态，静默丢报告。
    """
    library = _Library(counts={}, asked=[])
    loop = _loop(library)

    # 这一行的时刻不在这一屏的时刻列里（模拟半屏糊掉）
    assert loop._already_in_library(_row(0, text=TIMES[0]), [None] * 6) is False


# -- 前提⑤ 早停仍然走 `_stop_after_known()` -----------------------------------


def test_skipping_still_asks_the_worklist_before_stopping() -> None:
    """⚠️⚠️ **跳过之后该不该收工，必须问同一个判据。**

    「这一封库里已有」和「把它开出来读到 KNOWN」是同一件事，所以后续行为
    也得一样：`_stop_after_known()` 每封重查待补清单、**清单空了才停**。

    单子上还有欠账 → 不许停。这一条正是 2026-08-11 那四发的补丁：
    报告在库里却没认领上时，早停会把它们永久钉在「待战报」。
    """
    loop = _loop(_Library(counts={}, asked=[]), outstanding=3)
    said: list[str] = []
    pirate_loop.say = said.append  # type: ignore[assignment]

    assert loop._stop_after_known() is False
    assert any("还有 3 发" in line for line in said)


def test_an_empty_worklist_lets_the_skip_stop_the_opening() -> None:
    """单子空了 → 可以收工。两条一起才说明「问了单子」而不是「写死不停」。"""
    loop = _loop(_Library(counts={}, asked=[]), outstanding=0)
    pirate_loop.say = lambda _line: None  # type: ignore[assignment]

    assert loop._stop_after_known() is True


# -- 前提④ 补录那一档不跳（接线，不是判据） -----------------------------------


def test_only_the_daily_path_turns_the_skip_on() -> None:
    """⚠️ **补录那一档刻意不开跳过。**

    那个入口存在的理由正是要够到被各种闸筛掉的邮件（`exhaustive` 那一档
    连早停都不走）。在它上面开跳过，等于把「人手动来救」这条最后的路也堵掉。

    这一条读源码而不是跑流程：接线错了跑流程也看不出来（两个入口的
    `visit` 不同，而跳过发生在 `visit` 之前）。
    """
    from pathlib import Path

    source = Path(pirate_loop.__file__).read_text(encoding="utf-8")
    assert source.count("skip_known=True") == 1, "开跳过的入口不该多于一个"

    # 那一处必须落在 `_scan_for_reconcile` 里，而不是 `backfill_reports` 里
    head, _sep, tail = source.partition("def _scan_for_reconcile")
    assert "skip_known=True" not in head, "跳过被接到了 `_scan_for_reconcile` 之前的入口上"
    assert "skip_known=True" in tail


@pytest.fixture(autouse=True)
def _restore_say() -> Any:
    """`say` 是模块级函数，改了要还原 —— 否则后面的文件跟着受影响。"""
    original = pirate_loop.say
    yield
    pirate_loop.say = original  # type: ignore[assignment]
