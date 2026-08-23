"""采集段的重叠自查**接线**：相邻两屏接不上有没有留下痕迹。

判据本身住在 `domain.ranking.screens_overlap`（用例在 `tests/unit/domain/test_ranking.py`），
「这一屏读出了哪些坐标」那把尺子住在 `tools.ranking_scan.coordinates_of`
（用例在 `test_ranking_scan.py`）。这个文件量的是**采集循环有没有真的把它们接上**，
以及接上之后的三条口径：

1. 真断了要在**三个地方**留痕：当场那一行、`record_log("采集一屏")` 的 payload、
   以及收尾那条 `采集重叠断裂`。少任何一处都等于这次改动没做——
   跳过去的那几行压根没被读过，「采到的 bot 数」看起来完全正常。
2. 没断就**一个字都不许多打**。收尾那句是**异常信号**，每趟都打就成了噪声，
   而噪声里的告警等于没有告警。
3. 一屏坐标全读不出时，那一屏和**紧接着的下一屏**都答「不知道」。

## ⚠️⚠️ 账：这道判据原先建在名次上，而名次不可信

第一版是 `rows_skipped(上屏末行名次, 本屏首行名次)`，报「漏掉 N 名」。生产
run `91c7f9ec`（2026-08-23 20:35）整趟只从第 771 名走到第 1343 名（约 570 名），
它却喊了 12 次、累计「漏掉 8922 名」——一次都不是真的。根因是名次列的 OCR 会串出
高位噪声，而那 5 个一模一样的 `996` 就是指纹（`= 1000 − 4`：千位丢了 + 真实重叠
4 行）。用户口径（2026-08-23）：「名次字段可以忽略，我们只需要使用军力进行判断」。

所以判据换到**坐标**上：一次拖动推约 8 行而一屏可见 11–14 行（同一趟 70 屏实测
572/70 = 8.2 行/屏），相邻两屏正常共享 3–6 行；共享行一个都没有才可疑。
下面 `test_ranks_read_wrong_never_produce_an_alert` 就是那次误报的回归用例。

⚠️ 全程不起游戏、不驱动鼠标、不碰任何数据库：驱动、导航、OCR、仓储全是替身，
读到的那几屏是喂进来的清单。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_nav import ScrollOutcome, ScrollStep
from evo_helper.game.ranking_ui import (
    BLIND_SCROLL_MARGIN_ROWS,
    BLIND_SCROLL_ROWS,
    ROWS_PER_SCREEN,
)
from evo_helper.tools import ranking_scan
from evo_helper.tools.ranking_scan import HumanStretch

#: 真人段那一段直接宣布「翻到 bot 区了」时报的行数。
#:
#: ⚠️ **必须比盲滚行数多出一个余量**，否则 `report_bot_area_reached` 会额外打一条
#: 「盲滚余量告急」的告警——那一条有它自己的用例（`test_ranking_blind_scroll_warning.py`），
#: 混进来只会把「一个字都不多打」那条断言弄脏。
ROWS_TO_BOT_AREA = BLIND_SCROLL_ROWS + BLIND_SCROLL_MARGIN_ROWS + 17


#: 一次拖动推进几行。2026-08-23 生产实测 70 屏平均 8.2 行/屏，取 8——
#: 于是相邻两屏共享 5 行，正是实机上那个形状。
ADVANCE_PER_SCROLL = 8


def _bot(index: int) -> Coordinate:
    """榜上第 `index` 个 bot 的坐标。**同一个 index 永远是同一个坐标。**

    这是这一整个文件的地基：判据问的是「相邻两屏有没有共同坐标」，所以
    「哪一行是哪一个 bot」必须是喂进来的事实，而不是每屏随便换一个恒星系。
    （原先的 `_screen(..., system=137)` 就是每屏换一个恒星系——那在新判据下
    等于每一屏都报「重叠断了」，用例会变成红的假证明。）

    位号 5–20：1–4 号位是游戏固定生成的海盗，`is_bot_entry` 会整行剔掉，
    所以一个恒星系装 16 个 bot，装满换下一个。
    """
    return Coordinate(4, 137 + index // 16, 5 + index % 16)


def _screen(
    start: int, *, rows: int = ROWS_PER_SCREEN, ranks: Sequence[int | None] | None = None
) -> list[RankingRow]:
    """从第 `start` 个 bot 起的一屏。名次默认按 `850 + index` 连续给。

    `ranks` 是给那条回归用例开的口子：名次读成什么都不该改变这道判据的答案。

    ⚠️ **军力值全屏取同一个数**（不是逐行递减）。真榜是降序的，而重叠行的军力
    **高于上一屏末行**——跨屏锚点（`domain.ranking.trusted_scores` 的
    `out_of_order`）会把它们整段判成「破坏降序」，于是每一屏都多打一行
    「军力值不可信，丢掉这几行的分数」。那是另一条判据的事（它有自己的用例），
    混进来只会把这个文件里「一个字都不多打」那几条断言弄脏。取同一个数则三道
    军力判据（`renderable_score` / `out_of_order` / 断崖）一句话都不说。
    """
    given = list(ranks) if ranks is not None else [850 + start + offset for offset in range(rows)]
    return [
        RankingRow(
            rank=given[offset],
            name=f"bot_4_{_bot(start + offset).system}_{_bot(start + offset).position}",
            score=29_000.0,
            coordinate=_bot(start + offset),
        )
        for offset in range(rows)
    ]


def _walk(screens: int, *, advance: int = ADVANCE_PER_SCROLL) -> list[list[RankingRow]]:
    """连着 `screens` 屏、每屏推进 `advance` 行的一趟。重叠完好的那种。"""
    return [_screen(index * advance) for index in range(screens)]


class _NoOcr:
    """假 `pytesseract` 模块。

    ⚠️ **塞进 `sys.modules`，不让 `scan()` 去 import 真的那个。**
    `scan()` 开头就写 `pytesseract.pytesseract.tesseract_cmd = Settings().tesseract_path`,
    而那是**进程级**的赋值：改了真模块，同一轮里后面那些真跑 OCR 的用例就跟着
    用上这个假路径了。
    """

    class _Binary:
        tesseract_cmd = ""

    def __init__(self) -> None:
        self.pytesseract = _NoOcr._Binary()


class _Settings:
    """假配置。三个值都是**故意填成不可用的**：这一趟不该碰 OCR，也不该碰库。"""

    tesseract_path = "这一趟一次 OCR 都不做"
    player_name = "Kucleer"
    database_url = "这一趟一次连接都不建"


class _Driver:
    """假 `LiveDriver`。**一次点击、一次移动、一次截图都不发。**

    `capture()` 交个空壳就够：读一屏那条路（`rows_from_image`）整个被替掉了，
    图根本没人看。
    """

    def capture(self) -> object:
        return object()

    def wait(self, _seconds: float) -> None:
        pass


class _Board:
    """一趟采集会读到的那几屏，按顺序发。

    `interrupt_after` 是「发完第几屏之后被掐」——用来验 `finally` 那条路。
    抛 `KeyboardInterrupt` 而不是自定义异常：实机上真正打断这个循环的就是
    Ctrl+C 和调度器抢占，而它们进不了任何 `except`，只走 `finally`。
    """

    def __init__(
        self, screens: Sequence[Sequence[RankingRow]], *, interrupt_after: int | None = None
    ) -> None:
        self.screens = [list(screen) for screen in screens]
        self.handed = 0
        self.interrupt_after = interrupt_after

    def first(self) -> list[RankingRow]:
        """开榜之后 `read_rows()` 读到的那一屏。"""
        self.handed = 1
        return list(self.screens[0])

    def scroll_once(self) -> ScrollStep[RankingRow]:
        if self.handed == self.interrupt_after:
            raise KeyboardInterrupt("用户 Ctrl+C")
        rows = self.screens[self.handed]
        self.handed += 1
        return ScrollStep(outcome=ScrollOutcome.SCROLLED, rows=tuple(rows))


class _Nav:
    """假 `RankingNavigator`。只交行，不动画面。"""

    def __init__(self, board: _Board) -> None:
        self.board = board
        self.opened = 0
        self.closed = 0

    def open_military_ranking(self) -> None:
        self.opened += 1

    def scroll_once(self) -> ScrollStep[RankingRow]:
        return self.board.scroll_once()

    def scroll_blind(self) -> None:
        raise AssertionError("真人段被替掉了，这一趟一屏都不该慢拖")

    def spin_blind(self, *, rows: int) -> None:
        raise AssertionError("真人段被替掉了，这一趟一格都不该拨")

    def close(self) -> bool:
        self.closed += 1
        return True


class _Repository:
    """假仓储。**连引擎都不建**——生产库和测试库一样碰不到。"""

    def __init__(self) -> None:
        self.saved: list[RankingTarget] = []

    def save_ranking_targets(self, targets: Sequence[RankingTarget]) -> None:
        self.saved.extend(targets)


class _Run:
    """一趟采集的全部产出：说了什么、记了什么、存了什么。"""

    def __init__(self, nav: _Nav, *, scrolls: int) -> None:
        self.nav = nav
        self.scrolls = scrolls
        self.said: list[str] = []
        self.logs: list[tuple[str, dict[str, Any]]] = []
        self.repository = _Repository()

    def record(
        self, _level: str, _source: str, message: str, *, payload: dict[str, Any] | None = None
    ) -> None:
        self.logs.append((message, dict(payload or {})))

    def scan(self) -> int:
        return ranking_scan.scan(bot_scrolls=self.scrolls)

    def lines(self) -> list[str]:
        """`say` 出去的那些话，去掉缩进——循环里那几行是缩进过的。"""
        return [line.strip() for line in self.said]

    def payloads(self, message: str) -> list[dict[str, Any]]:
        return [payload for logged, payload in self.logs if logged == message]

    def overlap(self) -> list[bool | None]:
        """每一屏 `record_log("采集一屏")` 里那个 `overlap_intact`。"""
        return [payload["overlap_intact"] for payload in self.payloads("采集一屏")]


def _reached_bots(**kwargs: Any) -> HumanStretch:
    """替掉真人段：直接宣布「翻到 bot 区了」。

    ⚠️ 真跑一遍不是更真实，而是更吵：盲滚和检测那两段会往 `said` 里灌十几行，
    而这几条用例量的就是「有没有多打一行」。那一段有它自己一整个文件的用例
    （`test_ranking_human_stretch.py`）。
    """
    progress = kwargs["progress"]
    progress.stage = ranking_scan.ScanStage.DETECTING
    progress.blind_rows = kwargs["blind_rows"]
    progress.human_rows = ROWS_TO_BOT_AREA
    return HumanStretch(
        reached_bots=True,
        rows=ROWS_TO_BOT_AREA,
        detection_scrolls=0,
        reason="名字列里出现了 bot",
    )


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    screens: Sequence[Sequence[RankingRow]],
    *,
    interrupt_after: int | None = None,
    scrolls: int | None = None,
) -> _Run:
    """把 `scan()` 架空到只剩采集循环，返回还没起跑的那一趟。

    `scrolls` 默认刚好等于要发的屏数，这样循环**自然走完**——不靠离页也不靠
    `DRY_SCREENS` 收尾，那两条各自会往 `said` 里加一行，而「一个字都不多打」
    那条断言不该被它们干扰。
    """
    board = _Board(screens, interrupt_after=interrupt_after)
    nav = _Nav(board)
    run = _Run(nav, scrolls=len(board.screens) - 1 if scrolls is None else scrolls)
    monkeypatch.setitem(sys.modules, "pytesseract", _NoOcr())
    monkeypatch.setattr(ranking_scan, "Settings", _Settings)
    monkeypatch.setattr(ranking_scan, "LiveDriver", _Driver)
    monkeypatch.setattr(ranking_scan, "SlowDragDriver", lambda driver: driver)
    # 松鼠标那一下是真会动鼠标的（`driver._gui.mouseUp()`），从这里整个撤掉。
    monkeypatch.setattr(ranking_scan, "release_stuck_mouse", lambda _driver: None)
    monkeypatch.setattr(ranking_scan, "enter_game_exit_code", lambda *_a, **_k: 0)
    monkeypatch.setattr(ranking_scan, "RankingNavigator", lambda **_kwargs: nav)
    # 三层一起替掉：不建引擎、不开会话、不写库。
    monkeypatch.setattr(ranking_scan, "create_database_engine", lambda _url: None)
    monkeypatch.setattr(ranking_scan, "create_session_factory", lambda _engine: None)
    monkeypatch.setattr(ranking_scan, "SqlAlchemyRepository", lambda _factory: run.repository)
    monkeypatch.setattr(ranking_scan, "rows_from_image", lambda *_a, **_k: board.first())
    monkeypatch.setattr(ranking_scan, "scroll_through_humans", _reached_bots)
    monkeypatch.setattr(ranking_scan, "say", run.said.append)
    monkeypatch.setattr(ranking_scan, "record_system_log", run.record)
    return run


# -- 真断了：三个地方都要留痕 ---------------------------------------------------


def test_a_broken_overlap_is_reported_on_the_spot_and_in_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **这是这道判据要抓的那种静默失败。**

    第三屏从第 40 个 bot 起，而第二屏只到第 20 个——中间第 21–39 个**压根没被
    读过**。采集段只按坐标去重，所以「采到的 bot 数」看起来完全正常，
    `is_bot_entry` 那种事后判据也救不了没读过的行。唯一的痕迹就是下面这三处。

    第二屏是**正常重叠**（从第 8 个起，和第一屏共享 5 行），它答 `True`——把这一屏
    一起放进来是为了钉住「重叠上了就不打那一行」：真断的那次要能从一屏噪声里认出来。
    """
    run = _harness(monkeypatch, [_screen(0), _screen(8), _screen(40)])

    assert run.scan() == 0

    assert run.overlap() == [True, False], "接没接上要进 `采集一屏` 的 payload，事后才查得出"
    assert "⚠️ 与上一屏没有一个共同坐标：重叠可能断了（中间的行没被读过）" in run.lines()
    assert ("采集重叠断裂", {"screens_without_overlap": 1}) in run.logs
    assert (
        "⚠️ 本趟有 1 屏与上一屏没有共同坐标（重叠可能断了；中间的行没被读过，事后判据救不了）"
        in run.lines()
    )


