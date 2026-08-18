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
钉的是**停止条件**：拖到「还是那几行」才算到顶。

## 第二次事故（2026-08-18）：判据被 OCR 噪声打穿

停止条件修好了，比的却是主题 + 时间拼起来的行身份，而主题那一格读不稳：
面板半透明，背后那一页的字透上来落进同一块 ROI，同一封邮件两次读成
`'大 Sw GEF攻击报告 bad'` 与 `'EN SEFATing bad Za once'`。于是「还是那几封」
**永远不成立**，每一趟都走满 40 次上限（一次约 5.8 秒，近四分钟），而用户当场
核对过：**进邮箱本来就在顶部**。生产库里那一句 2026-08-18 一天出现 17 次。

所以这个文件现在钉三件事：

1. 停止判据挂在**时间列**上，主题再怎么糊也不影响（`test_e_*`）；
2. 走满上限时那句话**不许**再声称「看到的不是最新的几封」——它并不知道这件事
   （`test_d_*`）；
3. 「读空」仍然不算到顶（`test_c_*`）——这是从上一次事故继承来的，别在换判据
   的时候弄丢。

还钉一件**没做**的事（`test_b_*`）：没有任何「刚登录 / 单子对得上就当在顶部」
的近路。判错方向的代价不对称——误判「已在顶部」是漏战报（两天排障），
误判「不在顶部」只是多花几秒。
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
    mail_times_settled,
)
from evo_helper.vision.parsers import ReportKind

NOW = datetime(2026, 8, 13, 21, 30, tzinfo=UTC)


def _rows(offset: int, *, subject: str = "侦察报告") -> list[MailRow]:
    """「列表滚到第 `offset` 行」时看得到的六行。`offset == 0` 就是顶部。"""
    return [
        MailRow(
            index=index,
            subject=subject,
            raw_time_text=(NOW - timedelta(minutes=offset + index)).strftime("%d/%m/%Y %H:%M:%S"),
            reported_at_utc=NOW - timedelta(minutes=offset + index),
            kind=ReportKind.SCOUT,
        )
        for index in range(6)
    ]


#: 实机上同一封邮件连着两次读成的样子（2026-08-18 20:33 的日志原文）。
#: 面板是半透明的，背后那一页的字透上来落进同一块 ROI。
NOISY_SUBJECTS = [
    "大 Sw GEF攻击报告 bad",
    "EN SEFATing bad Za once",
    "4d [全SGEF攻击报告 bas",
    "一一 bad . Za ORs",
]


class _List:
    """一个记得自己滚到哪的假邮件列表。往上拖会夹在第 0 行。"""

    def __init__(self, *, start: int, rows_per_drag: int = 5, noisy_subjects: bool = False) -> None:
        self.offset = start
        self._step = rows_per_drag
        self._noisy = noisy_subjects
        self.drags = 0
        self.reads = 0

    def read(self) -> list[MailRow]:
        subject = "侦察报告"
        if self._noisy:
            subject = NOISY_SUBJECTS[self.reads % len(NOISY_SUBJECTS)]
        self.reads += 1
        return _rows(self.offset, subject=subject)

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


def test_b_it_never_takes_a_shortcut_and_always_verifies_by_dragging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**没有正面证据就照常拖**，哪怕这一屏看起来完全像顶部。

    用户 2026-08-18 提的是「重新登录后信箱默认就在顶部，别一直往上翻」。
    对，但「刚登录过」是**没有反证**，不是证据；库里能拿到的两个候选
    （最新战报时刻 / 在等的那几发的期望时刻）都被生产数据否掉了，
    理由整段写在 `_scroll_mail_list_to_top` 的 docstring 上。

    所以这里钉的是**下界**：一次都不拖是不允许的。真在顶部时代价是
    「两屏一拖」≈12 秒，而误判在顶部的代价是漏战报——2026-08-13 那次
    「17 发攻击 0 份战报」正是这个形态。
    """
    listing = _List(start=0)
    loop = _wire(monkeypatch, listing)

    loop._scroll_mail_list_to_top()

    # 已经在顶上：也要拖一次亲眼确认它不动，但**只拖一次**——上限是兜底，不是节奏。
    assert listing.drags == 1
    assert listing.offset == 0


def test_the_stop_is_the_rows_not_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """一次拖动能走几行**没有标定过**，所以停止条件只能是「还是那几行」。

    这里把步距调成 1 行——写死次数的那版会停在第 97 行，而按判据停的这版
    照样回到第 0 行，只是多拖了几次。
    """
    listing = _List(start=20, rows_per_drag=1)
    loop = _wire(monkeypatch, listing)

    loop._scroll_mail_list_to_top()

    assert listing.offset == 0
    assert listing.drags == 21  # 20 次把它拖上去，第 21 次确认拖不动了


def test_e_subject_noise_no_longer_defeats_the_stop_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**这条就是 2026-08-18 那一整天的正主。**

    主题每读一次都不一样（`NOISY_SUBJECTS` 是实机日志原文），而时间列稳。
    比主题 + 时间拼起来的行身份的那版在这里必然走满 40 次上限；
    比时间列的这版照旧一次就停。
    """
    listing = _List(start=0, noisy_subjects=True)
    loop = _wire(monkeypatch, listing)

    loop._scroll_mail_list_to_top()

    assert listing.drags == 1


