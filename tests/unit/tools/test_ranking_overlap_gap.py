"""采集段的重叠自查**接线**：漏掉的名次有没有留下痕迹。

判据本身住在 `domain.ranking.rows_skipped`（用例在 `tests/unit/domain/test_ranking.py`），
头尾名次那两把尺子住在 `tools.ranking_scan`（用例在 `test_ranking_scan.py`）。
这个文件量的是**采集循环有没有真的把它们接上**，以及接上之后的三条口径：

1. 真断了要在**三个地方**留痕：当场那一行、`record_log("采集一屏")` 的 payload、
   以及收尾那条 `采集重叠断裂`。少任何一处都等于这次改动没做——
   跳过去的那几名压根没被读过，「采到的 bot 数」看起来完全正常。
2. 没断就**一个字都不许多打**。收尾那句是**异常信号**，每趟都打就成了噪声，
   而噪声里的告警等于没有告警。
3. 一屏名次全读不出时，那一屏和**紧接着的下一屏**都答「不知道」。

账在这里：采集段原先没有这道校验，靠的是「一次拖动推进得比一屏少」这个从未被
校验过的隐含前提。2026-08-23 实机十屏实测推进 `+8 +8 +7 +10 +8 +8 +4 +12 +8`
——**4 到 12 行**，而可见 13–14 行。推进 12 那一屏重叠只剩 1–2 行。

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
from evo_helper.game.ranking_ui import BLIND_SCROLL_MARGIN_ROWS, BLIND_SCROLL_ROWS
from evo_helper.tools import ranking_scan
from evo_helper.tools.ranking_scan import HumanStretch

#: 真人段那一段直接宣布「翻到 bot 区了」时报的行数。
#:
#: ⚠️ **必须比盲滚行数多出一个余量**，否则 `report_bot_area_reached` 会额外打一条
#: 「盲滚余量告急」的告警——那一条有它自己的用例（`test_ranking_blind_scroll_warning.py`），
#: 混进来只会把「一个字都不多打」那条断言弄脏。
ROWS_TO_BOT_AREA = BLIND_SCROLL_ROWS + BLIND_SCROLL_MARGIN_ROWS + 17


def _screen(ranks: Sequence[int | None], *, system: int) -> list[RankingRow]:
    """一屏 bot 行：名次照 `ranks` 给，坐标按**每屏换一个恒星系**排。

    换恒星系是为了让每一屏都有新 bot：`take_batch_targets` 按坐标去重，全屏都是
    见过的坐标时 `fresh` 为空、`dry` 开始累加，到 `DRY_SCREENS` 就提前收工——
    那会让循环在断言点之前就停下来，用例变成绿的假证明。

    军力值取 10 的整数倍且逐行递减：`renderable_score` 和 `descending_breaks`
    这两道网在这几条用例里一句话都不该说（它们各有自己的用例），说了就会多出
    一行「军力值破坏降序」，而这里量的恰恰是「有没有多打一行」。

    位号从 5 起：1–4 号位是游戏固定生成的海盗，`is_bot_entry` 会整行剔掉。
    """
    rows: list[RankingRow] = []
    for index, rank in enumerate(ranks):
        position = 5 + index
        rows.append(
            RankingRow(
                rank=rank,
                name=f"bot_4_{system}_{position}",
                score=29_000.0 - index * 10,
                coordinate=Coordinate(4, system, position),
            )
        )
    return rows


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

    def skipped(self) -> list[int | None]:
        """每一屏 `record_log("采集一屏")` 里那个 `rows_skipped`。"""
        return [payload["rows_skipped"] for payload in self.payloads("采集一屏")]


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
    """⚠️⚠️ **这是这次改动要抓的那种静默失败。**

    第三屏首行是 860，而第二屏末行是 855——中间 856–859 那四名**压根没被读过**。
    采集段只按坐标去重，所以「采到的 bot 数」看起来完全正常，`is_bot_entry` 那种
    事后判据也救不了没读过的行。这四名唯一的痕迹就是下面这三处。

    第二屏是**正常重叠**（首行 852 回头落在第一屏里），它答 0——把这一屏一起放进来
    是为了钉住「0 不打那一行」：真断的那次要能从一屏噪声里认出来。
    """
    run = _harness(
        monkeypatch,
        [
            _screen([850, 851, 852, 853], system=137),
            _screen([852, 853, 854, 855], system=138),
            _screen([860, 861, 862, 863], system=139),
        ],
    )

    assert run.scan() == 0

    assert run.skipped() == [0, 4], "漏了几名要进 `采集一屏` 的 payload，事后才查得出"
    assert "⚠️ 与上一屏之间漏掉 4 名（重叠断了）" in run.lines()
    assert ("采集重叠断裂", {"rows_missed": 4}) in run.logs
    assert "⚠️ 本趟重叠断了，累计漏掉 4 名（没被读过，事后判据救不了）" in run.lines()


def test_the_gaps_add_up_across_the_whole_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """收尾报的是**整趟累计**，不是最后一屏那一个。

    一趟要滚几百屏。只报最后一次的话，中间断过五次、每次漏十名，收尾照样说
    「漏掉 0 名」——那正好是这条判据最该说话的一趟。
    """
    run = _harness(
        monkeypatch,
        [
            _screen([850, 851], system=137),
            _screen([856, 857], system=138),  # 漏 4（852–855）
            _screen([860, 861], system=139),  # 漏 2（858–859）
        ],
    )

    assert run.scan() == 0

    assert run.skipped() == [4, 2]
    assert ("采集重叠断裂", {"rows_missed": 6}) in run.logs


# -- 没断：一个字都不多打 -------------------------------------------------------


def test_a_run_whose_overlap_never_broke_says_nothing_about_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **收尾那句是异常信号，每趟都打就成了噪声。**

    `missed` 为 0 时 `采集重叠断裂` 那条记录和那句话都不许出现。一趟几百屏、
    一天八趟，把它打成常态之后真断的那一次就淹在里面了——而这条判据存在的
    全部理由就是让那一次被看见。

    payload 里的 `rows_skipped` **照旧要记 0**：那是「量过了，没漏」，
    和「没量」（`None`）是两件事，日志上必须分得开。
    """
    run = _harness(
        monkeypatch,
        [
            _screen([850, 851, 852, 853], system=137),
            _screen([852, 853, 854, 855], system=138),
            _screen([856, 857, 858, 859], system=139),  # 紧接着 855，连续
        ],
    )

    assert run.scan() == 0

    assert run.skipped() == [0, 0]
    assert run.payloads("采集重叠断裂") == []
    assert not [line for line in run.lines() if "漏掉" in line or "重叠断" in line]


