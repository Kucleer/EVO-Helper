"""挑今晚打谁：**先按读数新鲜度划一条线，再在线内按军力截断，最后按距离出击。**

用户口径（2026-08-18）敲定的五步，**次序本身就是判据**，不能重排：

| 步 | 做什么 | 住在哪 |
|---|---|---|
| 1 | 剔除 24h 内已攻击的 + 刚撞过保护期的 + 本轮走完的 | `mission_scheduler._military_candidates` |
| 2 | 只保留**有军力读数**的目标 | `with_a_military_reading` |
| 3 | 只保留读数落在**有效期窗口**内的 | `within_score_window` |
| 4 | 窗口内按军力降序取前 100（可配）＝**军力截断** | `strongest_within` |
| 5 | 这 100 个按距离分配出发星、由近到远出击 | `military_attack.assign_by_capacity_and_distance` |

第 2--4 步合起来就是 `choose_by_military`；再接上单出发点的第 5 步就是
`strongest_then_nearest`。

## 第 1 步为什么必须在最前

若先拿前 N 再排除已攻击目标，首批刚好都打过时军力任务会把候选池缩成空集，
较低排名、从未攻击的目标永远轮不到。理由整段写在 `_military_candidates` 上。

⚠️ **「刚撞上过保护期」和「24h 内已攻击」是同一档，必须并排站在第 1 步里。**
它是 2026-08-18 加的：游戏的 8 小时保护期任何人打过都会触发、只能撞上了才知道，
而在它落库之前，同样的四个目标会被每一轮反复挑中——实机一轮 11.5 分钟、一发没派，
下一轮一秒后照挑不误。把它挪到取前 N 之后，缩成空集那个失败模式会原样复发。

## 第 2 步：从没上过军力榜的目标不再攻击（2026-08-18 用户决定）

⚠️ **这一条推翻了「没有分数的按距离补位」那一版。** 旧设计的依据是一句错话
——「凡是没被榜单扫到过的 bot 就永远不会被攻击，而那正是库里最多的一批」
——那个「最多」是把非 bot 的行也算进去数出来的。实测生产库：**从未上过军力榜
的 bot 有 628 个，占 bot 总数（3604）的 17.4%**，不是「最多的一批」。

放弃这 17.4% 换来的是「军力优先」这个模式真的成立：补位不参与按军力排序，
补位一多，这条链路就退化成「按距离随便打」。整段善后写在
`domain.military_attack` 的模块头上。

## 第 3 步：窗口筛选——**这一版修掉的正是上一版的这一步**

### 上一版（PR #176）错在哪

上一版这一步是「按读数时间倒序取前 500 个」，叫「时间池」。它看起来只是
「优先用新数据」，实际是一道**反向的军力截断**：

**军力榜是从强到弱扫的，所以「读数最新」系统性地等价于「军力最弱」。**
生产实测（2026-08-18 07:33--08:53 那一轮扫描，按读数时刻分段）：

| 读数时段 | 个数 | 均值 | 最高 |
|---|---|---|---|
| 07:40 | 190 | 31,756 | 262,899 |
| 07:50 | 225 | 19,108 | 21,270 |
| 08:00 | 224 | 13,806 | 17,510 |
| 08:10 | 236 | 8,045 | 10,560 |
| 08:20 | 181 | 6,756 | 8,660 |
| 08:30 | 206 | 4,301 | 5,600 |
| 08:40 | 231 | 2,938 | 3,250 |
| 08:50 | 98 | 2,616 | 2,720 |

**单调下降。** 于是「取最新 500 个」＝「取最后扫到的 500 个」＝「取最弱的
500 个」；再在这批里按军力取前 200，选出来的是 3,200~5,600。实机 2026-08-18
09:00 那 8 发的军力就是 3,270 / 5,590 / 4,835 / 3,360 / 4,390 / 4,430 / 5,740 / 5,420
——「军力优先」这条链路选出来的是全库最弱的那一档。

### ⚠️ 窗口筛选和「取最新 N 个」不是一回事，别再判成等价

**这两件事只差一层，而那一层就是全部：**

- **「取最新 N 个」按名次截断。** 名次由读数时间排出来，而读数时间和军力强相关
  （见上表），所以这一刀**带选择偏差**：它挑走的恰好是军力最弱的那一段。
- **窗口筛选按时间划一条线。** 线的位置由 `max_age` 定，与「这一批有多少个」
  「谁排第几」都无关。**同一轮扫描出来的目标要么整批在线内、要么整批在线外**，
  强的弱的一视同仁——所以它**不带选择偏差**。

上一版就是把这两件事判成了等价才写成那样的。这段话留在这里，是为了下一个人
不要再判一次。

### 窗口内不足时：**放弃窗口**，不是「按时间往下补」

```
③ 只保留读数在 max_age 窗口内的
   ├─ 窗口内的数量 ≥ 军力截断数 K → 就用窗口内这批，进第 4 步
   └─ 窗口内不足 K → 放弃窗口，在「全部有读数的目标」里按军力截断，并大声告警
```

**为什么不足时不按时间往下补**：往下补捞到的正是刚出窗口那一批，而按上表，
那一批恰恰是最弱的——补下去等于把上一版的缺陷换个地方原样复发。放弃窗口之后
按军力截断，至少拿到的是**全库最强**的那一批。

**放宽必须大声说出来。** 用户口径（2026-08-18）：「今晚这件事的真正问题不是
『用了旧数据』，而是**用了旧数据却没人告诉你**——你是从攻击日志里一条一条对
出来的」。所以 `MilitaryChoice.widened` 是这一步的一等结果，不是可选的附注：
`application` 据它打 WARNING、页面据它显示 `TaskStatus.WIDENED_SCORE_WINDOW`。

## 第 4 步：军力必须是**截断**，不能是**排序**

第 5 步按距离重排会把排序结果整个抹掉，所以军力只有一次机会生效，就是这一刀。
实测用户一天只派约 35 发，而窗口内常有一千多个——只有百分之几会被打到，
**「谁被打」完全由这一刀决定**。填成 ≥ 可用候选数就等于没截（2026-08-18 之前
`top_n` 被填成 500 而可用候选只有 591，正是这个状态）。

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

#: 军力截断：在窗口内按军力取前几个。用户口径（2026-08-18）：100。
#:
#: 这个数与航路数是两件事：航路数（`scheduler_config.fleet_line_limit`）决定
#: **同时**在飞几发，而这个数决定**这一轮谁有资格被打**。
#:
#: ⚠️ **它是一道截断，不是一次排序。** 第 5 步按距离重排，所以军力只在这里生效
#: 一次。**填成大于等于可用候选数就完全失效**——2026-08-18 之前它被填成 500，
#: 而可用候选只有 591，等于军力压根没参与。
#:
#: ⚠️ **它同时是第 3 步「够不够」的那把尺子**：窗口内的目标数 < 这个数时窗口
#: 会被放弃（见模块头第 3 步）。所以调大它不只是「多打几个」，也在让放宽更容易
#: 触发——两件事同一个数，是因为它们问的本来就是同一句话：「这一轮要挑出几个」。
#:
#: 分类：**运维旋钮**，可在任务参数里改（`top_n`）。调小 = 只打最强的，代价是
#: 可选距离变差、且窗口更不容易被放弃；调大 = 军力优先被稀释、放宽更常触发。
#: 没有唯一正确答案。
TOP_BY_MILITARY = 100

#: 军力读数「算不算新」的门槛，也就是第 3 步那扇**窗口**的宽度。
#: 默认取**一轮军力榜扫描时长的约 2 倍**。
#:
#: ⚠️ **2026-08-18 起它重新是一道真的筛选器。** 它的语义换过三次，别按任何一个
#: 旧版本理解它：
#:
#: 1. 最早叫 `rescan_after_hours`，只是一句提示，照样拿旧读数派遣；
#: 2. 2026-08-17 改成硬判据「超期的整批跳过」——一个新鲜分数都没有的夜里
#:    军力被整个踢出选靶，实机连续停摆 2.5 小时；
#: 3. PR #176 又把它降级成提示，选靶交给「时间池」——那一版把最弱的一批选了
#:    出来（理由整段在模块头第 3 步）；
#: 4. **现在**：它筛，但**筛不出足够的目标时会放弃窗口并告警**，而不是让这一轮
#:    空手。第 2 版那种停摆因此不会回来，第 3 版那种偏差也不会长出来。
#:
#: 取值实测生产库的扫描速率（`mission_runs` 对 `bot_targets.military_score_at_utc`）：
#: 2026-08-17 10:18 那一轮 58.1 分钟采 946 个（16.3 个/分）、09:31 那一轮 41.4 分钟
#: 采 361 个（8.7 个/分）。用户计划一轮扫 1000 个，也就是**一轮约 61 分钟**，取两倍。
#:
#: 分类：**运维旋钮**，可在任务参数里改（`score_max_age_hours`）。
DEFAULT_SCORE_MAX_AGE = timedelta(hours=2)

#: **游戏规则：被攻击过的目标进入 8 小时保护期。** 任何人打过都会触发。
#:
#: ⚠️ **这是事实，不是偏好项，别把它做成配置。** 它写在这里只为让下面那个默认值
#: 有出处；改它不会让结果「更适合我」，只会让它变错。原始出处与实机证据在
#: `game.pirate_ui.DIALOG_NO_MISSION`。
GAME_PROTECTION_HOURS = 8

#: 撞上保护期之后，这个坐标多久之内不再进候选池。**这是策略，不是上面那条规则。**
#:
#: ⚠️ **两者数值相同、身份完全不同，别合并成一个常量。** 我们只知道「在时刻 T 撞上
#: 了保护期」，**不知道保护期是什么时候开始的**——runner 撞上弹窗那一刻，保护可能
#: 已经走了 7 小时，也可能刚开始。所以按 `T + 8 小时` 排除会**过度排除**。
#:
#: **代价不对称，所以宁可过度排除**（用户口径 2026-08-18）：
#:
#: - 过度排除 → 少打几个目标，而候选池有 3000+ 个，损失可忽略；
#: - 排除不足 → 每个目标每轮白烧约 2.9 分钟鼠标时间（导航 + 开面板 + 撞弹窗 +
#:   退出），而鼠标时间是这台机器真正的瓶颈——一天只有 56% 在干活。
#:
#: 实机 2026-08-18 20:29 那一轮：四个目标全在保护期，11.5 分钟一发没派；20:41
#: 结算完，**一秒之后的下一轮又把同样的四个挑了出来**。
#:
#: 这是「没配置时」的默认值，页面上有一个框
#: （`military_attack_config.protection_exclusion_hours`），留空才走这里。
DEFAULT_PROTECTION_EXCLUSION = timedelta(hours=GAME_PROTECTION_HOURS)

#: 用户能填进去的保护期排除时长上界（小时）。
#:
#: 24 小时的依据：**超过 8 小时的每一分钟都是纯粹的过度排除**（保护期最长就是 8
#: 小时，从撞上那一刻起算最多还剩 8 小时），所以 8 以上纯属留给「宁可更保守」的
#: 余量。再往上就越过了 `bot_revisit_hours` 的默认值，两个旋钮开始争同一件事——
#: 「这个坐标为什么进不了候选池」会变成一道两选一的谜题，而排障时那正是要一眼看
#: 出来的东西。
PROTECTION_EXCLUSION_MAX_HOURS = 24


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
    - 有分数却说不清什么时候读的 → 进不了窗口：窗口按读数时刻划线，一个没有时刻的
      目标在那把尺子上没有位置。把它当成「很旧」或者「很新」都是在编一个没量到的数。
    """
    return target.military_score is not None and target.military_score_at_utc is not None


