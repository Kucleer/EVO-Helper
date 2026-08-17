"""挑今晚打谁：**先按读数时间取一池，再在池内按军力截断，最后按距离出击。**

用户口径（2026-08-18）敲定的五步，**次序本身就是判据**，不能重排：

| 步 | 做什么 | 住在哪 |
|---|---|---|
| 1 | 剔除 24h 内已攻击的 + 本轮已走完的 | `application.mission_scheduler._military_candidates` |
| 2 | 只保留**有军力读数**的目标 | `with_a_military_reading` |
| 3 | 按读数时间倒序取前 500（可配）＝**时间池** | `newest_readings_first` |
| 4 | 在时间池里按军力降序取前 100（可配）＝**军力截断** | `strongest_within` |
| 5 | 这 100 个按距离分配出发星、由近到远出击 | `military_attack.assign_by_capacity_and_distance` |

第 2--4 步合起来就是 `recent_then_strongest`；再接上单出发点的第 5 步就是
`strongest_then_nearest`。

## 第 1 步为什么必须在最前

若先拿前 N 再排除已攻击目标，首批刚好都打过时军力任务会把候选池缩成空集，
较低排名、从未攻击的目标永远轮不到。理由整段写在 `_military_candidates` 上。

## 第 2 步：从没上过军力榜的目标不再攻击（2026-08-18 用户决定）

⚠️ **这一条推翻了「没有分数的按距离补位」那一版。** 旧设计的依据是一句错话
——「凡是没被榜单扫到过的 bot 就永远不会被攻击，而那正是库里最多的一批」
——那个「最多」是把非 bot 的行也算进去数出来的。实测生产库：**从未上过军力榜
的 bot 有 628 个，占 bot 总数（3604）的 17.4%**，不是「最多的一批」。

放弃这 17.4% 换来的是「军力优先」这个模式真的成立：补位不参与按军力排序，
补位一多，这条链路就退化成「按距离随便打」。整段善后写在
`domain.military_attack` 的模块头上。

## 第 3 步：时间池——这一版的核心改动

**`score_max_age_hours` 从「过滤器」降级成「提示信号」。**

旧行为是把超期目标整批滤掉。于是「一个新鲜分数都没有」时，池子退化成
「按距离补位、军力完全不参与」——2026-08-17 晚上实机连续 2.5 小时就是这个状态，
而页面上只是一句不痛不痒的话。

改成「按读数时间倒序取前 500」之后：**最新的 500 个总是存在**，哪怕它们全部超期，
第 4 步的军力截断照样成立。有效期不再挡任何目标，只用来在日志和页面上说一句
「这批分数已经超期多久」。

排序键是**读数时间**而不是军力：军力最高的那些恰恰读数最旧（榜单按军力降序排、
扫描也从上往下读），拿军力当排序键等于把时间池变成第二道军力截断，
「用多新的数据」这件事就没人管了。

## 第 4 步：军力必须是**截断**，不能是**排序**

第 5 步按距离重排会把排序结果整个抹掉，所以军力只有一次机会生效，就是这一刀。
实测用户一天只派约 35 发，而时间池 500 个——只有 7% 会被打到，**「谁被打」
完全由这一刀决定**。填成 ≥ 可用候选数就等于没截（2026-08-18 之前 `top_n` 被填成
500 而可用候选只有 591，正是这个状态）。

⚠️ 这里刻意**没有**档位阈值。先前写过一版按军力分档（>100K / 20K–100K / …），
边界取自 2026-08-15 那批数据的分位数，而**那批数据是脏的**：30 个军力值因为丢
小数点飞到 10 万以上（`17.73K` 读成 `1773K`），又通过插值传染了 12 个。取前 N 名
不需要任何阈值——这正是它比分档结实的地方。而且军力值每周一 UTC+0 随机刷新
（用户口径 2026-08-14），任何写死的阈值下一周都要重标。

## 第 5 步：先打近的

用户口径（2026-08-18）明确选择「先打近的」。近目标往返 20--30 分钟、跨银河
2.6 小时（实测，见 `domain.distance`），同样的航线数先打近的能派十几发。

## 读不出军力值的排最后（`strongest_first`）

**不是把它们当成 0 分**：0 分是一个**读到的事实**（榜单上真的有 0 分的行），
而 None 是「不知道」。混在一起就等于把「没数据」伪装成「数据是 0」——
这个仓有一条硬规矩：猜出来的数不许长得像量出来的。

第 2 步之后，`None` 那一档其实已经不会走到这里了；判据留着，因为
`strongest_first` 是通用的排序，而「不知道不等于 0」在哪里都成立。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from evo_helper.domain.distance import distance_key
from evo_helper.domain.models import Coordinate

#: 军力截断：在时间池里按军力取前几个。用户口径（2026-08-18）：100。
#:
#: 这个数与航路数是两件事：航路数（`scheduler_config.fleet_line_limit`）决定
#: **同时**在飞几发，而这个数决定**这一轮谁有资格被打**。
#:
#: ⚠️ **它是一道截断，不是一次排序。** 第 5 步按距离重排，所以军力只在这里生效
#: 一次。**填成大于等于可用候选数就完全失效**——2026-08-18 之前它被填成 500，
#: 而可用候选只有 591，等于军力压根没参与。
#:
#: 分类：**运维旋钮**，可在任务参数里改（`top_n`）。调小 = 只打最强的，代价是
#: 可选距离变差；调大 = 军力优先被稀释。没有唯一正确答案。
TOP_BY_MILITARY = 100

#: 时间池大小：按读数时间倒序取前几个。用户口径（2026-08-18）：500。
#:
#: 它回答的是「**用多新的军力数据**」，与 `TOP_BY_MILITARY`（「只打多强的」）
#: 各管一件事，**必须分开配**。合成一个数的话，想放宽数据新鲜度就只能连带把
#: 攻击面一起放宽，反过来也一样。
#:
#: 分类：**运维旋钮**，可在攻击配置页上改（`military_attack_config.military_time_pool`）。
#: 调大 = 用到更旧的军力数据、排序可能不准；调小 = 可选面变窄。
DEFAULT_TIME_POOL = 500

#: 军力读数「算不算新」的默认门槛：**一轮军力榜扫描时长的约 2 倍**。
#:
#: ⚠️ **2026-08-18 起它不再挡任何目标。** 它曾经是硬判据（分数过期的整批跳过），
#: 而那正是 2026-08-17 晚上攻击停摆 2.5 小时的成因：一个新鲜分数都没有时，
#: 池子退化成「军力完全不参与」。现在超期与否只影响日志和页面上那句
#: 「这批分数已经超期多久」，选靶交给时间池（`DEFAULT_TIME_POOL`）。
#:
#: 取值仍然写在这里，因为那句提示得有个基准。实测生产库的扫描速率
#: （`mission_runs` 对 `bot_targets.military_score_at_utc`）：2026-08-17 10:18 那一轮
#: 58.1 分钟采 946 个（16.3 个/分）、09:31 那一轮 41.4 分钟采 361 个（8.7 个/分）。
#: 用户计划一轮扫 1000 个，也就是**一轮约 61 分钟**，取两倍。
DEFAULT_SCORE_MAX_AGE = timedelta(hours=2)


@dataclass(frozen=True)
class ScoredTarget:
    """一个候选目标：坐标 + 军力值（可能没有）。"""

    coordinate: Coordinate
    military_score: float | None = None
    #: 军力值读到的时刻；None 表示榜单从未见过，不伪造「新鲜」。
    military_score_at_utc: datetime | None = None


def _coordinate_key(target: ScoredTarget) -> tuple[int, int, int]:
    """并列时的定序键。**每一步都要有它**：次序不定的话，同一批目标每次挑出来的
    前 N 个可能不一样，而那会让「上一轮打到哪了」无从谈起，事后拿日志也对不上。
    """
    return (target.coordinate.galaxy, target.coordinate.system, target.coordinate.position)


def _reading_time(target: ScoredTarget) -> datetime:
    """读取时刻。调用前必须已按 `has_a_military_reading` 过滤过。"""
    moment = target.military_score_at_utc
    if moment is None:  # pragma: no cover - 上游已过滤，留着是为了别静默按 0 处理
        raise ValueError("没有读取时刻的目标不该走到时间池")
    return moment


def strongest_first(targets: Iterable[ScoredTarget]) -> list[ScoredTarget]:
    """按军力从强到弱排。读不出来的排最后（理由见模块头）。

    次序是**确定**的：军力相同时按坐标定序。
    """
    return sorted(
        targets,
        key=lambda target: (
            target.military_score is None,
            -(target.military_score or 0.0),
            *_coordinate_key(target),
        ),
    )


def has_a_military_reading(target: ScoredTarget) -> bool:
    """**第 2 步的判据**：这一条有没有一份能用的军力读数。

    要求**分数和读取时刻两样都在**：

    - 没有分数 → 从没上过军力榜。用户 2026-08-18 决定这一档不再攻击（实测 628 个，
      占 bot 总数 3604 的 17.4%），理由在模块头。
    - 有分数却说不清什么时候读的 → 进不了时间池：时间池按读数时间排序，
      一个没有时刻的目标在那把尺子上没有位置。把它当成「很旧」或者「很新」
      都是在编一个没量到的数。
    """
    return target.military_score is not None and target.military_score_at_utc is not None


def with_a_military_reading(targets: Iterable[ScoredTarget]) -> list[ScoredTarget]:
    """**第 2 步**：只留下有军力读数的。次序保持传入的次序（排序是后面两步的事）。"""
    return [target for target in targets if has_a_military_reading(target)]


def newest_readings_first(
    targets: Iterable[ScoredTarget], *, take: int = DEFAULT_TIME_POOL
) -> tuple[ScoredTarget, ...]:
    """**第 3 步**：按读数时间倒序取前 `take` 个，也就是**时间池**。

    ⚠️ **排序键是读数时间，不是军力。** 换成军力的话这一步就变成第二道军力截断，
    而「用多新的数据」这件事再没人管——最新的那批读数会被军力最低的那些顶掉，
    因为榜单按军力降序扫，先读到的（军力最高的）读数最旧。

    ⚠️ **这一池总是非空**（只要第 2 步还剩东西），哪怕全部超期。这正是它替掉
    「超期整批跳过」的原因：2026-08-17 晚上实机连续 2.5 小时一个新鲜分数都没有，
    旧实现于是把军力整个踢出了选靶。

    并列（同一时刻读到的，扫描一屏之内很常见）按坐标定序。
    """
    if take < 1:
        return ()
    rated = sorted(with_a_military_reading(targets), key=_coordinate_key)
    rated.sort(key=_reading_time, reverse=True)
    return tuple(rated[:take])


def strongest_within(
    targets: Iterable[ScoredTarget], *, take: int = TOP_BY_MILITARY, max_score: float | None = None
) -> tuple[ScoredTarget, ...]:
    """**第 4 步**：在给进来的这一池里按军力降序取前 `take` 个。

    ⚠️ **这是一道截断，不是一次排序。** 第 5 步按距离重排，排序结果会被整个抹掉，
    所以军力只有这一次机会生效。填成 ≥ 池子大小就等于没截。

    `max_score` 是**上限**：军力高于它的一律不进池。用户 2026-08-14 要求
    「军力确实要设置上限」——太强的目标不是当前预设打得动的，派过去只是白烧
    一次配额和一趟往返。默认 `None` = 不设上限。

    ⚠️ **这个上限目前是空转的，别据此推断什么。** 用户口径（2026-08-17）：
    「目前的 bot 的军事能力不存在太强这个可能性……已知周一刷新当日 bot 的最高
    战力只有 70 多 K」。留着不删：哪天 bot 变强了它就有用，而重新长出一条上限
    比留着一条暂时不生效的贵得多。

    ⚠️ **上限只挡「太强」，不挡「读不出来」**：`military_score is None` 的目标在
    这里照样留下。它们在第 2 步就已经出局了，这里不必也不该再判一次——
    「不知道多强」从来不构成「一定太强」。
    """
    if take < 1:
        return ()
    affordable = [
        target
        for target in targets
        if max_score is None or target.military_score is None or target.military_score <= max_score
    ]
    return tuple(strongest_first(affordable)[:take])


def recent_then_strongest(
    targets: Iterable[ScoredTarget],
    *,
    time_pool: int = DEFAULT_TIME_POOL,
    take: int = TOP_BY_MILITARY,
    max_score: float | None = None,
) -> tuple[ScoredTarget, ...]:
    """**第 2--4 步合起来**：有读数的 → 读数最新的 `time_pool` 个 → 其中最强的 `take` 个。

    三步各是一个独立的函数，这里只负责把它们串起来——**串的次序就是判据**，
    每一步的理由写在各自的 docstring 与模块头上。
    """
    return strongest_within(
        newest_readings_first(targets, take=time_pool), take=take, max_score=max_score
    )


def score_is_fresh(target: ScoredTarget, *, now: datetime, max_age: timedelta) -> bool:
    """这一条的军力**分数**读得够不够新。判据**逐目标**，不看池子里别人的读数。

    ⚠️ **2026-08-18 起这只是一个提示信号，它不挡任何目标。** 选靶由时间池
    （`newest_readings_first`）决定；这个判据只用来在日志和页面上说清
    「正要打的这一批分数已经超期多久」。当成过滤器用的那一版，在
    「一个新鲜分数都没有」的夜里会把军力整个踢出选靶（2026-08-17 实机 2.5 小时）。

    判据逐目标而不是拿 `min(整池)` 判整池：一条陈旧记录就让整池被判过期，于是
    警告永远在响，而响的时候并不知道**正要打的那个**新不新。实机 2026-08-17：
    日志里写着「最旧读数 2026-08-14 21:58」（三天前的某一条），而当时正要打的
    `4:293:6` 读数是当日 01:50、攻击发生在 05:28——超期 3.6 小时。

    没有分数、或者读到过分数却没有读取时刻的，在这里恒为假：说不清什么时候读的
    分数谈不上新不新。这两档在第 2 步（`has_a_military_reading`）就已经出局了。
    """
    if target.military_score is None:
        return False
    scanned_at = target.military_score_at_utc
    return scanned_at is not None and now - scanned_at < max_age


@dataclass(frozen=True)
class FreshnessSplit:
    """一批候选按「有没有读数、读数新不新」分成的三堆。次序一律保持传入的次序。

    ⚠️ **2026-08-18 起这张表只用来记账，不再决定谁出局。** 三堆的去向变了：

    | 堆 | 从前 | 现在 |
    |---|---|---|
    | `rated`（有分数且新鲜） | 唯一参与按军力排序的 | 进时间池 |
    | `expired`（有分数但超期） | **整堆跳过** | **照样进时间池**，只是在日志里被点名 |
    | `unrated`（完全没有分数） | 按距离补位，照打 | **整堆出局**（用户 2026-08-18 决定） |

    两处都反过来了，两处都是踩出来的：超期整批跳过让 2026-08-17 晚上停摆 2.5 小时；
    没有分数的按距离补位则让「军力优先」在补位多的夜里退化成「随便打」。
    """

    #: 有分数、且读数还在有效期内。
    rated: tuple[ScoredTarget, ...]
    #: 完全没有分数（从没上过军力榜，或者那一格没解析出来）。**不再参与攻击。**
    unrated: tuple[ScoredTarget, ...]
    #: 有分数但已经超期，或者说不清什么时候读的。**照样参与**，只是会被日志点名。
    expired: tuple[ScoredTarget, ...]


def split_by_freshness(
    targets: Iterable[ScoredTarget], *, now: datetime, max_age: timedelta
) -> FreshnessSplit:
    """把候选分三堆。**只分堆、只为了记账**，选靶不看它。

    ⚠️ **别把它接回选靶去。** 它曾经是选靶闸门（`expired` 整堆跳过），而这一版
    刻意把那件事交给了时间池：时间池永远拿得出最新的 N 个，超期与否只影响
    「日志里怎么说」。接回去等于把 2026-08-17 那晚的停摆重新装上。
    """
    rated: list[ScoredTarget] = []
    unrated: list[ScoredTarget] = []
    expired: list[ScoredTarget] = []
    for target in targets:
        if target.military_score is None:
            unrated.append(target)
        elif score_is_fresh(target, now=now, max_age=max_age):
            rated.append(target)
        else:
            expired.append(target)
    return FreshnessSplit(tuple(rated), tuple(unrated), tuple(expired))


def strongest_then_nearest(
    targets: Iterable[ScoredTarget],
    origin: Coordinate,
    *,
    time_pool: int = DEFAULT_TIME_POOL,
    take: int = TOP_BY_MILITARY,
    max_score: float | None = None,
) -> tuple[Coordinate, ...]:
    """第 2--5 步的单出发点版本：`recent_then_strongest` 之后按离 `origin` 由近到远排。

    多出发点那条路走 `domain.military_attack.assign_by_capacity_and_distance`——
    前四步一模一样，只有第 5 步换成按航线预算分配。**前四步只能有这一份实现**，
    各写一遍的结果是「命令行按新口径算、页面按旧口径显示」，而那种不一致
    2026-08-15 已经撞过一次。
    """
    pool = recent_then_strongest(targets, time_pool=time_pool, take=take, max_score=max_score)
    return tuple(
        target.coordinate
        for target in sorted(pool, key=lambda item: distance_key(item.coordinate, origin))
    )


__all__ = [
    "DEFAULT_SCORE_MAX_AGE",
    "DEFAULT_TIME_POOL",
    "TOP_BY_MILITARY",
    "FreshnessSplit",
    "ScoredTarget",
    "has_a_military_reading",
    "newest_readings_first",
    "recent_then_strongest",
    "score_is_fresh",
    "split_by_freshness",
    "strongest_first",
    "strongest_then_nearest",
    "strongest_within",
    "with_a_military_reading",
]
