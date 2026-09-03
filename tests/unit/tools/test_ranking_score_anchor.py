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
from evo_helper.domain.ranking import RankingRow, trusted_scores
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


def test_the_anchor_handed_on_is_the_biggest_trusted_reading_not_the_last() -> None:
    """⚠️⚠️ **锚点取这一屏的最大值，不是末行。**

    守的是 2026-08-23 自己踩的一个设计错误：第一版取「最后一个可信值」，
    而**相邻两屏必然重叠**（一次拖动推进约 8 行，一屏可见 9–14 行），
    所以本屏头几行就是上屏的中段、军力**理应高于上屏末行**。拿末行当降序基准，
    每屏开头那 4–5 行会被整段判成「破坏降序」：

        上屏  … 10690 10660 10640 10620       末行 10620 当锚点
        本屏  10690 10660 10640 10620 10600 10580
        判据  ✗     ✗     ✗     ✓     ✓     ✓

    后果本来只是日志噪声（被丢的那几行早在上一屏就以真值入过库，
    `take_batch_targets` 按坐标去重不会写回估算值），但那句「军力值不可信」
    会**每屏都打一次**——而这道判据存在的意义就是让那句话有信号。每屏都喊等于没喊。
    """
    rows = _rows([10_980.0, 10_900.0, 10_810.0])

    assert next_score_anchor(rows, anchor=13_200.0) == 10_980.0


def test_the_overlapping_head_of_the_next_screen_is_not_refused() -> None:
    """接着上一条：拿最大值当锚点之后，**重叠那几行必须全部放行**。

    这是这个改动唯一真正要证明的事——上一条只说锚点取哪个数，这一条说那个数
    用下去的效果。
    """
    previous = _rows([10_690.0, 10_660.0, 10_640.0, 10_620.0])
    anchor = next_score_anchor(previous, anchor=None)

    # 本屏前四行就是上屏的后四行（重叠），后两行是新露出来的。
    overlapping = [10_690.0, 10_660.0, 10_640.0, 10_620.0, 10_600.0, 10_580.0]

    assert trusted_scores(overlapping, anchor=anchor) == overlapping


def test_a_refused_last_row_is_not_handed_on_as_the_anchor() -> None:
    """不可信的读数不许当锚点——哪怕它是这一屏最大的那个。

    这里末行 10,920 比上一行大（屏内破坏降序）被判掉；它同时也是三个数里最大的，
    所以「取最大值」这条实现如果写成 `max(所有读数)` 而不是 `max(可信的那些)`，
    就会把一个已经判定不可信的读数立成下一屏的基准。
    """
    rows = _rows([10_900.0, 10_880.0, 10_920.0])

    assert next_score_anchor(rows, anchor=13_200.0) == 10_900.0


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
        self.backfilled: list[RankingTarget] = []

    def save_ranking_targets(self, targets: Sequence[RankingTarget]) -> None:
        self.saved.extend(targets)

    def backfill_missing_military_scores(self, records: Sequence[RankingTarget]) -> int:
        """收尾时的曲线补数。⚠️ 假仓储也要实现它，否则「接进去之后的样子」没被测到。

        真仓储那一版只填 `military_score IS NULL` 的行；这里只记下被补的是哪些，
        由用例断言。
        """
        self.backfilled.extend(records)
        return len(records)


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


def _collect_with_repository(
    monkeypatch: pytest.MonkeyPatch, screens: Sequence[Sequence[RankingRow]]
) -> _Repository:
    """同 `_collect`，但把**假仓储本身**交回来——补数写的不是 `saved` 那一份。"""
    _collect(monkeypatch, screens, _keep=True)
    assert _LAST_REPOSITORY[0] is not None
    return _LAST_REPOSITORY[0]


#: `_collect` 用过的最后一个假仓储。补数那条路要看它的 `backfilled`。
_LAST_REPOSITORY: list[_Repository | None] = [None]