def with_a_military_reading(targets: Iterable[ScoredTarget]) -> list[ScoredTarget]:
    """**第 2 步**：只留下有军力读数的。次序保持传入的次序（排序是后面两步的事）。"""
    return [target for target in targets if has_a_military_reading(target)]


def score_is_fresh(target: ScoredTarget, *, now: datetime, max_age: timedelta) -> bool:
    """这一条的军力**分数**读得够不够新。判据**逐目标**，不看池子里别人的读数。

    判据逐目标而不是拿 `min(整池)` 判整池：一条陈旧记录就让整池被判过期，于是
    警告永远在响，而响的时候并不知道**正要打的那个**新不新。实机 2026-08-17：
    日志里写着「最旧读数 2026-08-14 21:58」（三天前的某一条），而当时正要打的
    `4:293:6` 读数是当日 01:50、攻击发生在 05:28——超期 3.6 小时。

    边界取「小于」：正好等于有效期的算超期。

    没有分数、或者读到过分数却没有读取时刻的，在这里恒为假：说不清什么时候读的
    分数谈不上新不新。这两档在第 2 步（`has_a_military_reading`）就已经出局了。
    """
    if target.military_score is None:
        return False
    scanned_at = target.military_score_at_utc
    return scanned_at is not None and now - scanned_at < max_age