# -- 名次读不出：这一屏和下一屏都答「不知道」 -----------------------------------


def test_a_screen_with_no_readable_rank_does_not_poison_the_next_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **`previous_last_rank` 不许写成 `last_rank_of(rows) or previous_last_rank`。**

    那个 `or` 在「这一屏名次全读不出」时会**保留上一屏的值**，于是下一屏拿
    **隔了两屏**的名次去比，凭空报出一个 8–16 名的假漏采。而假警报比不报更坏：
    它把这条判据教成「经常喊狼来了」，真断的那次就没人看了。

    这里第二屏名次全读不出（OCR 整列没认出来是实机常态），第三屏首行 870。
    按 `or` 那种写法会拿 853 去比，报「漏掉 16 名」——而真实推进只有四行，
    一名都没漏。读不出就该是 `None`，让下一次比较答「不知道」。

    第四屏钉的是**恢复**：拿到读得出的名次之后，正常比较立刻接上，
    不会因为中间断过一屏就此永远闭嘴。
    """
    run = _harness(
        monkeypatch,
        [
            _screen([850, 851, 852, 853], system=137),
            _screen([None, None, None, None], system=138),
            _screen([870, 871, 872, 873], system=139),
            _screen([874, 875, 876, 877], system=140),
        ],
    )

    assert run.scan() == 0

    assert run.skipped() == [None, None, 0], (
        "读不出名次的那一屏答 None，紧接着的下一屏也答 None；第四屏恢复正常比较"
    )
    assert not [line for line in run.lines() if "漏掉" in line], "一名都没漏，不许报"
    assert run.payloads("采集重叠断裂") == []


def test_an_unreadable_screen_is_never_counted_as_a_clean_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **`None` 不许在 payload 里落成 0。**

    落成 0 的话，日志上「这一屏量过、没漏」和「这一屏连名次都没读出来」长得
    一模一样——而后者恰恰是最可疑的那种屏。查库的人分不开这两件事，就等于
    这条判据没记住任何东西。
    """
    run = _harness(
        monkeypatch,
        [
            _screen([850, 851], system=137),
            _screen([None, None], system=138),
        ],
    )

    assert run.scan() == 0

    # 断 `is None` 而不是 `== 0`：那个键**必须在**（不是「漏记了」），
    # 而它的值必须是「不知道」。
    assert "rows_skipped" in run.payloads("采集一屏")[0]
    assert run.payloads("采集一屏")[0]["rows_skipped"] is None


# -- 被打断也要报得出来 ---------------------------------------------------------


def test_an_interrupted_run_still_reports_what_it_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **`missed` 与 `previous_last_rank` 是提到 `try` 之前的，这条守的就是那件事。**

    实机上打断这个循环的是 Ctrl+C 和调度器抢占，两者都进不了任何 `except`，
    只走 `finally`。而收尾那条 `采集重叠断裂` 就在 `finally` 里——两个变量要是
    定义在 `try` 里面（或者循环里面），`finally` 读它们会撞 `NameError`：
    一次干净的中断被变成一条堆栈，而**这一趟已经漏掉的四名连带被吞掉**。

    所以这里既断言异常**原样穿出去**（撞了 `NameError` 的话 `pytest.raises`
    收到的就不是 `KeyboardInterrupt` 了），也断言那条记录照样落下来。
    """
    run = _harness(
        monkeypatch,
        [
            _screen([850, 851], system=137),
            _screen([856, 857], system=138),  # 漏 4（852–855）
        ],
        interrupt_after=2,
        scrolls=2,
    )

    with pytest.raises(KeyboardInterrupt):
        run.scan()

    assert ("采集重叠断裂", {"rows_missed": 4}) in run.logs
    assert run.nav.closed == 1, "面板照样要关，不然下一趟开在一个开着的面板上"
