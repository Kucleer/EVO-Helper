"""采集循环的**特征化基准**：把一趟跑下来的全部可观测输出录成清单。

## 这个文件不判断对错，它只钉住「现在是什么样」

用途只有一个：**给行为中性的重构当安全网**。`docs/军力榜补数/方案.md` 的
Phase 0 第一步要把逐屏那段抽出来给首屏复用（首屏现在完全走在滚动循环之外：
不打「采集一屏」日志、没有 `dry`、没有重叠判断、**不进自愈阀的计数**），
而「行为中性」是最容易偷偷塞进回归的那类改动 —— 说得出口，验不出来。

所以这里录两样，重构前后必须**逐字不变**：

1. **落库的目标序列**（坐标、军力、估算标记、名次），按写入顺序
2. **所有 `say()` 行**，按打印顺序

⚠️ **录的是 `say()` 而不是 `record_log()`。** 后者的 payload 在 Phase 0 里要加字段
（六个计数、影子条件、重置上下文），加了就不可能逐字不变。而 `say()` 是给人看的
那一路，Phase 0 不该动它 —— 这条界线正是这个基准能一直用下去的原因。

⚠️ **它会因为「判据变了」而变红，那是对的。** 判据变了就该重新录，并在提交信息里
说清为什么变。它挡的是「本来只想搬代码，却顺手改了行为」。

## 场景是挑过的，不是随手编的

六屏里第 3、4 屏**故意全是已经见过的坐标**。相邻两屏本来就重叠 3–6 行，而
`take_batch_targets` 按坐标去重，于是 `fresh` 为空 —— 自愈阀现在看的就是这个数，
**所以它会在 OCR 完全正常、历史持续增长的情况下触发**。那正是 Phase 0 要量的
「误触发」，钉在基准里能保证重构不把它悄悄改掉。
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
    ranking_scan.scan(**kwargs)

    written = [
        f"{target.coordinate.galaxy}:{target.coordinate.system}:{target.coordinate.position}"
        f" 军力={target.military_score} 估算={target.military_score_estimated}"
        f" 名次={target.military_rank}"
        for target in repository.saved
    ]
    return _Trace(written=written, said=said, logged=logged)


#: 六屏。**第 3、4 屏故意重复前两屏的坐标** —— 见模块头「场景是挑过的」那一段。
SCENARIO = (
    _rows([10_600.0, 10_590.0, 10_580.0], system=137, first_rank=850),
    _rows([10_570.0, 10_560.0, 10_550.0], system=138, first_rank=853),
    _rows([10_540.0, 10_530.0, 10_520.0], system=137, first_rank=856),
    _rows([10_510.0, 10_500.0, 10_490.0], system=138, first_rank=859),
    _rows([10_480.0, 10_470.0, 10_460.0], system=139, first_rank=862),
    _rows([10_450.0, 1_044.0, 10_430.0], system=140, first_rank=865),
)


def test_the_written_targets_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """落库的目标序列 —— 坐标、军力、估算标记、名次，按写入顺序。"""
    assert _trace(monkeypatch, SCENARIO).written == BASELINE_WRITTEN


def test_the_spoken_lines_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有 `say()` 行 —— 按打印顺序。"""
    assert _trace(monkeypatch, SCENARIO).said == BASELINE_SAID


def test_a_first_screen_that_fills_the_batch_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 首屏就采够 `bot_limit` 的那一条路：`range(1, 0)` 为空，**循环一次都不跑**。

    这一趟连一条「采集一屏」都不会打。Phase 0 的重构要让首屏走进逐屏那段，
    而这一条钉住的是「走进去之后，这条路的对外输出仍然一样」。
    """
    run = _trace(monkeypatch, SCENARIO, bot_limit=3)

    assert run.written == BASELINE_LIMIT_WRITTEN
    assert run.said == BASELINE_LIMIT_SAID


# -- Phase 0 的计数与影子评估 ------------------------------------------------
#
# 上面那三条钉的是「别变」（只录 `say`）；下面这两条钉的是「新加的计数真的量对了」
# （只看 payload 里的特定键）。两者分开是故意的：payload 以后还会加字段，
# 而基准不能因为加字段就变红。


def test_the_counters_separate_the_four_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠⚠ **四个来源必须分得开** —— 这是整个 Phase 0 的前提。

    第 3 屏（`scroll=2`）的坐标全是第 1 屏见过的，于是去重后一个新目标都没有；
    而 OCR 读得完全正确、判据三行全采信、三个点也都进了历史。

        fresh_valued     = 0      ← 自愈阀现在看的就是这个数
        verdict_trusted  = 3      ← 判据其实完全正常
        history_appended = 3      ← 曲线也在往前走

    一个数就区分不了「判据失败」和「去重吃掉了」，而那两件事的处置完全相反。
    """
    run = _trace(monkeypatch, SCENARIO)
    duplicate = run.payloads("采集一屏")[2]

    assert duplicate["fresh_valued"] == 0, "第 3 屏全是已见过的坐标，新增带值目标应为 0"
    assert duplicate["verdict_trusted"] == 3, "判据其实采信了三行"
    assert duplicate["history_appended"] == 3, "三个点也真的进了历史"