def within_score_window(
    targets: Iterable[ScoredTarget], *, now: datetime, max_age: timedelta
) -> tuple[ScoredTarget, ...]:
    """**第 3 步**：只留下读数落在 `max_age` 窗口内的。次序保持传入的次序。

    ⚠️ **它按时间划一条线，不按名次截断——这是它和「取最新 N 个」的本质区别。**
    名次截断带选择偏差（军力榜从强到弱扫，「读数最新」系统性地等价于「军力最弱」，
    实测表在模块头第 3 步）；划线不带：线的位置只由 `max_age` 定，与「这一批有
    多少个」「谁排第几」无关，同一轮扫描出来的强弱目标一视同仁。

    **上一版就是把这两件事判成了等价**，于是「优先用新数据」实际选出了全库最弱的
    一批。别再判一次。

    ⚠️ **这一步可能筛空**，而那不是错——`choose_by_military` 负责在窗口内不够时
    放弃窗口并把这件事说出来。单独看这个函数时别给它补一个「至少留 N 个」的兜底：
    那个兜底就是「按时间往下补」，而往下补捞到的正是刚出窗口那批最弱的目标。
    """
    return tuple(target for target in targets if score_is_fresh(target, now=now, max_age=max_age))


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


@dataclass(frozen=True)
class MilitaryChoice:
    """第 2--4 步这一次的结果，**连「窗口有没有被放弃」一起带出来**。

    做成一个结构而不是只返回一个清单，是因为**放宽窗口这件事必须能被说出来**。
    用户口径（2026-08-18）：「今晚这件事的真正问题不是『用了旧数据』，而是
    **用了旧数据却没人告诉你**——你是从攻击日志里一条一条对出来的」。

    只返回 `selected` 的话，调用方能看见的只有「这一轮打了谁」，看不见
    「凭什么是这几个」；而这两次事故（2026-08-17 的停摆、2026-08-18 的选弱）
    的共同形状恰恰是**判据在背地里换了，页面和日志照旧**。
    """

    #: 第 2 步之后：有军力读数的那些。次序保持传入的次序。
    with_readings: tuple[ScoredTarget, ...]
    #: 第 3 步划出来的窗口内那批。**放宽与否都记**——「窗口内只有几个」正是
    #: 告警里最要紧的那个数。
    in_window: tuple[ScoredTarget, ...]
    #: 第 4 步真正参与截断的那一池：窗口够就是 `in_window`，不够就是 `with_readings`。
    considered: tuple[ScoredTarget, ...]
    #: 第 4 步之后：这一轮真的要打的。
    selected: tuple[ScoredTarget, ...]
    #: **这一轮用到了窗口外的读数吗。** 判据是「选中的这批里有没有超期的」，
    #: 而不是「有没有走放宽那条分支」——两者只在一种情形下不同：窗口内不足 K，
    #: 但库里本来就只有这些读数（`in_window == with_readings`）。那种情形下放弃
    #: 窗口一个目标都没多捞到，**没有用到旧数据，也就不该告警**。
    #: 日志说假话比不说更糟。
    widened: bool


