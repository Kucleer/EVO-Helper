"""进信箱之后要拖回**真正的**顶部，而「拖几次算到顶」不能猜。

事故（2026-08-13 夜，UTC 19:51–23:01）：bot 派了 17 发 BBB 攻击，
`battle_reports` **一行都没有**。日志尾部的模式统计里「战报到点了却没翻到」
30 次——单子是对的、到期判断也对，就是在信箱里找不到。

根因是这一段算术：

- 一次慢拖走 `PANEL_DRAG_FROM_Y - PANEL_DRAG_TO_Y` = 400px，行距 86px，
  也就是约 4.6 行；原先无条件拖 3 次 ≈ **14 行**。
- 而一趟开工对账要往下翻 `RECONCILE_MAX_PAGES` = 8 屏 ≈ **32 行**。
- 列表**会记住上次滚到哪**（`_enter_mailbox` 的注释早就写着这条）。

于是每一趟净往下沉约 18 行，而信箱有 600 封（用户实测），永远沉不到底。

现场图 `var/logs/dump-mail-detail-unrendered-053043.png`（本地 05:30 = **21:30
UTC**）拍到的就是结果：列表最上面几行是 16:42–17:02 的侦察报告，**比墙钟旧四个
半钟头**；而同一张图上二级角标写着「战斗 10」未读，正好等于那时已经落地的
10 发攻击（19:51、19:52、19:54、20:12、20:14、20:53、20:54、21:00、21:02、21:10）。
战报就躺在列表顶上，扫描窗口停在四个半小时之前，七趟信箱一次都没够到过。

所以这里钉的不是「拖了几次」——原来那版拖的次数是写死的，数次数的测试照样绿。
钉的是**停止条件**：拖到「还是那几封」才算到顶。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.tools import pirate_loop
from evo_helper.tools.pirate_loop import (
    MAIL_SCROLL_TO_TOP_MAX_DRAGS,
    RECONCILE_MAX_PAGES,
    LoopOptions,
    MailRow,
    PirateLoop,
)
from evo_helper.vision.parsers import ReportKind

NOW = datetime(2026, 8, 13, 21, 30, tzinfo=UTC)


def _rows(offset: int) -> list[MailRow]:
    """「列表滚到第 `offset` 行」时看得到的六行。`offset == 0` 就是顶部。"""
    return [
        MailRow(
            index=index,
            subject="侦察报告",
            raw_time_text=(NOW - timedelta(minutes=offset + index)).strftime("%d/%m/%Y %H:%M:%S"),
            reported_at_utc=NOW - timedelta(minutes=offset + index),
            kind=ReportKind.SCOUT,
        )
        for index in range(6)
    ]


class _List:
    """一个记得自己滚到哪的假邮件列表。往上拖会夹在第 0 行。"""

    def __init__(self, *, start: int, rows_per_drag: int = 5) -> None:
        self.offset = start
        self._step = rows_per_drag
        self.drags = 0
        self.reads = 0

    def read(self) -> list[MailRow]:
        self.reads += 1
        return _rows(self.offset)

    def drag_up(self) -> None:
        self.drags += 1
        self.offset = max(0, self.offset - self._step)


@pytest.fixture(autouse=True)
def _capture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把 `say` 收进一个清单。日志是这条链路唯一的现场，所以它也是判据之一。"""
    said: list[str] = []
    monkeypatch.setattr(pirate_loop, "say", said.append)
    return said


def _wire(monkeypatch: pytest.MonkeyPatch, listing: _List) -> Any:
    """一个只装了「读一屏 + 往上拖」的 `PirateLoop`。慢拖换成假列表的滚动。"""
    monkeypatch.setattr(pirate_loop, "slow_drag", lambda *args, **kwargs: listing.drag_up())
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=True, attack=True)
    loop._driver = object()
    loop._mail_list_rows = listing.read  # type: ignore[assignment, method-assign]
    return loop


def test_a_list_left_deep_is_dragged_all_the_way_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """**这条就是那 17 发的正主。**

    上一趟把列表留在第 100 行（一夜下来只会更深）。写死 3 次的那版拖完停在
    第 85 行，往下扫 48 行也就是 85–133 行——今天的战报在第 0 行附近，
    永远够不到。
    """
    listing = _List(start=100)
    loop = _wire(monkeypatch, listing)

    loop._scroll_mail_list_to_top()

    assert listing.offset == 0


def test_it_stops_as_soon_as_the_list_stops_moving(monkeypatch: pytest.MonkeyPatch) -> None:
    """到顶就停，不把上限拖满——上限是兜底，不是节奏。

    每次拖之前要读一屏（≈1–2 秒），拖满 40 次是两分钟；本来就在顶上的那种
    常见情形不该付这个钱。
    """
    listing = _List(start=0)
    loop = _wire(monkeypatch, listing)

    loop._scroll_mail_list_to_top()

    # 已经在顶上：拖一次（确认它不动）就够了。
    assert listing.drags == 1
    assert listing.offset == 0


def test_the_stop_is_the_rows_not_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """一次拖动能走几行**没有标定过**，所以停止条件只能是「还是那几封」。

    这里把步距调成 1 行——写死次数的那版会停在第 97 行，而按判据停的这版
    照样回到第 0 行，只是多拖了几次。
    """
    listing = _List(start=20, rows_per_drag=1)
    loop = _wire(monkeypatch, listing)

    loop._scroll_mail_list_to_top()

    assert listing.offset == 0
    assert listing.drags == 21  # 20 次把它拖上去，第 21 次确认拖不动了


def test_it_always_stops_even_when_the_list_never_settles(
    monkeypatch: pytest.MonkeyPatch, _capture: list[str]
) -> None:
    """列表比上限还深（或者主题一直读不出来）时必须停下来，并且**说出来**。

    不说的话，这一趟看到的「最上面几行」不是信箱最上面几行，而收不到战报
    是必然的——2026-08-13 那一夜整整七趟都是这么静悄悄过去的。
    """
    listing = _List(start=10_000)
    loop = _wire(monkeypatch, listing)

    loop._scroll_mail_list_to_top()

    # ⚠️ 上界写成字面量，**不写 `MAIL_SCROLL_TO_TOP_MAX_DRAGS`**：拿被守的常量
    # 当自己的尺子，把上限改成 400（一趟 20 分钟）这条也照样绿。60 次 ≈ 280 行、
    # 约三分钟，是「还能忍」的那一档。
    assert 0 < listing.drags <= 60
    assert any("仍没到顶" in line for line in _capture)
    # 而下界也要有：拖不到一趟对账往下沉的那么多行，等于没修
    # （`RECONCILE_MAX_PAGES` = 8 屏，每屏一次拖动）。
    assert MAIL_SCROLL_TO_TOP_MAX_DRAGS > RECONCILE_MAX_PAGES * 2


def test_an_unreadable_screen_is_not_mistaken_for_the_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一屏行都读不出来（空清单）**不算**到顶——那是 OCR 没读出来，照拖不误。

    两次空清单相等，按「和上一次一样就停」的朴素写法会当场停在半路，
    而那正好是列表深到看不清时最容易发生的情形。
    """
    listing = _List(start=30)
    blanks = [0, 1]

    def _read() -> list[MailRow]:
        if blanks:
            blanks.pop()
            listing.reads += 1
            return []
        return listing.read()

    loop = _wire(monkeypatch, listing)
    loop._mail_list_rows = _read  # type: ignore[assignment, method-assign]

    loop._scroll_mail_list_to_top()

    assert listing.offset == 0
