"""曲线判据在**真实的前向单程**里必须点火。

## 为什么单独一个文件钉这件事

`curve/2`（#274）上线后在生产上**一次都没生效过**，而 CI 全绿、日志还报「89.5% 有
曲线参照」。成因是两处配合出来的：

- `curve_reference` 要求目标名次**上下各**两个已知点；
- 而判定是严格的前向单程 —— 一屏判完才进历史，所以判某一行时历史里**只有名次更小
  的点**，「上方」永远凑不齐。

2026-09-03 拿生产一整趟的真实名次前向重放，覆盖率如下：

    curve/1  ±60 内 5 点（单侧可）      判 750 行，拿到参照 717 = 95.6%
    curve/2  两侧各 2 点               判 750 行，拿到参照   0 =  0.0%

⚠️⚠️ **原有的曲线用例一条都没拦住它**，因为它们都是手工塞一份**跨过**目标名次的
历史：

    history = [(1630, ...), (1631, ...), (1635, ...), (1636, ...)]   # 两下两上
    trusted_scores([3480.0], ranks=[1634], history=list(history))

两侧正好凑齐 —— **而这个形状生产上根本产生不出来**。函数本身对，调用它的处境不对，
于是判据在一个到不了的分支里被验证。

所以这个文件不喂手工历史：它**按扫描真正的样子**跑一串前向、不重叠的屏，
让历史自己长出来。任何「只在两侧都有点时才成立」的判据，在这里都会露出来。
"""

from __future__ import annotations

from evo_helper.domain.ranking import CURVE_MAX_GAP, curve_reference, judge_scores

#: 一屏读出来的行数（生产实测 12–14 行）。
SCREEN_ROWS = 12

#: 每滚推进多少个名次。生产实测每滚推进 15.6 个名次、读出 12 行 ——
#: **屏与屏之间没有重叠**，甚至还漏掉几个。这是这个文件的要害：
#: 有重叠时后一屏的头几行能从前一屏尾部借到「上方」的点，没重叠就一个都借不到。
SCROLL_ADVANCE = SCREEN_ROWS


def _screen(first_rank: int, *, top: float, step: float) -> tuple[list[int], list[float]]:
    """一屏干净的读数：名次连续，军力按 `step` 平稳下降。"""
    ranks = [first_rank + offset for offset in range(SCREEN_ROWS)]
    scores = [top - step * offset for offset in range(SCREEN_ROWS)]
    return ranks, scores


def _scan(screens: int, *, first_rank: int = 1000, top: float = 11_290.0, step: float = 10.0):
    """按扫描的样子跑若干屏，交出 `(历史, 锚点, 下一屏首名次, 下一屏该有的顶值)`。

    历史**只由判据自己填**（`judge_scores` 采信一个值才追加），锚点按
    `tools.ranking_scan` 的做法取上一屏最后一个可信值。
    """
    history: list[tuple[int, float]] = []
    anchor: float | None = None
    rank, value = first_rank, top
    for _ in range(screens):
        ranks, scores = _screen(rank, top=value, step=step)
        verdict = judge_scores(scores, anchor=anchor, ranks=ranks, history=history)
        kept = [score for score in verdict.trusted if score]
        if kept:
            anchor = kept[-1]
        rank += SCROLL_ADVANCE
        value -= step * SCROLL_ADVANCE
    return history, anchor, rank, value


def test_the_curve_answers_during_a_forward_scan() -> None:
    """跑够几屏之后，判据必须真的用上曲线参照。

    断言看的是 `Judgement.references` —— **判据自己交出来的、当时真正用上的**
    那份，不是事后拿一份更全的历史去补算。事后补算正是 #274 那次把 0% 报成
    89.5% 的那个动作。
    """
    history, anchor, rank, top = _scan(6)
    ranks, scores = _screen(rank, top=top, step=10.0)

    verdict = judge_scores(scores, anchor=anchor, ranks=ranks, history=history)

    used = [reference for reference in verdict.references if reference is not None]
    assert len(used) == SCREEN_ROWS, (
        f"前向单程跑了 6 屏，这一屏 {SCREEN_ROWS} 行里只有 {len(used)} 行用上了曲线 "
        f"—— 判据在生产上等于没有"
    )


def test_two_sided_history_is_what_a_forward_scan_can_never_produce() -> None:
    """钉住那次回归本身：同一份历史，要求两侧就交不出东西。

    ⚠️ 这不是说两侧要求错了——整趟收尾的补数靠它拦住「往扇描末端外推」。
    错的是把它当成**无条件**的默认值，于是边扇边判那一段也跟着吃下去了。
    """
    history, _, rank, _ = _scan(6)

    assert curve_reference(history, rank, require_both_sides=True) is None, (
        "前向单程的历史里竟然凑出了「上方」的点，这个文件的前提就不成立了"
    )
    assert curve_reference(history, rank, require_both_sides=False) is not None, (
        "不限侧也交不出参照 —— 那么这一步修的不是真正的成因"
    )


