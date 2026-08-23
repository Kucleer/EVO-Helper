"""军力锚点的**接线**：两道判据谁先跑、锚点交给下一屏什么、以及它跨屏活不活着。

判据本身住在 `domain.ranking.trusted_scores`（用例在
`tests/unit/domain/test_ranking_score_cliff.py`）。这个文件量的是它在
`tools.ranking_scan` 里被接成什么样：

1. `screen_scores` 里 `renderable_score` 与锚点比较的**顺序**；
2. `next_score_anchor` 交给下一屏的是什么，整屏读废时又是什么；
3. `targets_from_rows` 端到端：丢掉的是分数不是行、补出来的值标成估算、
   而「是不是 bot」吃的仍是 OCR 的**原始读数**；
4. 采集循环里锚点**真的跨屏传下去了**——生产 2026-08-23 那三屏偏小 10 倍的
   读数只有跨屏才看得见，锚点断在任何一屏上，这道判据就等于没有。

⚠️ 循环那条用例全程不起游戏、不驱动鼠标、不碰任何数据库：驱动、导航、OCR、
仓储全是替身，读到的那几屏是喂进来的清单。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_nav import ScrollOutcome, ScrollStep
from evo_helper.game.ranking_ui import BLIND_SCROLL_MARGIN_ROWS, BLIND_SCROLL_ROWS
from evo_helper.tools import ranking_scan
from evo_helper.tools.ranking_scan import (
    HumanStretch,
    next_score_anchor,
    screen_scores,
    targets_from_rows,
)

NOW = datetime(2026, 8, 23, 20, 35, tzinfo=UTC)

#: 真人段那一段直接宣布「翻到 bot 区了」时报的行数。理由同
#: `test_ranking_overlap_gap.py`：比盲滚行数多留一个余量，免得多出一条
#: 「盲滚余量告急」的告警。
ROWS_TO_BOT_AREA = BLIND_SCROLL_ROWS + BLIND_SCROLL_MARGIN_ROWS + 17


def _rows(
    scores: Sequence[float | None], *, system: int = 137, first_rank: int = 850
) -> list[RankingRow]:
    """一屏 bot 行：军力照 `scores` 给，名次逐行 +1，坐标按 `system` 排。

    位号从 5 起：1–4 号位是游戏固定生成的海盗，`is_bot_entry` 会整行剔掉。
    """
    rows: list[RankingRow] = []
    for index, score in enumerate(scores):
        position = 5 + index
        rows.append(
            RankingRow(
                rank=first_rank + index,
                name=f"bot_4_{system}_{position}",
                score=score,
                coordinate=Coordinate(4, system, position),
            )
        )
    return rows


# -- 两道判据的顺序：先看渲染得出来吗，再和锚点比 -------------------------------


def test_a_score_the_game_cannot_render_is_never_allowed_to_become_the_basis() -> None:
    """⚠️⚠️ **顺序反过来会把一整段好读数判成破坏降序。**

    3,835 是 2026-08-23 语料里的真实错读（图上 `9.83K` 读成 `3.835K`）——它多插了
    一位，游戏渲染不出来（1000 以上的军力必然是 10 的整数倍），所以
    `renderable_score` 认得出它，而锚点判据认不出：3,835 比锚点 9,650 小，
    跌幅也不到 5 倍，完全「合法」。

    先跑锚点判据的话，3,835 会被采信、当上后面那些行的基准，于是 9,600 和 9,590
    都成了「比上一行大」——**一格读错，整屏归零**。先按「渲染得出来吗」把它挑掉，
    后面两行的基准就还是锚点 9,650，两个都留得下来。
    """
    rows = _rows([3_835.0, 9_600.0, 9_590.0])

    assert screen_scores(rows, anchor=9_650.0) == [None, 9_600.0, 9_590.0]


def test_the_two_nets_together_catch_both_directions() -> None:
    """两道网挡的是两个方向，摆在一起过一遍。

    10,259 是多插一位（`10.29K` → `10.259K`）——比上一行小，锚点判据抓不住，
    `renderable_score` 抓得住。93,670 是丢小数点——是 10 的整数倍，
    `renderable_score` 抓不住，锚点抓得住。
    """
    rows = _rows([10_259.0, 93_670.0, 10_200.0])

    assert screen_scores(rows, anchor=10_300.0) == [None, None, 10_200.0]


# -- 交给下一屏的锚点 -----------------------------------------------------------


def test_the_anchor_handed_on_is_the_last_trusted_reading_of_the_screen() -> None:
    """正常一屏交出末行那个值——它才是下一屏第一行唯一的可比对象。"""
    rows = _rows([10_980.0, 10_900.0, 10_810.0])

    assert next_score_anchor(rows, anchor=13_200.0) == 10_810.0


def test_the_last_trusted_reading_is_not_the_last_row() -> None:
    """末行本身不可信时，往上找**最后一个可信的**，而不是交出那个坏值。

    这里末行 10,920 比上一行大（屏内破坏降序），交它出去就等于把一个已经判定
    不可信的读数立成下一屏的基准——下一屏的正常读数会被它一起判错。
    """
    rows = _rows([10_900.0, 10_880.0, 10_920.0])

    assert next_score_anchor(rows, anchor=13_200.0) == 10_880.0


def test_a_screen_whose_every_reading_is_refused_keeps_the_old_anchor() -> None:
    """⚠️⚠️ **整屏都不可信时沿用旧锚点，不许清成 `None`。**

    这就是 2026-08-23 生产那三屏偏小 10 倍的中间一屏：整屏被判掉之后清空锚点，
    下一屏就没有任何可比对象——而那一屏同样是偏小 10 倍的，于是**它照样落库**。
    清空等于把这道判据只用一次，而故障恰恰是连着三屏。
    """
    rows = _rows([1_750.0, 1_700.0, 1_600.0])

    assert next_score_anchor(rows, anchor=13_200.0) == 13_200.0


def test_a_screen_that_read_nothing_at_all_keeps_the_old_anchor() -> None:
    """整屏读废（离页、面板没铺开）时 `read_rows()` 交的是空列表，同样沿用。

    这一路是**常态**而不是异常：`rows_from_image` 只要名字读不出就丢行，
    一屏全丢就是空的。
    """
    assert next_score_anchor([], anchor=13_200.0) == 13_200.0
    assert next_score_anchor([], anchor=None) is None


# -- 端到端：丢的是分数不是行 ---------------------------------------------------


def test_the_first_row_of_a_screen_is_now_judged_against_the_previous_screen() -> None:
    """⚠️⚠️ **生产漏网①的端到端：93,670 是屏首那一行，原先没有任何约束。**

    锚点传进来之后它落不进库了。留意 `military_score` 是 `None` 而不是某个补出来
    的数：它左边没有邻居（就是第一行），`interpolate_scores` 不外推——榜首/榜尾
    之外编一个数出来就是凭空造数据。

    行本身照旧保留：坐标是好的，丢的只是分数。

    ⚠️ 生产那一屏第四行读的是 6,625，这里刻意**没**放进来：它不是 10 的整数倍，
    走的是另一道网（`renderable_score`，多插了一位），放进来只会让这条用例同时
    量两件事。那一道有它自己的用例。
    """
    rows = _rows([93_670.0, 9_650.0, 9_650.0])

    targets = targets_from_rows(rows, observed_at=NOW, anchor=9_650.0)

    assert len(targets) == 3, "丢的是分数不是行"
    assert [t.coordinate for t in targets] == [row.coordinate for row in rows]
    assert [t.military_score for t in targets] == [None, 9_650.0, 9_650.0]
    assert 93_670.0 not in [t.military_score for t in targets]


def test_a_row_dropped_by_the_anchor_is_refilled_and_marked_estimated() -> None:
    """被锚点判掉的那一行，补出来的值必须**标成估算**。

    ⚠️ 判据看的是「丢完之后」那份，不是「读到的」那份：看后者的话，被判掉的行会
    伪装成实读，而它恰恰是这一屏最不可信的一条。这里 10,675 是上下两个好邻居的
    中点，不是量出来的数。
    """
    rows = _rows([10_700.0, 93_670.0, 10_650.0])

    targets = targets_from_rows(rows, observed_at=NOW, anchor=10_740.0)

    assert [t.military_score for t in targets] == [10_700.0, 10_675.0, 10_650.0]
    assert [t.military_score_estimated for t in targets] == [False, True, False]


def test_being_a_bot_is_still_decided_by_the_raw_reading() -> None:
    """⚠️⚠️ **这条口径不许被这次改动动到：`is_bot_entry` 吃的是 OCR 的原始读数。**

    用户口径（2026-08-22）：判 bot 要「id 符合 + 军力不等于 0」，因为 `bot_` 前缀
    是玩家可以改名伪装的，而伪装的真人军力常年是 0。

    而这条流水线正好会把那个信号擦掉：0 分那一行相对锚点是「跌掉 10 倍以上」，
    先被判成不可信、再被 `interpolate_scores` 补成两个非零邻居的中点——
    **插出来的值必然非零**，于是它看起来只是「一个普通的低分 bot」。

    所以这里断的是那一行**整行不进目标清单**（三行读数只出两个目标），
    而不是「它的分数变成了什么」。锚点这道新判据多了一条通往插值的路，
    这条口径要跟着一起守住。
    """
    rows = _rows([10_700.0, 0.0, 10_650.0])

    targets = targets_from_rows(rows, observed_at=NOW, anchor=10_740.0)

    assert [t.coordinate for t in targets] == [rows[0].coordinate, rows[2].coordinate]
    assert [t.military_score for t in targets] == [10_700.0, 10_650.0]


def test_an_ordinary_screen_still_stores_every_reading_as_measured() -> None:
    """基线：正常一屏一个分数都不许动，一个都不许标成估算。

    锚点这道判据的误伤代价就是把实读值换成插值——所以「正常屏一个字不动」和
    「异常屏全丢」一样重要。
    """
    rows = _rows([10_980.0, 10_900.0, 10_810.0])

    targets = targets_from_rows(rows, observed_at=NOW, anchor=13_200.0)

    assert [t.military_score for t in targets] == [10_980.0, 10_900.0, 10_810.0]
    assert not any(t.military_score_estimated for t in targets)


# -- 采集循环：锚点跨屏传下去 ---------------------------------------------------
#
# 下面这一套替身照 `test_ranking_overlap_gap.py` 那份搬过来（同一个循环、同一批
# 替身），只把「喂进去的那几屏」换成生产 2026-08-23 那趟的军力形状。


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
    """假 `LiveDriver`。**一次点击、一次移动、一次截图都不发。**"""

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
        """开榜之后 `read_rows()` 读到的那一屏。"""
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
    """假仓储。**连引擎都不建**——生产库和测试库一样碰不到。"""

    def __init__(self) -> None:
        self.saved: list[RankingTarget] = []

    def save_ranking_targets(self, targets: Sequence[RankingTarget]) -> None:
        self.saved.extend(targets)


def _reached_bots(**kwargs: Any) -> HumanStretch:
    """替掉真人段：直接宣布「翻到 bot 区了」。

    ⚠️ 真跑一遍不是更真实，而是更吵：盲滚和检测那两段会往输出里灌十几行，
    而那一段有它自己一整个文件的用例（`test_ranking_human_stretch.py`）。
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