def test_the_shadow_evaluation_shows_only_the_current_condition_would_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠⚠ **这一条就是那个「误触发」的定量证据。**

    第 3、4 屏连着两屏全是已见过的坐标，于是：

    - 现行条件（`success_old`）两屏都算失败 → **真阀真的清了 12 个历史点**
    - 而 `success_verdict` / `success_history` 两屏都算成功 → 它们一次都不会重置

    换句话说：这一次重置把收尾补数的证据销毁了，**而扫描本身什么都没错**。

    ⚠️ 影子评估只记录、不生效 —— 真阀照旧响（下面那两条断言）。
    轨迹在换条件之后会分叉，所以这里量到的只是「三种条件在现行轨迹上的比较」。
    """
    run = _trace(monkeypatch, SCENARIO)
    screens = run.payloads("采集一屏")

    for index in (2, 3):
        assert screens[index]["success_old"] is False
        assert screens[index]["success_verdict"] is True
        assert screens[index]["success_history"] is True

    assert screens[2]["shadow_reset"] == [], "第一屏失败还不够两屏，谁都不应该重置"
    assert screens[3]["shadow_reset"] == ["old"], "只有现行条件会在这里重置"

    resets = run.payloads("军力锚点重置")
    assert len(resets) == 1, "真阀照旧响一次 —— 影子计数不得影响它"
    assert resets[0]["history"] == 12, "而它销毁的是 12 个健康的历史点"


def test_the_reset_log_carries_the_two_screens_that_triggered_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠⚠ **阀是看「连续两屏」才响的，所以重置日志必须把那两屏都记下。**

    只记触发当屏的话，事后分不出「真的两屏都读废了」和「去重吃掉了」。
    这一趟的重置日志里那两屏长这样：

        screen_seq=2   fresh_valued=0   verdict_trusted=3
        screen_seq=3   fresh_valued=0   verdict_trusted=3

    两屏都是「新增带值目标为 0、而判据其实采信了三行」—— 误触发的指纹。

    ⚠️ 计数得在阀**之前**就算好。算在后面的话，这里记下的会是前两屏，
    恰好漏掉触发它的那一屏。
    """
    run = _trace(monkeypatch, SCENARIO)
    resets = run.payloads("军力锚点重置")

    assert len(resets) == 1
    detail = resets[0]["screens_detail"]
    assert [screen["screen_seq"] for screen in detail] == [2, 3], (
        f"记的不是触发它的那两屏：{detail}"
    )
    assert all(screen["fresh_valued"] == 0 for screen in detail)
    assert all(screen["verdict_trusted"] == 3 for screen in detail)
    assert resets[0]["screen_seq"] == 3