def test_the_curve_saves_the_rows_a_misread_bracket_would_condemn() -> None:
    """首尾双双丢首位时，屏内那几行完好的读数必须留下来。

    生产实况（2026-09-03 14:20:32，逐字摘录）：

        [区间 1100–1200 · 锚点 11280]:
        [(1, 1200.0, 比基准小一个数量级), (2, 11160.0, 出界), (3, 11150.0, 出界),
         (4, 11150.0, 出界), (5, 11130.0, 出界), (6, 11130.0, 出界), (7, 11130.0, 出界), ...]

    首尾两行都把 11,2xx 读成了 1,2xx（丢首位），于是屏内区间塌成 1100–1200，
    **8 行彼此严格递减、与曲线完全吻合的好读数**全部出界陪葬。

    ⚠️ 首尾一起坏时区间判据是**没救的**（它的两个端点就是那两行），而曲线不看端点，
    看的是名次相邻的历史点 —— 这一条只有曲线拦得住，这也是它存在的全部理由。
    """
    history, anchor, rank, top = _scan(6)
    ranks, scores = _screen(rank, top=top, step=10.0)
    # 首尾双双丢首位：11,2xx → 1,2xx。中间那几行照实读出。
    scores[0] /= 10
    scores[-1] /= 10

    verdict = judge_scores(scores, anchor=anchor, ranks=ranks, history=history)
    trusted, reasons = verdict.trusted, verdict.reasons

    assert trusted[0] is None, "丢了首位的首行必须被丢"
    assert trusted[-1] is None, "丢了首位的末行必须被丢"
    hurt = [
        (index, scores[index], reasons[index])
        for index in range(1, len(trusted) - 1)
        if trusted[index] is None
    ]
    assert not hurt, f"完好的中间几行被误伤了：{hurt}"


def test_the_curve_still_catches_a_misread_inside_the_bracket() -> None:
    """放宽不等于放行：屏内区间框得住、却偏离曲线的读数照样要丢。

    ⚠️ 这一条是上面那条的**反面钉子**。上面要的是「别误伤」，容易一路放宽到「什么都
    信」；而 6 → 5 那类误读（生产上最多的一类，实测 5410 / 5400 / 5480 对着 64xx 的
    参照）偏离约 16%，屏内首尾一旦也偏就框不住它，只有曲线认得出来。
    """
    history, anchor, rank, top = _scan(6)
    ranks, scores = _screen(rank, top=top, step=10.0)
    # 首行读成 6 → 5 那一类（偏低约 10%），末行跟着偏 —— 区间于是框得住整屏。
    scores[0] *= 0.90
    scores[-1] *= 0.88

    trusted = judge_scores(scores, anchor=anchor, ranks=ranks, history=history).trusted

    assert trusted[0] is None, "偏离曲线 10% 的首行被放行了"
    assert trusted[-1] is None, "偏离曲线 12% 的末行被放行了"


def test_a_hole_in_the_fit_window_makes_the_curve_keep_quiet() -> None:
    """窗口里隔着大洞时曲线必须闭嘴 —— 否则级联误伤换个机制又回来了。

    ⚠️ 这一条是**放宽的边界**。连着几屏被丢光之后，历史就剩「几个旧点 + 一个新点」，
    而那个紧密小簇会把斜率中位数带到它自己那一段上去（四个点里三个挤在一块，两两
    组合就占了 6 对中的 3 对）。据此去否决好读数，正是这一版要治的那个病。

    生产数据分档（2026-09-03，638 个实测点前向留一验证）：窗口内空洞 ≤4 时超 3% 的
    只占 7–10%，5–8 时是 47.6% —— 断得很干脆，`CURVE_MAX_GAP` 取 4 就是从这来的。
    """
    dense = [(1000 + offset, 20_000.0 - 10.0 * offset) for offset in range(4)]
    assert curve_reference(dense, 1004, require_both_sides=False) is not None, (
        "名次连续的窗口反倒不表态了，那这条用例测不到它该测的东西"
    )

    holed = [*dense[:3], (1000 + CURVE_MAX_GAP + 4, 19_000.0)]
    assert curve_reference(holed, 1010, require_both_sides=False) is None, (
        "窗口里有大洞却照样给了参照 —— 那个参照的斜率是那三个旧点自己的"
    )
