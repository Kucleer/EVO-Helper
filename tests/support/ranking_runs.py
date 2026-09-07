"""跑一趟军力榜采集的假环境：驱动、导航、OCR、仓储全是替身。

⚠️ **全程不起游戏、不驱动鼠标、不碰任何数据库。** 读到的那几屏是喂进来的清单。

放在 `tests/support` 而不是某一个用例文件里，因为有两处要用同一趟：

- `test_ranking_scan_characterization.py` —— 特征化基准与 Phase 0 的计数
- `test_ranking_replay.py` —— 拿它录出来的语料去验回放工具

两边必须跑**同一趟**：回放的全部意义就是「同一份输入下新旧算法对照」，
两个文件各自编一份场景就失去了那个对照。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_nav import ScrollOutcome, ScrollStep
from evo_helper.game.ranking_ui import BLIND_SCROLL_MARGIN_ROWS, BLIND_SCROLL_ROWS
from evo_helper.tools import ranking_scan
from evo_helper.tools.ranking_scan import HumanStretch

#: 真人段直接宣布「翻到 bot 区了」时报的行数。比盲滚行数多留一个余量，
#: 免得多出一条「盲滚余量告急」的告警（同 `test_ranking_score_anchor.py`）。
ROWS_TO_BOT_AREA = BLIND_SCROLL_ROWS + BLIND_SCROLL_MARGIN_ROWS + 17


def _rows(scores: Sequence[float | None], *, system: int, first_rank: int) -> list[RankingRow]:
    """一屏 bot 行：军力照 `scores` 给，名次逐行 +1，坐标按 `system` 排。

    位号从 5 起：1–4 号位是游戏固定生成的海盗，`is_bot_entry` 会整行剔掉。
    """
    return [
        RankingRow(
            rank=first_rank + index,
            name=f"bot_4_{system}_{5 + index}",
            score=score,
            coordinate=Coordinate(4, system, 5 + index),
        )
        for index, score in enumerate(scores)
    ]


class _NoOcr:
    """假 `pytesseract` 模块，塞进 `sys.modules` —— `scan()` 开头那个赋值是进程级的。"""

    class _Binary:
        tesseract_cmd = ""

    def __init__(self) -> None:
        self.pytesseract = _NoOcr._Binary()


class _Settings:
    """假配置。三个值都故意填成不可用的：这一趟不碰 OCR，也不碰库。"""

    tesseract_path = "这一趟一次 OCR 都不做"
    player_name = "Kucleer"
    database_url = "这一趟一次连接都不建"


class _Driver:
    """假 `LiveDriver`。一次点击、一次移动、一次截图都不发。"""

    def capture(self) -> object:
        return object()

    def wait(self, _seconds: float) -> None:
        pass


class _Board:
    """一趟采集会读到的那几屏，按顺序发。"""

    def __init__(self, screens: Sequence[Sequence[RankingRow]]) -> None:
        self.screens = [list(screen) for screen in screens]
        self.handed = 0

    def first(self) -> list[RankingRow]:
        self.handed = 1
        return list(self.screens[0])

    def scroll_once(self) -> ScrollStep[RankingRow]:
        rows = self.screens[self.handed]
        self.handed += 1
        return ScrollStep(outcome=ScrollOutcome.SCROLLED, rows=tuple(rows))


class _Nav:
    """假 `RankingNavigator`。只交行，不动画面。"""

    def __init__(self, board: _Board) -> None:
        self.board = board
        self.closed = 0

    def open_military_ranking(self) -> None:
        pass

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
    """假仓储。连引擎都不建 —— 生产库和测试库一样碰不到。"""

    def __init__(self) -> None:
        self.saved: list[RankingTarget] = []
        self.backfilled: list[RankingTarget] = []

    def save_ranking_targets(self, targets: Sequence[RankingTarget]) -> None:
        self.saved.extend(targets)

    def backfill_missing_military_scores(self, records: Sequence[RankingTarget]) -> int:
        self.backfilled.extend(records)
        return len(records)

    def military_states(
        self, coordinates: Sequence[Coordinate]
    ) -> dict[Coordinate, dict[str, Any]]:
        """只给逐屏语料用。假库里假装第一个坐标已经有值、第二个已拉黑。"""
        states: dict[Coordinate, dict[str, Any]] = {}
        for index, coordinate in enumerate(coordinates):
            if index == 0:
                states[coordinate] = {
                    "military_score": 1.0,
                    "estimated": False,
                    "blacklisted": False,
                }
            elif index == 1:
                states[coordinate] = {
                    "military_score": None,
                    "estimated": False,
                    "blacklisted": True,
                }
        return states


def _reached_bots(**kwargs: Any) -> HumanStretch:
    """替掉真人段：直接宣布「翻到 bot 区了」。"""
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


@dataclass(frozen=True)
class _Trace:
    """一趟跑下来的全部可观测输出。"""

    written: list[str]
    said: list[str]
    logged: list[tuple[str, dict[str, Any]]]

    def payloads(self, message: str) -> list[dict[str, Any]]:
        return [payload for logged, payload in self.logged if logged == message]


def _trace(
    monkeypatch: pytest.MonkeyPatch,
    screens: Sequence[Sequence[RankingRow]],
    *,
    bot_limit: int | None = None,
    capture_rows: bool = False,
) -> _Trace:
    """跑一趟，交回落库清单 / say 行 / 结构化 payload。三样都是有序的。"""
    board = _Board(screens)
    nav = _Nav(board)
    repository = _Repository()
    said: list[str] = []
    logged: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setitem(sys.modules, "pytesseract", _NoOcr())
    monkeypatch.setattr(ranking_scan, "Settings", _Settings)
    monkeypatch.setattr(ranking_scan, "LiveDriver", _Driver)
    monkeypatch.setattr(ranking_scan, "SlowDragDriver", lambda driver: driver)
    monkeypatch.setattr(ranking_scan, "release_stuck_mouse", lambda _driver: None)
    monkeypatch.setattr(ranking_scan, "enter_game_exit_code", lambda *_a, **_k: 0)
    monkeypatch.setattr(ranking_scan, "RankingNavigator", lambda **_kwargs: nav)
    monkeypatch.setattr(ranking_scan, "create_database_engine", lambda _url: None)
    monkeypatch.setattr(ranking_scan, "create_session_factory", lambda _engine: None)
    monkeypatch.setattr(ranking_scan, "SqlAlchemyRepository", lambda _factory: repository)
    monkeypatch.setattr(ranking_scan, "rows_from_image", lambda *_a, **_k: board.first())
    monkeypatch.setattr(ranking_scan, "scroll_through_humans", _reached_bots)
    monkeypatch.setattr(ranking_scan, "say", said.append)

    def _record(_level: str, _source: str, message: str, **kwargs: Any) -> None:
        logged.append((message, dict(kwargs.get("payload") or {})))

    monkeypatch.setattr(ranking_scan, "record_system_log", _record)

    kwargs: dict[str, Any] = {"bot_scrolls": len(board.screens) - 1}
    if bot_limit is not None:
        kwargs["bot_limit"] = bot_limit
    if capture_rows:
        kwargs["capture_rows"] = True
    ranking_scan.scan(**kwargs)

    written = [
        f"{target.coordinate.galaxy}:{target.coordinate.system}:{target.coordinate.position}"
        f" 军力={target.military_score} 估算={target.military_score_estimated}"
        f" 名次={target.military_rank}"
        for target in repository.saved
    ]
    return _Trace(written=written, said=said, logged=logged)


def _human_rows(*, first_rank: int, count: int = 3) -> list[RankingRow]:
    """一屏**真人**行：`coordinate=None`，所以 bot 过滤会整批剔掉。

    ⚠️ **判据看的是坐标，不是名字。** `domain.ranking.is_bot_entry` 要「名字反解得出
    坐标 + 军力不为 0」——真人的名字反解不出坐标，所以这里必须给 `None`。
    给个坐标就等于伪造了一屏 bot，而这个场景要的恰恰是相反的那一半。

    名字照实机那几屏的样子写（2026-09-07 08:07 那趟名字列读到的是
    `Kucleer XiaoFan result EXSTRIM Playerl423929772`）。
    """
    return [
        RankingRow(
            rank=first_rank + index,
            name=f"Player{first_rank + index}",
            score=10_000.0 - index * 10,
            coordinate=None,
        )
        for index in range(count)
    ]


#: 检测段宣布「到了 bot 区」，可整趟**一个 bot 行都没读到**。
#:
#: ⚠️⚠ **这是 2026-09-07 早上那 6 趟空跑的最小复现。** bot 区检测会误报
#: （明明还在真人段就说「到了」），而实测样本原先就在那一刻发出去 ——
#: 于是一趟采到 0 条的跑法留下一条偏小的实测，下一趟按它滚得更少、又误报、
#: 数更小。**自我强化且不自愈**：当天 7 趟里 6 趟一条都没采到，
#: 一直到有人手工把盲滚行数填死才停下。
SCENARIO_NO_BOTS = (
    _human_rows(first_rank=580),
    _human_rows(first_rank=583),
    _human_rows(first_rank=586),
)

#: 六屏。**第 3、4 屏故意重复前两屏的坐标** —— 见模块头「场景是挑过的」那一段。
SCENARIO = (
    _rows([10_600.0, 10_590.0, 10_580.0], system=137, first_rank=850),
    _rows([10_570.0, 10_560.0, 10_550.0], system=138, first_rank=853),
    _rows([10_540.0, 10_530.0, 10_520.0], system=137, first_rank=856),
    _rows([10_510.0, 10_500.0, 10_490.0], system=138, first_rank=859),
    _rows([10_480.0, 10_470.0, 10_460.0], system=139, first_rank=862),
    _rows([10_450.0, 1_044.0, 10_430.0], system=140, first_rank=865),
)

#: 首两屏一个分数都读不出的场景。
#:
#: ⚠⚠ **专门用来区分「首屏进不进自愈阀」。**
#:
#: `SCENARIO` 里首屏是成功的，所以把阀的 `screen_seq > 0` 闸拿掉也看不出差别
#: （首屏进阀只是把失败计数归零）—— 那个变异在那个场景上是**行为惰性**的。
#:
#: 这一份里首两屏都产不出带值的新目标，于是：
#:
#:     带闸（现行）  首屏不计数 → 重置晚一屏
#:     去掉闸      首屏也计数 → 重置提前到第 1 屏
#:
#: 两边的重置位置不同，回放的 `verify` 就抳得住它了。
SCENARIO_BLIND_START = (
    _rows([None, None, None], system=137, first_rank=850),
    _rows([None, None, None], system=138, first_rank=853),
    _rows([10_540.0, 10_530.0, 10_520.0], system=139, first_rank=856),
    _rows([10_510.0, 10_500.0, 10_490.0], system=140, first_rank=859),
)

#: 首屏读得出、随后**两屏一个分数都读不出**、第四屏又读得出。
#:
#: ⚠️⚠️ **专门用来钉「换了成功条件之后，真盲还得照样重置」。**
#:
#: `SCENARIO` 里那次重置是**误**重置（坐标重复导致 `fresh` 为空，而判据其实读对了），
#: 候选条件会把它去掉 —— 这是对的。但换条件的风险正好在反面：万一把真盲也一起
#: 去掉了，自愈阀就等于废了，而那恰恰是它唯一存在的理由。
#:
#: 这一份里第 1、2 屏名次读得出、分数全 `None`，于是三个口径同时不成立：
#: `fresh_valued=0`、`verdict_trusted=0`、`history_appended=0`。三个条件都该在第 2 屏响。
#:
#: ⚠️ `SCENARIO_BLIND_START` 顶不上这个用：那里盲的是第 0、1 屏，而首屏不进阀
#: （`screen_seq > 0`），第 2 屏就又成功了 —— 三个条件一个都不响，什么也证不了。
SCENARIO_TRULY_BLIND = (
    _rows([10_600.0, 10_590.0, 10_580.0], system=137, first_rank=850),
    _rows([None, None, None], system=138, first_rank=853),
    _rows([None, None, None], system=139, first_rank=856),
    _rows([10_510.0, 10_500.0, 10_490.0], system=140, first_rank=859),
)