def test_the_run_summary_says_where_the_last_reset_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **「重置共几次」不够用，要的是「最后一次在第几屏」。**

    收尾补数用的是**最后**那一次重置之后攒起来的历史。一趟里 7 次前段
    误重置 + 1 次临近收尾的真重置，误触发率 87.5% —— 但修掉前七次之后，
    最后那一次照样清空全部历史，补数照样很差。所以位置才是那个关键量。
    """
    run = _trace(monkeypatch, SCENARIO)
    summary = run.payloads("采集收尾汇总")

    assert len(summary) == 1
    assert summary[0]["reset_count"] == 1
    assert summary[0]["last_reset_screen"] == 3
    assert summary[0]["history_at_end"] == 5, "重置清掉 12 点，之后两屏又攒回 5 点"
    assert summary[0]["shadow_resets"] == {"old": 1, "verdict": 0, "history": 0}


#: ⚠️ 几条超长的带了 `noqa: E501` —— 它们是**录下来的原文**，折行会让重录时对不上。
#: 录于 2026-09-06、`a7c4511`（#277 合并后）。判据版本 `curve/3`。
#:
#: ⚠️ 重录过一次：首屏接进 `process_screen` 之后多出了「采集第  0滚」那一行。
#: 那一次的 diff **只有这一行新增**，落库序列一个字都没变 —— 那正是那一步要证明的事。
#:
#: ⚠️ 注意第 6 行「连着 2 屏一个军力值都没采信」那一句 —— 它是在**曲线历史 12 点、
#: OCR 完全正常**的情况下打出来的，成因只是第 3、4 屏的坐标全部重复过。
#: 这就是方案里那个「误触发」，在这里被确定性地钉住了。
BASELINE_WRITTEN: list[str] = [
    "4:137:5 军力=10600.0 估算=False 名次=850",
    "4:137:6 军力=10590.0 估算=False 名次=851",
    "4:137:7 军力=10580.0 估算=False 名次=852",
    "4:138:5 军力=10570.0 估算=False 名次=853",
    "4:138:6 军力=10560.0 估算=False 名次=854",
    "4:138:7 军力=10550.0 估算=False 名次=855",
    "4:139:5 军力=10480.0 估算=False 名次=862",
    "4:139:6 军力=10470.0 估算=False 名次=863",
    "4:139:7 军力=10460.0 估算=False 名次=864",
    "4:140:5 军力=10450.0 估算=False 名次=865",
    "4:140:6 军力=10440.0 估算=True 名次=866",
    "4:140:7 军力=10430.0 估算=False 名次=867",
]

BASELINE_SAID: list[str] = [
    "翻了 800 行到达 bot 区",
    "  采集第  0滚 读出  3 行 本屏 bot 3 连续空屏 0",
    "  ⚠️ 与上一屏没有一个共同坐标：重叠可能断了（中间的行没被读过）",
    "  采集第  1滚 读出  3 行 本屏 bot 3 连续空屏 0",
    "  ⚠️ 与上一屏没有一个共同坐标：重叠可能断了（中间的行没被读过）",
    "  采集第  2滚 读出  3 行 本屏 bot 0 连续空屏 1",
    "⚠️ 连着 2 屏一个军力值都没采信（锚点 10510.0、曲线历史 12 点）：锚点和历史一起撤掉重新起头",
    "  ⚠️ 与上一屏没有一个共同坐标：重叠可能断了（中间的行没被读过）",
    "  采集第  3滚 读出  3 行 本屏 bot 0 连续空屏 2",
    "  ⚠️ 与上一屏没有一个共同坐标：重叠可能断了（中间的行没被读过）",
    "  采集第  4滚 读出  3 行 本屏 bot 3 连续空屏 0",
    "军力值不可信，丢掉这几行的分数（坐标保留）[判据 curve/3 · 曲线参照 10430（±3.0%，1/3 行用上了，历史 5 点） · 锚点 10480.0]: [(1, 866, 1044.0, '渲染不出')]",  # noqa: E501
    "  ⚠️ 与上一屏没有一个共同坐标：重叠可能断了（中间的行没被读过）",
    "  采集第  5滚 读出  3 行 本屏 bot 3 连续空屏 0",
    "军事榜采集完成：真人段走了 800 行（盲滚 700 行 + 检测 0 屏），采集段滚了 5 屏；逐屏写入 12 条，其中末屏可疑 0 条",  # noqa: E501
    "⚠️ 本趟有 5 屏与上一屏没有共同坐标（重叠可能断了；中间的行没被读过，事后判据救不了）",
]

BASELINE_LIMIT_WRITTEN: list[str] = [
    "4:137:5 军力=10600.0 估算=False 名次=850",
    "4:137:6 军力=10590.0 估算=False 名次=851",
    "4:137:7 军力=10580.0 估算=False 名次=852",
]

BASELINE_LIMIT_SAID: list[str] = [
    "翻了 800 行到达 bot 区",
    "  采集第  0滚 读出  3 行 本屏 bot 3 连续空屏 0",
    "已采够军力攻击批次 3 个 bot；交给攻击任务",
    "军事榜采集完成：真人段走了 800 行（盲滚 700 行 + 检测 0 屏），采集段滚了 0 屏；逐屏写入 3 条，其中末屏可疑 0 条",  # noqa: E501
]