def _collect(
    monkeypatch: pytest.MonkeyPatch,
    screens: Sequence[Sequence[RankingRow]],
    *,
    _keep: bool = False,
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
    _LAST_REPOSITORY[0] = repository
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


# -- 那一行日志得说得清是被哪条判据拦的 -------------------------------------------


def _log_line(monkeypatch: pytest.MonkeyPatch, scores: list[float], *, anchor: float) -> str:
    """跑一遍 `targets_from_rows`，交出它打的那句「军力值不可信」。"""
    said: list[str] = []
    monkeypatch.setattr(ranking_scan, "say", said.append)
    targets_from_rows(_rows(scores), observed_at=NOW, anchor=anchor)
    hits = [line for line in said if "军力值不可信" in line]
    return hits[0] if hits else ""


def test_the_log_says_which_rule_dropped_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️⚠️ **四条判据各有各的处置，日志必须分得开。**

    2026-09-02 查这件事时最费劲的一步就是这个：日志只说「不可信」加一串下标，只能
    拿「值 ÷ 锚点」去反推是哪一条——而实测 71.5% 的被丢值其实 **≤ 锚点**，那个比值
    什么都说明不了，白花了半天。

    - `出界` / `破坏降序` → 低位读错，上下邻居插值补得回来
    - `比基准大/小一个数量级` → 丢首位或多一位，是 ROI / 小数点那类缺陷
    """
    line = _log_line(monkeypatch, [9_770.0, 3_760.0, 9_750.0], anchor=9_800.0)

    assert "出界" in line, "被区间判掉的那一行要说「出界」"
    assert "3760" in line.replace(",", ""), "原值要照抄，不能只留下标"

    magnitude = _log_line(monkeypatch, [9_770.0, 93_700.0, 9_730.0], anchor=9_800.0)
    assert "数量级" in magnitude, "数量级错了要报数量级，不能混进「出界」"


def test_the_log_shows_the_bracket_it_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 区间作不作数决定了走区间判据还是逐行兜底，**两条的宽严差着 3 倍的丢弃率**。

    所以区间是多少、或者为什么不作数，都得看得见——否则事后无法判断某一屏是被
    哪一档处置的。
    """
    line = _log_line(monkeypatch, [9_770.0, 3_760.0, 9_750.0], anchor=9_800.0)

    assert "区间 9750–9770" in line.replace(",", ""), f"没写出用的是哪个区间：{line}"


def test_the_log_says_why_the_bracket_did_not_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 不作数时要说**被哪一条否的** —— 三条的下一步完全不同。

    这里首尾跨度 5.6 倍（末行丢了首位），属于「端点自己读错了」那一档，该去查 ROI，
    而不是去查降序判据。区间既然不作数，这一屏就退回逐行兜底 —— 9,800 破坏降序、
    1,740 跌掉一个数量级，两条都该在日志里说清楚。
    """
    line = _log_line(monkeypatch, [9_740.0, 9_800.0, 1_740.0], anchor=9_800.0)

    assert "区间不作数" in line
    assert "跨度" in line, f"没说清是被哪一条否的：{line}"


def test_the_log_carries_the_rule_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 版本指纹进日志 —— **只靠库里的日志就要能判断生产跑没跑上这一版**。

    仓库里有过教训：#266 那道支线落地时一行新日志都没留，用户问「生产跑的是哪个
    版本」时只能答「看不出」。
    """
    from evo_helper.domain.ranking import SCORE_RULE_VERSION

    line = _log_line(monkeypatch, [9_770.0, 3_760.0, 9_750.0], anchor=9_800.0)

    assert SCORE_RULE_VERSION in line


def test_the_log_and_the_rule_read_the_same_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️⚠️ **日志报的区间，必须是判据真正用的那个。**

    `screen_scores` 先过 `renderable_score` 把多插一位的挑掉，再交给判据。日志若拿
    未过滤的原始读数去算区间，两边就分家了——而分家之后日志会**理直气壮地说错话**。
    我 2026-09-02 写这段时就这么错过一次，是这条用例的来处。

    这里 `10259` 是渲染不出来的（多插了一位，真值约 1,025.9），它会在进判据之前被
    挑掉。⚠️ **它必须放在首行**：放中间的话首尾不变，两份输入算出同一个区间，
    这条用例就什么都证明不了（我第一版就这么写的，变异照样绿）。放首行之后，
    未过滤那份的上界是 10,259，判据真正用的是 9,770。
    """
    line = _log_line(monkeypatch, [10_259.0, 9_770.0, 3_760.0, 9_750.0], anchor=9_800.0)

    assert "区间 9750–9770" in line.replace(",", ""), f"日志用了未过滤的读数算区间：{line}"
    assert "10259" in line.replace(",", ""), "被挑掉的那一行也要出现在被丢清单里"
    assert "渲染不出" in line, "渲染不出来的那一行要照实说，不能留空洞"


def test_the_scan_actually_calls_the_curve_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️⚠️ **补数真的接进了扫描循环，不是一个没人调的函数。**

    `_backfill_from_the_curve` 自己那几条用例（`test_ranking_curve_backfill.py`）
    只证明它算得对；把整句调用删掉时那些用例照样全绿——2026-09-02 变异验证当场
    抓到的就是这个。仓库里同形的教训不止一次：**测了新东西，没测它接进去之后的样子。**

    ## ⚠️ 构造正是用户描述的那个场景

    用户口径（2026-09-02）：「后续的读到正确的判据，可以对之前没有读出的读数进行
    回填补数」。所以读不出的那一行放在**第三屏的末行**，而第四屏提供它下方的点：

    - 放**末行**：屏内没有右邻居，`interpolate_scores` 补不了（第一版放在中间，
      屏内插值当场就补掉了，用例什么都没证明）
    - 放**第三屏**而不是最后一屏：`curve_reference` 不许单边外推，得有后面的屏
      提供更低名次的点。**一趟最末那几行按设计就是补不上的**（见
      `test_the_curve_refuses_to_extrapolate_off_one_side`）
    """
    repository = _collect_with_repository(
        monkeypatch,
        [
            _rows([9_800.0, 9_790.0, 9_780.0], system=137, first_rank=850),
            _rows([9_770.0, 9_760.0, 9_750.0], system=138, first_rank=853),
            _rows([9_740.0, 9_730.0, None], system=139, first_rank=856),
            _rows([9_710.0, 9_700.0, 9_690.0], system=140, first_rank=859),
        ],
    )

    assert repository.backfilled, "扫描收尾没调曲线补数——那个函数没人调"
    filled = repository.backfilled[0]
    assert filled.military_score is not None
    assert filled.military_score_estimated is True


def test_a_poisoned_curve_history_does_not_wipe_the_rest_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠⚠ **历史被一批错读毒了之后，整趟余下的军力值不许全空。**

    #275 上线当天生产上真实发生过（20:28–20:30，连着 10 屏）：

        参照 -357 → -3,110 → -13,750 → -37,862 → -48,203 → -49,304
        「撤掉锚点重新起头」这句**响了 8 次**，而屏幕照旧一屏一屏全丢
        那一趟 302 个被丢行里 101 行（33%）出自这一段，不是真误读

    这是个**吸收态**：坏点进了历史 → 参照荒谬 → 整屏全丢 → 一个新点也进不了
    历史 → 下一屏还是同一批坏点。**它自己维持着自己**，而每屏只打一句「丢掉这
    几行」，没有累计信号。

    ⚠️ **自愈阀原先只撤锚点，而那一点用都没有** —— 有曲线参照时判据只听曲线的，
    锚点根本不参与。所以阀必须把 `score_history` 一起撤掉。

    ## ⚠⚠ 毒得从「曲线还没法说话」的时候进去

    曲线一旦立起来，它自己就会拦住后续的错读（这正是它存在的意义）。所以这一屏
    必须是**整趟的第一屏**：那时历史凑不够 `CURVE_FIT_POINTS`（4）个点，逐行兜底那几
    条又只看「比上一行小」和「与基准差几倍」——一批**彼此递减、自成一体**的丢首位
    读数（1050/1040/1030/1020）两条都不触发，悉数被采信。

    ⚠️ 那四个值必须是 **10 的整数倍** —— `renderable_score` 只放行游戏真渲染得出来的
    读数（1059 过不了，1050 过得了）；拿一个渲染不出的值来造毒，它在进判据之前就被
    挑掉了，毒不进去——我 2026-09-03 第一版用例就是这么假绿的。

    ⚠️ 而坐标都是好的，所以下游没有任何判据看得出来 —— 这是仓里
    `trusted_scores` 那段早就记下的「整屏中位数为 0」同一类入口，只是这次毒的是曲线。
    """
    saved = _collect(
        monkeypatch,
        [
            # 第一屏：首行是真值，后四行把 10,5xx 读成了 1,05x（丢首位）。
            # 那四行彼此递减，而那时既没锚点也没曲线，于是全部被采信进历史。
            _rows([10_600.0, 1_050.0, 1_040.0, 1_030.0, 1_020.0], system=137, first_rank=1060),
            # 之后每一屏都是好读数。前两屏会被那四个坏点全丢（自愈阀的代价），
            # 而从第三屏起必须恢复 —— 阀已经响了。
            _rows([10_500.0, 10_490.0, 10_480.0], system=138, first_rank=1065),
            _rows([10_470.0, 10_460.0, 10_450.0], system=139, first_rank=1068),
            _rows([10_440.0, 10_430.0, 10_420.0], system=140, first_rank=1071),
            _rows([10_410.0, 10_400.0, 10_390.0], system=141, first_rank=1074),
            _rows([10_380.0, 10_370.0, 10_360.0], system=142, first_rank=1077),
        ],
    )

    # 前 5 行是那一屏毒源，接下去 6 行是自愈阀触发前的代价，再往后必须全有值。
    tail = [target.military_score for target in saved[11:]]
    assert tail, "用例自己没送够屏，后面那段断言测不到东西"
    assert tail.count(None) == 0, (
        f"自愈阀响过之后仍有 {tail.count(None)}/{len(tail)} 行军力值是空的"
        f" —— 吸收态还在（历史没跟着锚点一起撤）：{tail}"
    )