def test_e_one_garbled_time_cell_does_not_restart_the_dragging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """六行里糊掉一格时间**不该**让「拖不动了」失效——判据是严格多数，不是逐字相等。

    时间那一格在实拍上是 93.5% 读得出（186 行里 174 行），也就是说每读几屏就会
    有一格糊掉。逐字相等的写法会被这种抖动推回「还能拖」，回到 2026-08-18
    那个「永远不成立」的老毛病，只是发作得稀一点、更难查。
    """
    listing = _List(start=0)

    def _read() -> list[MailRow]:
        rows = listing.read()
        # 每次读都换一格糊掉：位置和内容都不重样，逐字相等永远不成立。
        index = listing.reads % 6
        rows[index] = MailRow(
            index=index,
            subject=rows[index].subject,
            raw_time_text=None,
            reported_at_utc=None,
            kind=rows[index].kind,
        )
        return rows

    loop = _wire(monkeypatch, listing)
    loop._mail_list_rows = _read  # type: ignore[assignment, method-assign]

    loop._scroll_mail_list_to_top()

    assert listing.drags == 1


def test_d_giving_up_describes_the_evidence_instead_of_claiming_it_is_not_the_top(
    monkeypatch: pytest.MonkeyPatch, _capture: list[str]
) -> None:
    """走满上限时**只说能证明的**：拖了几次、第 0 行是什么时候的、判据是什么。

    ⚠️ 原先打的是「这一趟看到的**不是信箱最新的几封**」。2026-08-18 走满上限的
    那 17 趟里，用户当场核对过：**进邮箱本来就在顶部**，看到的正是最新的几封。
    那句话把「判不出来」说成了一个它并不知道的事实，而仓库口径是
    **日志说假话比不说更糟**（CLAUDE.md，`_say_still_waiting` 那次教训）。

    证据还要**落库**：实机在另一台机器上，本地 `var/logs` 取不到。
    """
    records: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        pirate_loop,
        "record_system_log",
        lambda level, source, message, *, payload=None: records.append(
            (level, message, dict(payload or {}))
        ),
    )
    listing = _List(start=10_000)
    seen: list[list[MailRow]] = []

    def _read() -> list[MailRow]:
        seen.append(listing.read())
        return seen[-1]

    loop = _wire(monkeypatch, listing)
    loop._mail_list_rows = _read  # type: ignore[assignment, method-assign]

    loop._scroll_mail_list_to_top()

    # ⚠️ 上界写成字面量，**不写 `MAIL_SCROLL_TO_TOP_MAX_DRAGS`**：拿被守的常量
    # 当自己的尺子，把上限改成 400（一趟 20 分钟）这条也照样绿。60 次 ≈ 280 行、
    # 约三分钟，是「还能忍」的那一档。
    assert 0 < listing.drags <= 60
    # 而下界也要有：拖不到一趟对账往下沉的那么多行，等于没修
    # （`RECONCILE_MAX_PAGES` = 8 屏，每屏一次拖动）。
    assert MAIL_SCROLL_TO_TOP_MAX_DRAGS > RECONCILE_MAX_PAGES * 2

    spoken = "\n".join(_capture)
    # 不许再声称「看到的不是最新的几封」——它没有任何证据支持这句。
    assert "不是信箱最新的几封" not in spoken
    assert "仍没到顶" not in spoken
    # 要说的是证据：**最后那一屏**第 0 行的时间，以及「判不出来」这件事本身。
    assert "判不出来" in spoken
    last_seen = seen[-1][0].raw_time_text
    assert last_seen is not None
    assert last_seen in spoken

    assert [level for level, _message, _payload in records] == ["WARNING"]
    _level, message, payload = records[0]
    assert "不是" not in message
    assert payload["max_drags"] == MAIL_SCROLL_TO_TOP_MAX_DRAGS
    assert payload["first_row_time"] == last_seen
    assert payload["readable_times"] == 6
    assert len(payload["row_times"]) == 6


def test_c_an_unreadable_screen_is_not_mistaken_for_the_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一屏行都读不出来（空清单）**不算**到顶——那是 OCR 没读出来，照拖不误。

    两次空清单相等，按「和上一次一样就停」的朴素写法会当场停在半路，
    而那正好是列表深到看不清时最容易发生的情形。

    ⚠️ 换判据（行身份 → 时间列）时**别把这条弄丢**：它是上一次事故留下来的。
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


def test_c_the_settled_criterion_needs_a_strict_majority_of_the_whole_screen() -> None:
    """判据本身的边界：分母是**总行数**，不是「读出来的行数」。

    分母写成「读出来的行数」时，只有一行读得出的那种烂屏会直接判成「没动」——
    而那正是列表深到看不清、最需要接着拖的时候。
    """
    six = ["a", "b", "c", "d", "e", "f"]

    assert mail_times_settled(six, six) is True
    # 六行里四行相同 = 严格多数，算没动（允许 OCR 抖掉两格）。
    assert mail_times_settled(six, ["a", "b", "c", "d", "x", "y"]) is True
    # 正好一半不算：拖动一次走 4.6 行，真到顶时六行会一起对上，不会卡在三比三。
    assert mail_times_settled(six, ["a", "b", "c", "x", "y", "z"]) is False
    # 只有一行读得出、而且对上了——分母是 6，过不了半数，照拖不误。
    assert mail_times_settled(six, ["a", None, None, None, None, None]) is False
    # 两屏都读空：`None == None` 不是证据。
    assert mail_times_settled([None] * 6, [None] * 6) is False
    assert mail_times_settled([], []) is False
    # 第一屏没有「上一屏」可比。
    assert mail_times_settled(None, six) is False