def choose_by_military(
    targets: Iterable[ScoredTarget],
    *,
    now: datetime,
    max_age: timedelta,
    take: int = TOP_BY_MILITARY,
    max_score: float | None = None,
) -> MilitaryChoice:
    """**第 2--4 步合起来**：有读数的 → 窗口内的 → 其中最强的 `take` 个。

    三步各是一个独立的函数，这里只负责把它们串起来，外加**窗口内不足时那一个
    决定**——串的次序与那个决定就是判据，理由整段写在模块头第 3 步上。这里只
    重复最容易搞反的一条：

    **不足时是「放弃窗口」，不是「按时间往下补」。** 往下补捞到的正是刚出窗口
    那一批，而军力榜从强到弱扫，那一批恰恰是最弱的——补下去等于把 PR #176 的
    缺陷换个地方原样复发。放弃窗口后在全部有读数的目标里按军力截断，至少拿到的
    是全库最强的那一批。
    """
    with_readings = tuple(with_a_military_reading(targets))
    in_window = within_score_window(with_readings, now=now, max_age=max_age)
    # 「够不够」的尺子就是军力截断那个数：这一步存在的全部意义是给第 4 步备料，
    # 备够了就不必动窗口，备不够再放宽也不迟。
    considered = in_window if len(in_window) >= take else with_readings
    selected = strongest_within(considered, take=take, max_score=max_score)
    widened = any(not score_is_fresh(target, now=now, max_age=max_age) for target in selected)
    return MilitaryChoice(
        with_readings=with_readings,
        in_window=in_window,
        considered=considered,
        selected=selected,
        widened=widened,
    )