def _collect(
    monkeypatch: pytest.MonkeyPatch, screens: Sequence[Sequence[RankingRow]]
) -> list[RankingTarget]:
    """把 `scan()` 架空到只剩采集循环，跑完，交回**落库的那一批目标**。

    滚屏数刚好等于要发的屏数，循环**自然走完**——不靠离页也不靠 `DRY_SCREENS` 收尾。
    """
    board = _Board(screens)
    nav = _Nav(board)
    repository = _Repository()
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
    monkeypatch.setattr(ranking_scan, "SqlAlchemyRepository", lambda _factory: repository)
    monkeypatch.setattr(ranking_scan, "rows_from_image", lambda *_a, **_k: board.first())
    monkeypatch.setattr(ranking_scan, "scroll_through_humans", _reached_bots)
    monkeypatch.setattr(ranking_scan, "say", lambda _line: None)
    monkeypatch.setattr(ranking_scan, "record_system_log", lambda *_a, **_k: None)

    assert ranking_scan.scan(bot_scrolls=len(board.screens) - 1) == 0
    return repository.saved


def test_the_anchor_is_carried_from_one_screen_to_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **生产 2026-08-23 那趟的形状：三屏偏小 10 倍，断层只在跨屏处看得见。**

    喂进去四屏（每屏换一个恒星系，好让每屏都有新 bot——`take_batch_targets`
    按坐标去重，全屏都是见过的坐标时会提前收工）：

        13,400 13,300 13,200   正常
         1,750  1,700  1,600   偏小 10 倍（真值约 11.75K），屏内自成完好的降序
         1,412  1,400  1,380   还是偏小 10 倍
        10,980 10,900 10,810   恢复正常

    中间那两屏**屏内每一行都比上一行小**，所以按屏跑的降序判据一个都抓不到；
    六个数全是 10 的整数倍，`renderable_score` 也抓不到。只有把上一屏末尾的
    13,200 带进来才看得见断层——而锚点断在循环里的任何一处，这道判据就等于没接。

    第三屏还额外钉住**沿用**：第二屏整屏被判掉、没有可信值可交，锚点必须仍是
    13,200。清成 `None` 的话第三屏就没有可比对象，1,412 那一批照样落库
    ——那正是生产上真实发生的事。

    第四屏钉的是**不许连坐**：13,200 → 10,980 是正常的相邻名次差，三个实读值
    一个都不许被换成估算值。
    """
    saved = _collect(
        monkeypatch,
        [
            _rows([13_400.0, 13_300.0, 13_200.0], system=137, first_rank=850),
            _rows([1_750.0, 1_700.0, 1_600.0], system=138, first_rank=853),
            _rows([1_412.0, 1_400.0, 1_380.0], system=139, first_rank=856),
            _rows([10_980.0, 10_900.0, 10_810.0], system=140, first_rank=859),
        ],
    )

    scores = [target.military_score for target in saved]

    assert scores[:3] == [13_400.0, 13_300.0, 13_200.0], "正常那一屏一个字都不许动"
    assert scores[3:9] == [None] * 6, (
        "偏小 10 倍那两屏必须整个丢掉分数；第二屏靠上一屏的锚点，第三屏靠「整屏不可信时沿用旧锚点」"
    )
    assert scores[9:] == [10_980.0, 10_900.0, 10_810.0], "恢复正常那一屏不许连坐"
    assert not any(target.military_score_estimated for target in saved), (
        "被丢掉的那六行两侧都没有可插值的邻居（整屏都丢了），所以补不出值、"
        "也就不该标成估算——估算标记只在真补出了值的时候为真"
    )
    assert [target.coordinate for target in saved] == [
        Coordinate(4, system, 5 + index) for system in (137, 138, 139, 140) for index in range(3)
    ], "丢的是分数不是行，十二个坐标一个都不许少"
