"""挑今晚打谁：**先按读数新鲜度划一条线，再按「军力 ÷ 往返小时」出击。**

用户口径（2026-08-18）敲定的四步，**次序本身就是判据**，不能重排：

| 步 | 做什么 | 住在哪 |
|---|---|---|
| 1 | 剔除 24h 内已攻击的 + 刚撞过保护期的 + 本轮走完的 | `mission_scheduler._military_candidates` |
| 2 | 只保留**有本周期军力读数**的目标 | `with_a_military_reading` |
| 3 | 只保留读数落在**有效期窗口**内的（不够就放弃窗口并告警） | `within_score_window` |
| 4 | 过军力上限这道安全线，按 **军力 ÷ 往返小时** 降序出击 | `military_attack` |

第 2--3 步加上安全线就是 `choose_by_military`；再接上单出发点的第 4 步就是
`most_valuable_first`。

## 第 1 步为什么必须在最前

若先挑一批再排除已攻击目标，首批刚好都打过时军力任务会把候选池缩成空集，
排名靠后、从未攻击的目标永远轮不到。理由整段写在 `_military_candidates` 上。

⚠️ **「刚撞上过保护期」和「24h 内已攻击」是同一档，必须并排站在第 1 步里。**
它是 2026-08-18 加的：游戏的 8 小时保护期任何人打过都会触发、只能撞上了才知道，
而在它落库之前，同样的四个目标会被每一轮反复挑中——实机一轮 11.5 分钟、一发没派，
下一轮一秒后照挑不误。

⚠️ **合并第 ④⑤ 步之后这条不变量换了载体，但没有放宽。** 从前它的说法是「排除必须
在**取前 N**之前」，而那道硬截断已经没有了；如今把候选池收窄成「这一轮真的派几发」
的是**航线预算**（第 4 步，航线用尽就不再配对）。所以现在的说法是：**排除必须在
花掉航线预算之前**。挪到后面，保护期里的高分目标会把航线占满、再被筛掉，
这一轮一发不派——缩成空集那个失败模式原样复发，只是换了个闸口。

## 第 2 步：从没上过军力榜的目标不再攻击（2026-08-18 用户决定）

⚠️ **这一条推翻了「没有分数的按距离补位」那一版。** 旧设计的依据是一句错话
——「凡是没被榜单扫到过的 bot 就永远不会被攻击，而那正是库里最多的一批」
——那个「最多」是把非 bot 的行也算进去数出来的。实测生产库：**从未上过军力榜
的 bot 有 628 个，占 bot 总数（3604）的 17.4%**，不是「最多的一批」。

放弃这 17.4% 换来的是「军力优先」这个模式真的成立：补位不参与按军力排序，
补位一多，这条链路就退化成「按距离随便打」。整段善后写在
`domain.military_attack` 的模块头上。

### 第 2 步的另一半：**读数早于本周期起点的，一律当作「没有读数」**

**bot 军力每周一 UTC+0 随机刷新**（用户口径 2026-08-14，记在
`docs/军力攻击优化-开发交接.md`）。刷新那一刻，**全库的军力读数同时作废**——
它们描述的是上周的 bot，不是这周的。用户口径（2026-08-19）：「周一刷新那一刻，
全部 bot 的军力读数同时作废」。

判据是 `reading_is_from_this_cycle`：读取时刻 `>=` `domain.rules.cycle_start_utc(now)`。
**周一边界由 `cycle_start_utc` 算，不许在这里自己算一次**——`attack_intents`
那一列用的就是它，两份实现迟早分家。

⚠️ **为什么放在第 2 步，而不是在第 3 步「放宽窗口」那里补一个 `if`。**
第 3 步窗口内不足门限时会**放弃窗口、改用 `with_readings`**。周一凌晨窗口内是
0 个，上周期的读数若还留在 `with_readings` 里，放宽之后捞回来的**全是失效数据**，
而页面上只会显示「军力读数已放宽窗口」这句正常告警——**比不打还糟，因为它看起来
在正常工作**。判据放在第 2 步，`with_readings` 本身就不含它们，放宽也捞不回来。
这是「把判据放在正确的那一层」。

⚠️ **周期边界和 `max_age` 是两条独立的判据，都生效，取更严的那个。** 周期边界不
替代有效期窗口：周二读到的数在周二仍然会因为 2 小时窗口过期、走放宽那条路。
反过来，窗口再宽也拉不回上周期的读数。

⚠️ **它不是旋钮，是游戏规则**（周一 UTC+0 刷新）。按 CLAUDE.md 的判据
「改这个值会让结果变『更适合我』还是变『错』」——是后者，所以**不做成可配置**。

**这一档筛空整池时会发生什么**：`with_readings` 为空 → `application` 那边
`MilitaryPoolReading.usable` 为 0 → `domain.scheduler.bot_round_complete` 为真 →
BOT 说「没活干」→ 填空隙的军力榜任务自然拿到时间片，扫出这周的读数。
「先扫再打」因此是现成的行为，**不要另写一条**。页面那一半由
`TaskFacts.scores_are_missing` 说出来（不然它会显示成「已完成」，一句听起来顺利、
实际相反的话）。

## 第 3 步：窗口筛选

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
500 个」。

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
   ├─ 窗口内的数量 ≥ 窗口门限 → 就用窗口内这批，进第 4 步
   └─ 窗口内不足     → 放弃窗口，改用「全部有读数的目标」，并大声告警
```

**为什么不足时不按时间往下补**：往下补捞到的正是刚出窗口那一批，而按上表，
那一批恰恰是最弱的——补下去等于把上一版的缺陷换个地方原样复发。

**放宽必须大声说出来。** 用户口径（2026-08-18）：「今晚这件事的真正问题不是
『用了旧数据』，而是**用了旧数据却没人告诉你**——你是从攻击日志里一条一条对
出来的」。所以 `MilitaryChoice.widened` 是这一步的一等结果，不是可选的附注：
`application` 据它打 WARNING、页面据它显示 `TaskStatus.WIDENED_SCORE_WINDOW`。

## 第 4 步：得分 = 军力 ÷ 往返小时（2026-08-18 合并了旧的第 4、5 两步）

### 换掉的是什么

旧的第 4 步是「窗口内按军力降序取前 `top_n` 个」（**硬截断**），第 5 步是
「这批人按距离由近到远出击」。两步各自都说得通，合起来却**说不清**：
「排名第 101 的目标一个都不打」和「排名第 1 与第 100 之间只按远近分先后」
是两条互相矛盾的口径，而它们之间那条线（`top_n`）纯粹是拍出来的。

现在只剩一条口径：

    得分 = 军力 ÷ 往返小时          （军力的指数 k = 1）

按得分降序出击。近而弱的和远而强的**在同一把尺子上比**，不再有一道人为的墙。

### ⚠️ 依据是**用户口径**，不是实测的材料产出数据

用户口径（2026-08-18）：「**已知军力和材料产出正相关，但是没有具体数据来拟合
相关曲线**」。这来自实际游戏经验。本仓**没有能否定它的数据**，也**没有能拟合
曲线的数据**——所以这里用的是用户的领域判断作**价值代理**，而不是一条回归线。

⚠️ **别把这当成「已经验证过」。** 与之相关的两条限制仍然成立：

1. **「军力总量」不能当目标函数。** 按军力相关的键排序当然会让军力总量最大化，
   那是**自证的**，证明不了材料产出。真正的目标函数是三种稀有材料
   （合金碎片 / 泰坦立方 / 收割者碎片）的产出。
2. 反过来，「军力与材料无关」这个更强的说法**也不成立**：2026-08-18 一度用
   5 个样本论证过它，而那 5 个样本的军力读数与战报时间相差 15--25 小时，
   最倚重的那个反例 `4:20:6` 的军力是**战报之后 15 小时**才读到的。
   拿「打完之后的军力」论证「打之前的军力不预测收获」，那个论证无效。

### 为什么 k 写死成 1，而且**不做旋钮**

`k` 是「军力对材料产出的弹性」。拟合它需要「派出那一刻的军力」配「那一发读全的
材料」——**这两样目前都不够**：`attack_intents` 从 PR #183（2026-08-18）起才开始
快照派出时刻的军力，而战报资源识别 34 份只读全了 5 份。

没有数据的时候，把 `k` 做成旋钮不是「留了余地」，是**把一个说不清的数推给用户去
猜**，而且猜错了页面上一点异常都看不出来。所以写死 1，并把它记进待办。

**修好战报资源识别、材料样本攒够之后，应当重新检验这个 k、也重新检验
「军力是不是一个合适的价值代理」**——门槛与复算用的 SQL 都在
`docs/选靶数据跟踪-待办.md`。

### 往返小时怎么算

走 `domain.flight_time.round_trip_hours`，**环形**距离（`galaxy_gap` /
`system_gap`），不是减法。那个模块的系数只在一套编组上标过，所以它的绝对秒数
不许外传；这里只用它做**同一轮内的比值**，同一轮同一个预设，比值是真的。
限制原文写在 `domain.flight_time` 的模块头上。

### `top_n` 还在，但它只剩一个身份

它现在**只是第 3 步「窗口够不够用」的那把尺子**（`window_floor`），
**不再决定打谁**。页面文案跟着改了——同一个数字在页面上和判据里说两件事，
是这条链路每一次事故共同的形状。

## `max_score` 是安全线，不是排序键

军力高于它的一律不进池：太强的目标不是当前预设打得动的，派过去只是白烧一次
配额和一趟往返。它**不参与排序**——安全线和价值判断是两件事，混在一起的话，
调安全线会静悄悄地改变打谁。

## 读不出军力值的排最后

**不是把它们当成 0 分**：0 分是一个**读到的事实**（榜单上真的有 0 分的行），
而 None 是「不知道」。混在一起就等于把「没数据」伪装成「数据是 0」——
这个仓有一条硬规矩：猜出来的数不许长得像量出来的。

第 2 步之后 `None` 那一档其实已经不会走到第 4 步了；判据留在 `value_key` 里，
因为「不知道不等于 0」在哪里都成立。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from evo_helper.domain.flight_time import round_trip_hours
from evo_helper.domain.models import Coordinate
from evo_helper.domain.rules import cycle_start_utc

#: 第 3 步的**窗口门限**：窗口内至少要有这么多个目标，这一轮才肯只用窗口内的。
#: 用户口径（2026-08-18）：100。任务参数里的键仍叫 `top_n`（生产库里存着的就是
#: 这个键，改名要迁移，而它只是个 `params_json` 键，不值得）。
#:
#: ⚠️ **它不再决定打谁。** 从前它是「窗口内按军力取前 N 个」的那道硬截断；
#: 2026-08-18 起第 4 步换成按 `军力 ÷ 往返小时` 排序，截断随之取消（理由整段在
#: 模块头第 4 步）。**别照着旧名字理解它**——现在调大它只有一个后果：窗口更容易
#: 被放弃。
#:
#: 这个数与航路数是两件事：航路数（`scheduler_config.fleet_line_limit`）决定
#: **同时**在飞几发，而这个数决定**这一轮肯不肯只信新数据**。
#:
#: 分类：**运维旋钮**，可在任务参数里改（`top_n`）。调大 = 对「新鲜」要求更苛刻，
#: 窗口更常被放弃（于是更常拿旧读数打，但会告警）；调小 = 更愿意只用窗口内那批，
#: 代价是可选目标少、得分最高的那些可能都在窗口外。没有唯一正确答案。
WINDOW_POOL_FLOOR = 100

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

#: 得分里军力的指数。**写死 1，刻意不做旋钮**——理由整段在模块头第 4 步：
#: 拟合它要的数据（派出时刻的军力 × 读全的材料）目前一样都不够，把一个说不清的
#: 数推给用户去猜，比写死一个说得出来历的数糟得多。
#:
#: 分类：**既不是旋钮也不是标定常量，是一条「等数据」的占位**。资源识别修好、
#: 材料样本攒够之后应当重新检验（`docs/选靶数据跟踪-待办.md`）。
#: 写成常量而不是直接乘出来，是为了让「它等于 1」这件事在代码里说得出口。
MILITARY_EXPONENT = 1.0


@dataclass(frozen=True)
class ScoredTarget:
    """一个候选目标：坐标 + 军力值（可能没有）。"""

    coordinate: Coordinate
    military_score: float | None = None
    #: 军力值读到的时刻；None 表示榜单从未见过，不伪造「新鲜」。
    military_score_at_utc: datetime | None = None


def _coordinate_key(target: ScoredTarget) -> tuple[int, int, int]:
    """并列时的定序键。**每一步都要有它**：次序不定的话，同一批目标每次挑出来的
    先后可能不一样，而那会让「上一轮打到哪了」无从谈起，事后拿日志也对不上账。
    """
    return (target.coordinate.galaxy, target.coordinate.system, target.coordinate.position)


def attack_value(target: ScoredTarget, origin: Coordinate) -> float | None:
    """这一发值不值：**军力 ÷ 往返小时**。军力读不出来时给 `None`。

    这是第 4 步唯一的判据，把从前那两步（军力硬截断 + 按距离出击）合成了一句话。
    分子是价值代理、分母是这一发要占住的航线成本，比值就是「每航线小时能换到
    多少价值」——一夜的航线是有限的，能被最大化的本来就是这个密度。

    ⚠️ **分子的依据是用户口径，不是实测的材料产出。** 用户口径（2026-08-18）：
    「已知军力和材料产出正相关，但是没有具体数据来拟合相关曲线」。
    指数写死 1（`MILITARY_EXPONENT`），资源识别修好之后应当重新检验——
    整段理由与复算门槛在模块头第 4 步与 `docs/选靶数据跟踪-待办.md`。

    ⚠️ **分母必须是环形距离算出来的往返时间**（`domain.flight_time`）。
    写成 `abs(a - b)` 不会报错，只会把绕过 499↔1 的近目标算成天涯海角，
    于是它们的得分被压到最低、一夜都轮不到——而页面上看不出任何异常。

    `None` 不是 0：读不出军力的目标在这里给 `None`，由 `value_key` 排到最后。
    它们在第 2 步就已经出局了，这个判据留着是因为「不知道不等于 0」在哪里都成立。
    """
    if target.military_score is None:
        return None
    weight = float(target.military_score**MILITARY_EXPONENT)
    return weight / round_trip_hours(target.coordinate, origin)


def value_key(target: ScoredTarget, origin: Coordinate) -> tuple[bool, float, int, int, int]:
    """按「从 `origin` 打过去最划算的排前面」排序时用的键。小的在前。

    三段的地位完全不同：

    1. **有没有军力读数** —— 读不出来的一律排最后（`None` 不是 0，见模块头）。
    2. **`-得分`** —— 得分越高排越前（`attack_value`）。
    3. **坐标** —— **只为定序**：得分并列时不定序的话，同一批目标每次排出来的
       先后取决于库里的返回顺序，而那个顺序换一次查询就会变，事后对不上账。

    ⚠️ **得分依赖 `origin`**，因为往返时间是 **(目标, 出发星球)** 的函数。
    所以这个键**每次现算**，不许缓存、不许存成列——多出发点那条路上，
    存下来的那一份会拿着按主星算的得分去排第二颗星的目标，而且完全不报错。
    """
    value = attack_value(target, origin)
    return (value is None, -(value or 0.0), *_coordinate_key(target))


def reading_is_from_this_cycle(target: ScoredTarget, *, now: datetime) -> bool:
    """这一条的军力读数**是不是本周期（本周一 UTC+0 之后）读到的**。

    ⚠️ **这不是偏好项，是游戏规则。** bot 军力每周一 UTC+0 随机刷新（用户口径
    2026-08-14），刷新那一刻全库的读数同时作废——它们描述的是上周的 bot。
    所以这条线的位置不许做成旋钮：改它不会让结果「更适合我」，只会让它变错。

    ⚠️ **周一边界一律问 `domain.rules.cycle_start_utc`，不许在这里自己算。**
    `attack_intents.cycle_start_utc` 那一列用的就是它；各算一份的结果是两处对同一
    个周一给出不同答案，而那种分家在页面上一点异常都看不出来。

    边界取「大于等于」：正好落在周一 00:00:00 UTC 那一秒读到的算**本周期**。

    没有读取时刻的恒为假——说不清什么时候读的，就谈不上「是这周读的」。
    """
    scanned_at = target.military_score_at_utc
    return scanned_at is not None and scanned_at >= cycle_start_utc(now)


def has_a_military_reading(target: ScoredTarget, *, now: datetime) -> bool:
    """**第 2 步的判据**：这一条有没有一份**本周期**能用的军力读数。

    要求**分数、读取时刻、以及「读于本周期」三样都在**：

    - 没有分数 → 从没上过军力榜。用户 2026-08-18 决定这一档不再攻击（实测 628 个，
      占 bot 总数 3604 的 17.4%），理由在模块头。
    - 有分数却说不清什么时候读的 → 进不了窗口：窗口按读数时刻划线，一个没有时刻的
      目标在那把尺子上没有位置。把它当成「很旧」或者「很新」都是在编一个没量到的数。
    - 读数早于本周期起点 → **描述的是上周的 bot，已经作废**
      （`reading_is_from_this_cycle`）。

    ⚠️ **第三档必须挡在这一步，不能挪到第 3 步「放宽窗口」那里去补。** 挪过去的话，
    周一凌晨窗口内是 0 个、放宽之后捞回来的全是上周期的失效读数，而页面只显示
    「军力读数已放宽窗口」这句正常告警——**比不打还糟，因为它看起来在正常工作**。
    整段理由在模块头第 2 步。
    """
    return (
        target.military_score is not None
        and target.military_score_at_utc is not None
        and reading_is_from_this_cycle(target, now=now)
    )


def with_a_military_reading(
    targets: Iterable[ScoredTarget], *, now: datetime
) -> list[ScoredTarget]:
    """**第 2 步**：只留下有本周期军力读数的。次序保持传入的次序（排序是第 4 步的事）。

    `now` **没有默认值**：周期边界要拿它算，而编一个出来（`datetime.now()`）会让
    调用方在测试里量不准、在实机里和调度器的时钟分家——正好跨在周一边界上的那一批
    目标恰恰是最容易两边不一致的。
    """
    return [target for target in targets if has_a_military_reading(target, now=now)]


def from_a_previous_cycle(target: ScoredTarget, *, now: datetime) -> bool:
    """这一条**读到过分数，只是那份读数属于上一个周期**——本周期算它「没有读数」。

    它和「从没上过军力榜」在第 2 步的结果上一模一样（都出局），但**成因与善后完全
    不同**，所以要分得开：前者等军力榜再扫一轮就好，后者是这个 bot 从来没被扫到过。
    日志与页面据此说清「这一轮为什么一个都打不了」——**日志说假话比不说更糟**。
    """
    return (
        target.military_score is not None
        and target.military_score_at_utc is not None
        and not reading_is_from_this_cycle(target, now=now)
    )


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


def within_max_score(
    targets: Iterable[ScoredTarget], *, max_score: float | None = None
) -> tuple[ScoredTarget, ...]:
    """**安全线**：军力高于 `max_score` 的一律不进池。次序保持传入的次序。

    用户 2026-08-14 要求「军力确实要设置上限」——太强的目标不是当前预设打得动的，
    派过去只是白烧一次配额和一趟往返。默认 `None` = 不设上限。

    ⚠️ **它是一道闸，不是排序键的一部分。** 排序走 `value_key`。混进排序的话，
    调安全线会静悄悄地改变「打谁」，而用户以为自己只是在设一条保险。

    ⚠️ **这个上限目前是空转的，别据此推断什么。** 用户口径（2026-08-17）：
    「目前的 bot 的军事能力不存在太强这个可能性……已知周一刷新当日 bot 的最高
    战力只有 70 多 K」。留着不删：哪天 bot 变强了它就有用，而重新长出一条上限
    比留着一条暂时不生效的贵得多。

    ⚠️ **上限只挡「太强」，不挡「读不出来」**：`military_score is None` 的目标在
    这里照样留下。它们在第 2 步就已经出局了，这里不必也不该再判一次——
    「不知道多强」从来不构成「一定太强」。
    """
    return tuple(
        target
        for target in targets
        if max_score is None or target.military_score is None or target.military_score <= max_score
    )


@dataclass(frozen=True)
class MilitaryChoice:
    """第 2--3 步这一次的结果，**连「窗口有没有被放弃」一起带出来**。

    做成一个结构而不是只返回一个清单，是因为**放宽窗口这件事必须能被说出来**。
    用户口径（2026-08-18）：「今晚这件事的真正问题不是『用了旧数据』，而是
    **用了旧数据却没人告诉你**——你是从攻击日志里一条一条对出来的」。

    只返回 `eligible` 的话，调用方能看见的只有「这一轮能打谁」，看不见
    「凭什么是这几个」；而这两次事故（2026-08-17 的停摆、2026-08-18 的选弱）
    的共同形状恰恰是**判据在背地里换了，页面和日志照旧**。
    """

    #: 第 2 步之后：有**本周期**军力读数的那些。次序保持传入的次序。
    with_readings: tuple[ScoredTarget, ...]
    #: 第 2 步按**周期边界**丢掉的那些：读到过分数，只是那份读数属于上一个周期。
    #:
    #: ⚠️ **它必须和「从没上过军力榜」分得开。** 两者在 `with_readings` 上的结果
    #: 一模一样（都不在里面），而善后完全不同：这一档等军力榜再扫一轮就好，
    #: 另一档是这个 bot 从来没被扫到过。合起来只报一个数的话，周一凌晨的日志会说
    #: 「N 个从未上榜」——而那是句假话。
    from_previous_cycles: tuple[ScoredTarget, ...]
    #: 第 3 步划出来的窗口内那批。**放宽与否都记**——「窗口内只有几个」正是
    #: 告警里最要紧的那个数。
    in_window: tuple[ScoredTarget, ...]
    #: 这一轮实际拿来用的那一池：窗口够就是 `in_window`，不够就是 `with_readings`。
    considered: tuple[ScoredTarget, ...]
    #: `considered` 过完安全线（`max_score`）之后剩下的，**这一轮有资格被打的全部**。
    #:
    #: ⚠️ **它不是「选中的这几个」。** 2026-08-18 之前这个字段叫 `selected`，
    #: 装的是军力硬截断出来的前 `top_n` 个；现在截断没了，真正打谁由第 4 步
    #: 按 `军力 ÷ 往返小时` 的得分连同航线预算一起定。名字跟着语义改，是因为
    #: 一个叫 `selected` 却装着整池的字段会让日志和页面一起说假话。
    eligible: tuple[ScoredTarget, ...]
    #: **这一轮的池子里混进了窗口外的读数吗。** 判据是「`eligible` 里有没有超期的」，
    #: 而不是「有没有走放宽那条分支」——两者只在一种情形下不同：窗口内不足门限，
    #: 但库里本来就只有这些读数（`in_window == with_readings`）。那种情形下放弃
    #: 窗口一个目标都没多捞到，**没有用到旧数据，也就不该告警**。
    #: 日志说假话比不说更糟。
    widened: bool


def choose_by_military(
    targets: Iterable[ScoredTarget],
    *,
    now: datetime,
    max_age: timedelta,
    window_floor: int = WINDOW_POOL_FLOOR,
    max_score: float | None = None,
) -> MilitaryChoice:
    """**第 2--3 步加上安全线**：有本周期读数的 → 窗口内的 → 打得动的。

    两步各是一个独立的函数，这里只负责把它们串起来，外加**窗口内不足时那一个
    决定**——串的次序与那个决定就是判据，理由整段写在模块头第 3 步上。这里只
    重复最容易搞反的两条：

    **不足时是「放弃窗口」，不是「按时间往下补」。** 往下补捞到的正是刚出窗口
    那一批，而军力榜从强到弱扫，那一批恰恰是最弱的——补下去等于把 PR #176 的
    缺陷换个地方原样复发。

    ⚠️ **周期边界（第 2 步）在放宽之前就把上周期的读数筛掉了，这是判据的一部分。**
    留到放宽那里再补一个 `if` 的话，周一凌晨窗口内是 0 个、放宽之后捞回来的全是
    失效读数，而页面只会说一句「已放宽窗口」——看起来完全正常。整段理由在模块头
    第 2 步。

    ⚠️ **这里不排序、也不截断。** 排序要知道从哪颗星球出发（往返时间是
    (目标, 出发星球) 的函数），而那是第 4 步才知道的事。2026-08-18 之前这里还
    做一道军力硬截断，那道截断已经取消——`window_floor` 现在只用来回答
    「窗口够不够用」这一个问题。
    """
    # 先落成元组：`targets` 可能是个只走一遍的生成器，而下面要数两趟。
    pool = tuple(targets)
    with_readings = tuple(with_a_military_reading(pool, now=now))
    from_previous_cycles = tuple(item for item in pool if from_a_previous_cycle(item, now=now))
    in_window = within_score_window(with_readings, now=now, max_age=max_age)
    # 「够不够」的尺子就是窗口门限：窗口内备够了就不必动它，备不够再放宽也不迟。
    considered = in_window if len(in_window) >= window_floor else with_readings
    eligible = within_max_score(considered, max_score=max_score)
    widened = any(not score_is_fresh(target, now=now, max_age=max_age) for target in eligible)
    return MilitaryChoice(
        with_readings=with_readings,
        from_previous_cycles=from_previous_cycles,
        in_window=in_window,
        considered=considered,
        eligible=eligible,
        widened=widened,
    )


def most_valuable_first(
    targets: Iterable[ScoredTarget],
    origin: Coordinate,
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_SCORE_MAX_AGE,
    window_floor: int = WINDOW_POOL_FLOOR,
    max_score: float | None = None,
) -> tuple[Coordinate, ...]:
    """第 2--4 步的单出发点版本：`choose_by_military` 之后按得分从高到低排。

    多出发点那条路走 `domain.military_attack.assign_by_capacity_and_value`——
    前三步一模一样，只有第 4 步换成按航线预算分配。**前三步只能有这一份实现**，
    各写一遍的结果是「命令行按新口径算、页面按旧口径显示」，而那种不一致
    2026-08-15 已经撞过一次。

    `now` **没有默认值**，而 `max_age` 有：前者是一个事实，编一个出来（比如
    `datetime.now()`）会让调用方在测试里量不准、在实机里和调度器的时钟分家；
    后者是一条策略，代码里本来就有一个说得出理由的默认值。
    """
    chosen = choose_by_military(
        targets, now=now, max_age=max_age, window_floor=window_floor, max_score=max_score
    )
    ordered = sorted(chosen.eligible, key=lambda item: value_key(item, origin))
    return tuple(target.coordinate for target in ordered)


__all__ = [
    "DEFAULT_SCORE_MAX_AGE",
    "MILITARY_EXPONENT",
    "WINDOW_POOL_FLOOR",
    "MilitaryChoice",
    "ScoredTarget",
    "attack_value",
    "choose_by_military",
    "from_a_previous_cycle",
    "has_a_military_reading",
    "most_valuable_first",
    "reading_is_from_this_cycle",
    "score_is_fresh",
    "value_key",
    "with_a_military_reading",
    "within_max_score",
    "within_score_window",
]