def strongest_then_nearest(
    targets: Iterable[ScoredTarget],
    origin: Coordinate,
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_SCORE_MAX_AGE,
    take: int = TOP_BY_MILITARY,
    max_score: float | None = None,
) -> tuple[Coordinate, ...]:
    """第 2--5 步的单出发点版本：`choose_by_military` 之后按离 `origin` 由近到远排。

    多出发点那条路走 `domain.military_attack.assign_by_capacity_and_distance`——
    前四步一模一样，只有第 5 步换成按航线预算分配。**前四步只能有这一份实现**，
    各写一遍的结果是「命令行按新口径算、页面按旧口径显示」，而那种不一致
    2026-08-15 已经撞过一次。

    `now` **没有默认值**，而 `max_age` 有：前者是一个事实，编一个出来（比如
    `datetime.now()`）会让调用方在测试里量不准、在实机里和调度器的时钟分家；
    后者是一条策略，代码里本来就有一个说得出理由的默认值。
    """
    chosen = choose_by_military(targets, now=now, max_age=max_age, take=take, max_score=max_score)
    nearest_first = sorted(chosen.selected, key=lambda item: distance_key(item.coordinate, origin))
    return tuple(target.coordinate for target in nearest_first)


__all__ = [
    "DEFAULT_SCORE_MAX_AGE",
    "TOP_BY_MILITARY",
    "MilitaryChoice",
    "ScoredTarget",
    "choose_by_military",
    "has_a_military_reading",
    "score_is_fresh",
    "strongest_first",
    "strongest_then_nearest",
    "strongest_within",
    "with_a_military_reading",
    "within_score_window",
]
