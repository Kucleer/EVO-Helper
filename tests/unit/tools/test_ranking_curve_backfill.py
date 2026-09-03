"""收尾时用这一趟的曲线，把没读出军力值的那些行补上。

用户口径（2026-09-02，逐字）：

    「后续的读到正确的判据，可以对之前没有读出的读数进行回填补数，
     这样只需要几个节点 就能补很多的数据了」

## 为什么补得到

一屏里读废的那几行，**后面几屏读到的可信值会把曲线延长过它的名次**。榜单按军力降序、
邻近名次的军力相近，所以曲线一旦盖过那个位置，就推得出它该是多少。

它比屏内插值（`interpolate_scores`）能补的多一档：那个只看**同一屏**的上下邻居，
屏首屏尾没有邻居就补不了。2026-09-02 实测全库 575 行「有名次、没军力值」，
其中 495 个（86%）在 ±60 名内凑得够 5 个已知点。

## 这个文件钉的是三条边界

补进去的是**估算值**，而库里已有的可能是**量出来的**——所以只填空、绝不覆盖，
而且一律标估算。凑不够历史点就不补。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import RankingTarget
from evo_helper.tools.ranking_scan import _backfill_from_the_curve

NOW = datetime(2026, 9, 2, tzinfo=UTC)
HISTORY = [(1600 + i, 9500.0) for i in range(8)]


class _Repository:
    """假仓储：记下被补的是哪些。真仓储那一版只填 `military_score IS NULL` 的行。"""

    def __init__(self, refuse: int = 0) -> None:
        self.backfilled: list[RankingTarget] = []
        self._refuse = refuse

    def backfill_missing_military_scores(self, records: Sequence[RankingTarget]) -> int:
        self.backfilled.extend(records)
        return max(len(records) - self._refuse, 0)


def _unread(rank: int | None, coordinate: Coordinate = Coordinate(4, 30, 12)) -> RankingTarget:
    return RankingTarget(
        coordinate=coordinate,
        military_score=None,
        military_score_at_utc=NOW,
        military_score_estimated=False,
        military_rank=rank,
    )


def _say(_message: str) -> None:
    return None


def _run(repo: Any, unread: list[RankingTarget], history: list[tuple[int, float]]) -> int:
    import evo_helper.tools.ranking_scan as module

    saved, module.say = module.say, _say
    try:
        return _backfill_from_the_curve(repo, unread, history=history)
    finally:
        module.say = saved


def test_a_row_the_scan_could_not_read_gets_filled_from_the_curve() -> None:
    """⚠️ 本文件的重点：这一趟后面读到的值，把前面读废的那一行补回来。"""
    repo = _Repository()

    assert _run(repo, [_unread(1604)], list(HISTORY)) == 1
    assert repo.backfilled[0].military_score == 9500.0


def test_what_gets_filled_in_is_marked_as_an_estimate() -> None:
    """⚠️⚠️ **猜出来的数不许长得像量出来的。**

    补进去的是从邻近名次推出来的，不是读出来的。不标估算的话，页面上、选靶那边
    都会把它当成实测值——而选靶的排序判据正是「军力 ÷ 往返小时」，分子是猜的却
    看不出来，是这个仓库明令禁止的那一档。
    """
    repo = _Repository()
    _run(repo, [_unread(1604)], list(HISTORY))

    assert repo.backfilled[0].military_score_estimated is True


def test_a_rank_the_curve_does_not_reach_is_left_alone() -> None:
    """⚠️ 凑不够历史点就不补——名次读错到离谱的那些（实测有 11,200 这种）
    在 ±60 名内一个邻居都没有，被这一条挡在外面，不需要另写判据。"""
    repo = _Repository()

    assert _run(repo, [_unread(11_300)], list(HISTORY)) == 0
    assert repo.backfilled == []


def test_a_row_without_a_rank_is_left_alone() -> None:
    """名次都没有就谈不上「曲线上的哪个位置」。"""
    repo = _Repository()

    assert _run(repo, [_unread(None)], list(HISTORY)) == 0
    assert repo.backfilled == []


def test_nothing_happens_without_a_curve() -> None:
    """整趟一个可信值都没读到时不补——那时曲线本身就是空的。"""
    repo = _Repository()

    assert _run(repo, [_unread(1604)], []) == 0
    assert repo.backfilled == []


def test_the_count_comes_back_from_the_repository_not_from_the_wish_list() -> None:
    """⚠️ 交出的是**真正补进库的条数**，不是「曲线推得出几条」。

    两者会不一样：库里已经有值的那些行，仓储那一层会跳过（只填空、不覆盖）。
    报「推得出的条数」就是把没做成的事说成做成了——日志说假话比不说更糟。
    """
    repo = _Repository(refuse=1)

    assert _run(repo, [_unread(1604), _unread(1605)], list(HISTORY)) == 1


def test_backfill_still_reaches_across_a_hole_in_the_readings() -> None:
    """⚠️⚠️ **补数这一步刻意不吃密度闸（`CURVE_MAX_GAP`），别顺手给它加上。**

    2026-09-03 给**边扫边判**那一段加了密度闸：单侧外推时，窗口里隔着大洞的参照有
    47.6% 偏出 3%，而它在那里会**否决好读数**，代价远大于收益。

    补数这一步的处境不同（两侧、整趟收尾、历史是全的），代价也反过来 ——
    同一批数据上加了闸，这一轮能补的从 **147/160（92%）掉到 84/160（52%）**。
    要不要拿 40 个百分点的覆盖率去换那 28%，是范围决定，归用户。

    所以这条用例钉的是「隔着洞也照样补」。它红了不代表回归，代表**有人替用户做了
    那个决定** —— 那时候要改的是这段注释和用户的口径，不是悄悄让它变绿。
    """
    repo = _Repository()
    # 名次 1600 与 1610 之间整整 9 个名次没有读数，远超 CURVE_MAX_GAP（4）。
    holed = [(1598, 9_560.0), (1600, 9_550.0), (1610, 9_500.0), (1612, 9_490.0)]

    assert _run(repo, [_unread(1605)], holed) == 1, "补数被密度闸挡住了 —— 那不是这一步该吃的闸"
    assert repo.backfilled[0].military_score_estimated is True