def test_the_broken_screens_add_up_across_the_whole_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """收尾报的是**整趟累计**，不是最后一屏那一个。

    一趟要滚几百屏。只报最后一次的话，中间断过五次、每次跳掉一整段，收尾照样说
    「0 屏」——那正好是这条判据最该说话的一趟。

    ⚠️ 数的是**屏**，不是「漏了几行」：跳过去的行没被读过，「几行」这个数在原理上
    就无从得知。原先那道名次判据敢报「漏掉 8922 名」，正因为它是从两个带噪声的
    名次减出来的。
    """
    run = _harness(monkeypatch, [_screen(0), _screen(40), _screen(200)])

    assert run.scan() == 0

    assert run.overlap() == [False, False]
    assert ("采集重叠断裂", {"screens_without_overlap": 2}) in run.logs


# -- 名次读成什么都不许影响这道判据 ---------------------------------------------


def test_ranks_read_wrong_never_produce_an_alert(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️⚠️ **2026-08-23 那 12 条假告警的回归用例。**

    生产 run `91c7f9ec` 上反复出现的形状：上一屏末行名次 `1008` 的千位被 OCR 吃掉、
    读成 `8`，而本屏首行 `1005` 读得没错。旧判据算 `1005 − 8 − 1 = 996`，于是
    报「与上一屏之间漏掉 996 名（重叠断了）」——那一趟整个只走了约 570 名，
    这一条就号称漏了 996。同一趟里这个数出现了 5 次。

    而这两屏**共享 5 行**，重叠好得很。坐标对上了就是对上了，名次读成什么都
    改不了这个答案，所以这一趟必须一个字都不提「重叠」。
    """
    run = _harness(
        monkeypatch,
        [
            # 末行名次的千位被吃掉：…1006, 1007, 8
            _screen(0, ranks=[1000 + offset for offset in range(ROWS_PER_SCREEN - 1)] + [8]),
            # 本屏首行 1005 读得没错，往下还串出个 `4781`（实机读到过的形状）。
            _screen(
                8, ranks=[1005, 4781] + [1007 + offset for offset in range(ROWS_PER_SCREEN - 2)]
            ),
        ],
    )

    assert run.scan() == 0

    assert run.overlap() == [True]
    assert not [line for line in run.lines() if "重叠" in line or "漏掉" in line]
    assert run.payloads("采集重叠断裂") == []


# -- 没断：一个字都不多打 -------------------------------------------------------


def test_a_run_whose_overlap_never_broke_says_nothing_about_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **收尾那句是异常信号，每趟都打就成了噪声。**

    没断时 `采集重叠断裂` 那条记录和那句话都不许出现。一趟几百屏、一天八趟，
    把它打成常态之后真断的那一次就淹在里面了——而这条判据存在的全部理由
    就是让那一次被看见。

    payload 里的 `overlap_intact` **照旧要记 `True`**：那是「量过了，接上了」，
    和「没量」（`None`）是两件事，日志上必须分得开。
    """
    run = _harness(monkeypatch, _walk(4))

    assert run.scan() == 0

    assert run.overlap() == [True, True, True]
    assert run.payloads("采集重叠断裂") == []
    assert not [line for line in run.lines() if "重叠" in line or "漏掉" in line]


def test_one_shared_row_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **推进一整屏差一行也算接上了**，别按「共享几行」卡门。

    2026-08-23 实机十屏里最狠的一次推进 12 行，而那一屏可见 13 行——重叠只剩 1 行。
    共享行本来就能少到 1 行，按数量卡就等于天天喊狼来了。
    """
    run = _harness(monkeypatch, _walk(3, advance=ROWS_PER_SCREEN - 1))

    assert run.scan() == 0

    assert run.overlap() == [True, True]
    assert not [line for line in run.lines() if "重叠" in line]


# -- 坐标读不出：这一屏和下一屏都答「不知道」 -----------------------------------


def test_a_screen_with_no_readable_coordinate_does_not_poison_the_next_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **`previous_coordinates` 不许写成 `coordinates_of(rows) or previous_coordinates`。**

    那个 `or` 在「这一屏坐标全读不出」时会**保留上一屏的集合**，于是下一屏拿
    **隔了两屏**的坐标去比——而隔两屏本来就不该有共同坐标（一次推约 8 行，
    两次就推出一整屏）。于是每一次读废都要连带造出一条假警报。而假警报比不报
    更坏：它把这条判据教成经常喊狼来了，真断的那次就没人看了。

    这里第二屏名字整列没认出来（实机常态：`rows_from_image` 名字读不出就丢行），
    第三屏正常接在第二屏该在的位置上。按 `or` 那种写法会拿第一屏去比，
    报「重叠断了」——而一行都没跳。

    第四屏钉的是**恢复**：拿到读得出的坐标之后，正常比较立刻接上，
    不会因为中间断过一屏就此永远闭嘴。
    """
    run = _harness(
        monkeypatch,
        [_screen(0), [], _screen(16), _screen(24)],
    )

    assert run.scan() == 0

    assert run.overlap() == [None, None, True], (
        "坐标全读不出的那一屏答 None，紧接着的下一屏也答 None；第四屏恢复正常比较"
    )
    assert not [line for line in run.lines() if "重叠" in line], "一行都没跳，不许报"
    assert run.payloads("采集重叠断裂") == []


def test_an_unreadable_screen_is_never_counted_as_a_broken_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **`None` 不许在 payload 里落成 `False`。**

    落成 `False` 的话，日志上「这一屏量过、接不上」和「这一屏连坐标都没读出来」
    长得一模一样——而后者恰恰是最可疑的那种屏，还会因此白挨一条告警。
    查库的人分不开这两件事，就等于这条判据没记住任何东西。
    """
    run = _harness(monkeypatch, [_screen(0), []])

    assert run.scan() == 0

    # 断 `is None` 而不是 `not ...`：`None` 和 `False` 在真值上一模一样，
    # 而那个键**必须在**（不是「漏记了」）。
    assert "overlap_intact" in run.payloads("采集一屏")[0]
    assert run.payloads("采集一屏")[0]["overlap_intact"] is None
    assert run.payloads("采集重叠断裂") == []


# -- 被打断也要报得出来 ---------------------------------------------------------


def test_an_interrupted_run_still_reports_what_it_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **`screens_without_overlap` 与 `previous_coordinates` 是提到 `try` 之前的，
    这条守的就是那件事。**

    实机上打断这个循环的是 Ctrl+C 和调度器抢占，两者都进不了任何 `except`，
    只走 `finally`。而收尾那条 `采集重叠断裂` 就在 `finally` 里——两个变量要是
    定义在 `try` 里面（或者循环里面），`finally` 读它们会撞 `NameError`：
    一次干净的中断被变成一条堆栈，而**这一趟已经断掉的那一屏连带被吞掉**。

    所以这里既断言异常**原样穿出去**（撞了 `NameError` 的话 `pytest.raises`
    收到的就不是 `KeyboardInterrupt` 了），也断言那条记录照样落下来。
    """
    run = _harness(
        monkeypatch,
        [_screen(0), _screen(40)],  # 第二屏跳过了第 13–39 个 bot
        interrupt_after=2,
        scrolls=2,
    )

    with pytest.raises(KeyboardInterrupt):
        run.scan()

    assert ("采集重叠断裂", {"screens_without_overlap": 1}) in run.logs
    assert run.nav.closed == 1, "面板照样要关，不然下一趟开在一个开着的面板上"
