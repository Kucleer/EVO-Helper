"""舰队数量的读取判据：合计对不上就不算数。

战斗详情页独立给出双方的「单位」总数，回放页给出逐舰种的数量。
**两者必须对上**——这是个单向校验，和坐标那边「请求 vs 面板读回」同构：
读错凑不出正确的合计，而合计一旦对上，逐行的数就没有可疑的余地了。

为什么需要这条判据：这个游戏的数字字体会把相邻笔画粘在一起，
`117` 读成 `17`、`11` 读成 `1`、`39` 读成 `33`。没有校验的话，
这些错误会一路"成功"入库——实测过一次，守方合计 247 存成了 144，
全程零报错，是靠人肉比对才发现的。

怎么读到对为止（用户给的办法，实测有效）：
背景那层半透明水印（`TOTAL CREW`、`personnel`）是主要干扰源，
**轻微拖动页面会改变它与文字的叠合关系**，等于换一个独立样本。
实测同一份战报拖动六次，`39` 有三次读对；但 `11` 六次全错，
所以光靠拖动不收敛——还要同时换 OCR 配方。两个轴一起扫。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

#: 数量列的配方阶梯：放大倍数与重采样。最近邻保住相邻笔画之间的缝，
#: LANCZOS 会把它插值糊掉；两种都留，因为它们读对的样本并不重合。
COUNT_RECIPES: tuple[tuple[int, str], ...] = (
    (4, "lanczos"),
    (3, "lanczos"),
    (5, "lanczos"),
    (4, "nearest"),
    (3, "nearest"),
)

#: 一份战报最多重拍几次。拖动改变的是背景叠合，不是内容，所以多拍无害；
#: 但也不能无限拍——读不出来要让人知道，而不是卡在那里。
MAX_RECAPTURES = 8


@dataclass(frozen=True)
class FleetReading:
    """一次读数，以及它与期望总数是否吻合。"""

    counts: tuple[int, ...]
    expected_total: int
    recipe: tuple[int, str]
    attempt: int

    @property
    def total(self) -> int:
        return sum(self.counts)

    @property
    def confirmed(self) -> bool:
        """合计对上才算数。空读数不算——0 == 0 不是证据。"""
        return bool(self.counts) and self.total == self.expected_total


class FleetCountsUnresolved(RuntimeError):
    """拍了几轮、换了几套配方都没对上合计。

    **不要退而求其次存一个最接近的**：数量是舰队时间线做差异的依据，
    存一个差不多的比不存更坏——它看起来像数据，不会有人再去核。
    """


def read_until_total(
    *,
    sample: Callable[[tuple[int, str]], Sequence[int]],
    expected_total: int,
    nudge: Callable[[int], None],
    recipes: Sequence[tuple[int, str]] = COUNT_RECIPES,
    max_recaptures: int = MAX_RECAPTURES,
) -> FleetReading:
    """反复读，直到某一次的合计等于 `expected_total`。

    每一轮把所有配方都试一遍；都不中就 `nudge` 一下换个背景再来。
    `nudge` 收到的是本轮的位移量——正负交替、幅度不同，
    连续同向拖会把内容推出可视区。
    """
    if expected_total <= 0:
        raise ValueError(f"期望总数必须为正，收到 {expected_total}")
    seen: list[FleetReading] = []
    for attempt in range(max_recaptures):
        if attempt:
            nudge(nudge_offset(attempt))
        for recipe in recipes:
            reading = FleetReading(
                counts=tuple(sample(recipe)),
                expected_total=expected_total,
                recipe=recipe,
                attempt=attempt,
            )
            if reading.confirmed:
                return reading
            seen.append(reading)
    best = max(seen, key=lambda r: -abs(r.total - expected_total), default=None)
    raise FleetCountsUnresolved(
        f"读了 {len(seen)} 次都没对上合计 {expected_total}；"
        f"最接近的一次是 {best.total if best else '（无）'}"
    )


def nudge_offset(attempt: int) -> int:
    """第 `attempt` 轮的拖动位移：正负交替，幅度小步变化。

    一直朝一个方向拖会把内容推出可视区；幅度也要变，
    同样的位移大概率复现同样的叠合、也就复现同样的错误。
    """
    magnitude = 14 + (attempt % 3) * 6
    return magnitude if attempt % 2 else -magnitude


@dataclass(frozen=True)
class FleetRow:
    """一行舰种数量，以及它是否可信。

    `uncertain` 为真时，前端应在数字旁标 `*`——那一行的数没有把握，
    但**整支舰队的总数是准的**（总数另有来源，见 `ReconciledFleet.total`）。
    """

    ship: str
    count: int
    uncertain: bool


@dataclass(frozen=True)
class ReconciledFleet:
    """一份战报里某一方的舰队读数。

    `total` 来自战斗详情页的「单位」，与逐行读数是**两个独立来源**；
    逐行读不准时，总数仍然可信，所以它单独存、单独展示。
    """

    rows: tuple[FleetRow, ...]
    total: int

    @property
    def rows_total(self) -> int:
        return sum(row.count for row in self.rows)

    @property
    def reconciled(self) -> bool:
        return self.rows_total == self.total

    @property
    def uncertain_rows(self) -> int:
        return sum(1 for row in self.rows if row.uncertain)


def reconcile_counts(
    candidates: Sequence[tuple[str, dict[int, int]]], expected_total: int
) -> ReconciledFleet:
    """按票数选每行的数，合计对不上时把**读数有过分歧的行**标为不确定。

    **不确定性不能由票数高低判定。** 实测：`11` 被读成 `1`、`17` 被读成 `7`，
    两者都是 100% 一致的误读；而只有 50% 一致的那行反倒是对的。
    按票数标 `*` 会给对的行标星、给错的行放行——比不标更误导。

    也不能只标「能独力补平差额」的那一行：实测差额是 11，而没有任何单行
    换个候选值能正好补 11（错的是三行，不是一行）。判据太窄就会退化成全标。

    所以规则是：合计对上 → 全部采信；对不上 → **凡是自己读出过第二个值的行都标**。
    每一遍都读成同一个数的行留作可信——那是现有证据里最扎实的一档。
    实测那份战报：4 个未标的行**全部正确**，3 个真错的行**全部被标**，
    另有 3 行是虚警。宁可虚警，不可漏标。
    """
    winners = [(ship, _plurality(votes)) for ship, votes in candidates]
    rows_total = sum(count for _ship, count in winners)
    if rows_total == expected_total:
        return ReconciledFleet(
            tuple(FleetRow(ship, count, uncertain=False) for ship, count in winners),
            expected_total,
        )
    return ReconciledFleet(
        tuple(
            FleetRow(ship, count, uncertain=len(votes) > 1)
            for (ship, count), (_s, votes) in zip(winners, candidates, strict=True)
        ),
        expected_total,
    )


def _plurality(votes: dict[int, int]) -> int:
    """票最多的那个值；平票取小的，让结果与读取顺序无关。"""
    if not votes:
        return 0
    return max(sorted(votes), key=lambda value: votes[value])


def pick_count(votes: Mapping[str, int]) -> str:
    """从多次读数里选一个值。**掉了字的让位于更全的候选。**

    这个字体的失败模式是恒定的：**只会漏掉笔画，从不凭空多字**。
    实测 `74` 读成 `4` 21 次、读对 15 次；`210` 与 `10` 各 15 次打平。
    单纯多数票会稳定选错短的那个——而短的恰恰是被漏掉字的那个。

    所以先把「能由某个更长候选漏掉字得来」的票并给那个更长的，再取多数。
    这条推理与坐标核对同源：错误只会漏读，不会凭空多读。

    漏掉的字有两种，都要认：

    1. **前面的字掉了** → 短的是长的**后缀**（`74` → `4`、`5.73K` → `73K`）。
    2. **小数点掉了** → 同一串数字，差整整 100 倍（`1.22K` → `122K`）。

    第 2 条是 2026-08-11 实机上一次真实事故：2:48:12 的守方「单位」实为
    `1.22K`（1220 艘），入库成了 122000，当时被分档判成 `8K+`。分档已经删了
    （bot 一律 BBB），但这一笔仍然要紧，只是后果换了地方：**「单位」是算胜负的
    四个输入之一**（剩余 = 单位 − 损失，`domain.battle_outcome`），差 100 倍
    足以把一场平局记成全歼——而平局与否决定这个坐标要不要再挨一发。
    那个小圆点只有几个像素，背后还压着水印，是这一行上最容易丢的一笔。

    两条都是**单向**的：带点的候选吸收去掉点之后与它相同的候选，反过来不成立。
    方向来自同一个前提——字体不会凭空多出一个点，正如它不会凭空多出一位数字。
    """
    if not votes:
        return ""
    folded: dict[str, int] = {}
    for text, count in votes.items():
        longer = [other for other in votes if other != text and _may_have_dropped_to(other, text)]
        target = max(longer, key=len) if longer else text
        folded[target] = folded.get(target, 0) + count
    return max(sorted(folded), key=lambda value: folded[value])


def _may_have_dropped_to(full: str, short: str) -> bool:
    """`short` 是不是 `full` 漏掉字之后的样子。判据见 `pick_count`。

    **不放宽成「`short` 是 `full` 的子序列」。** 那条会把 `11` 并进 `1.17K`
    （1、1 确实按顺序出现在里面），把一个 11 艘的读数说成 1170 艘。
    只认实测见过的两种漏法。
    """
    return full.endswith(short) or full.replace(".", "") == short


def row_grid(first_top: int, pitch: int, rows: int) -> list[int]:
    """按等距网格排出每一行的顶端。

    逐行检测靠不住：实测 17 行的表检出 18 行——`钛能守卫者` 整行没被认出来，
    位置被一个 `sk` 碎片顶替，之后所有行的索引都错开一位。
    行距本身很规整（实测 22px），拿第一行加行距推反而稳。

    只用检测结果定**第一行位置和行距**，不用它定每一行。
    """
    if pitch <= 0:
        raise ValueError(f"行距必须为正，收到 {pitch}")
    return [first_top + index * pitch for index in range(rows)]
