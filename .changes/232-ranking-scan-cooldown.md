---
issue: 232
agent: root
type: Added
date: 2026-08-20
---

军力榜扫描的**任务级冷却**（页面上叫「扫描间隔」）：两轮扫描之间至少隔 C 小时，
**从上一轮开始的那一刻算起**。留空 = 不限，与加这个旋钮之前逐字相同。

用户口径（2026-08-20）：「比如在周四，我会把 bot 攻击的军力范围选择为 6 小时。
但是我又不希望太多的扫描打断派出攻击。所以我会设定扫描间隔为 2 小时。**当新的扫描
发起时，检查上次开始扫描的时候是否大于 2 小时。** 当周一时，我会将军力范围选择为
2 小时，扫描冷却为 1 小时，这样尽快的轮转。」

## 键名与住处

`mission_tasks.params_json` 里的 **`scan_cooldown_hours`**，与 `score_max_age_hours`
同一层。名字沿用既有风格（`_hours` 后缀 + 说清它量的是什么）。

⚠️ **不进 `military_attack_config` 那张全局表**，两条理由都来自用户那段话：
用户按**周内相位**来回调（周一 1 小时 / 周四 2 小时），而它和同样按相位调的
「军力分数有效期」是配套的一对；再者扫描任务将来可能不止一个，全局值配不了
「这个扫得勤、那个扫得稀」。

⚠️ **故意没有代码默认值。** 理由照抄 `military_attack_config.blind_scrolls`：
给了默认值就分不开「没配」和「恰好配成当前默认」。`0` 与负数当场拒掉——「不限」
有一个明明白白的表达方式（把框留空），拿一个看起来像时长的数字表达它，只会让
下一个读库的人分不清那是「想不限」还是「填错了」。

## 判据挂在哪一行

`domain.scheduler.has_work` 的**第二句**，紧跟 `within_schedule_window`：

```python
if not within_schedule_window(task, facts.now_utc):
    return False
if scan_cooldown_verdict(task, facts).blocks:      # ← 这一行
    return False
```

两者同层，都是「这一刻允不允许开新的一轮」，都与「有没有活可干」无关。放在这里
自动覆盖三处：普通调度、军力批次那条专用路径、以及状态文案。⚠️ **必须在下面
那个「填空隙恒为真」的早退之前**——军力榜正是填空隙的那一种。

判据本身只有一份（`scan_cooldown_verdict`），返回一个带齐 `elapsed` / `remaining` /
`pool` 的结构，日志与页面都问它，谁都不许自己再减一遍。

**边界取「满 C 就放行」**（`elapsed >= cooldown`），与 `after_schedule_window`
同向（区间左闭右开）。

## 安全阀：冷却不许把自己饿死

```
冷却只在「窗口内还有货」时生效。
窗口内候选数一旦低于门限（top_n / WINDOW_POOL_FLOOR），冷却立刻让路。
```

**没有它，这个旋钮会在最不能出错的那一天炸掉。** 周一的配置是窗口 2 小时、间隔
1 小时；若某一轮扫描失败或被打断，下一轮又被间隔挡住，窗口内候选就会归零 →
选靶第 3 步触发「窗口内不足门限就放弃窗口」→ 回落到全部读数。而周一恰恰是最不能
用上周期读数的一天（全服刚重置，PR #212 专门处理过）。也就是说，一个本意是
「少打断攻击」的旋钮，会把攻击喂上一批已经作废的数据，而页面上只显示一句听起来
很正常的「军力读数已放宽窗口」。

**怎么实现的**：`SchedulerFacts` 上新增账号级事实 `military_window`
（`MilitaryWindowPool(in_window, floor)`）。它由 `_facts` 从**这一趟已经算好的**
`MilitaryPoolReading` 取（`_most_starved_window`），**不另查一遍库、不另算一份口径**
——`below_floor` 的比较式与 `choose_by_military` 里那一行
`len(in_window) >= window_floor` 逐字相同，正好是「选靶会放弃窗口」的那一刻。
多个军力优先任务时取**最饿的那一个**（`in_window - floor` 最小）。

⚠️ `military_window is None` 的含义是「**没有军力优先的 bot 任务在参与调度**」，
不是「窗口空了」——当成空的话，一个没开军力攻击的账号上这个旋钮永远被顶开。

## 批次闸门那条怎么验的

`_military_batch_decision` 在有子进程时一律 `Decision(Action.IDLE)`，而它扣着的
那一批交接问的是 **bot 任务**的 `has_work`——军力榜身上这道闸门碰不到它。两条用例
钉住：

- `test_the_cooldown_never_interrupts_a_scan_that_is_already_running`：间隔生效期间
  正在跑的那一轮活着、`started_at_utc` 不变、进程没被 terminate。
- `test_the_attack_batch_hands_over_even_while_the_scan_is_cooling`：榜刚采完 10 分钟
  （间隔 2 小时，远没到），下一步必须是 BOT 起来；同时断言此刻军力榜确实还显示
  「扫描间隔未到」——否则上一条断言什么都没证明。

## 日志（落库不落文件）

三个时刻，全部走 `infrastructure.system_log.record_system_log`：

| 时刻 | 级别 | 说了什么 |
|---|---|---|
| 挡掉（`BLOCKING`） | INFO | 上次开始于何时、已过多久、间隔多长、还差多久、窗口内几个 / 门限几个 |
| 安全阀让路（`OVERRIDDEN`） | WARNING | 同上，外加「窗口内已低于门限，再挡下去会回落到上一周期的陈旧读数」 |
| 一轮扫描本身长过间隔 | WARNING | 这一轮多久、间隔多长、以及「从开始算，所以它在结束时就已经过完」 |

前两条走 `_log_a_repeated_line` 限流（状态变了立刻写，没变一个窗口最多一条）。
⚠️ **签名只认状态**（`(挡不挡, 从哪一刻起算, 冷却多长)`），**不认「还差几分钟」**
——那个数每 tick 都在变，进签名等于限流整个失效，而 2026-08-18 那一小时的
12,080 行废日志正是这么来的。第三条不限流：`_finish` 每个子进程只走一次。

- Configuration: 任务参数 `scan_cooldown_hours`（调度台军力榜那一行，紧挨「扫描
  数量」）。**运维旋钮**：调大＝扫描少打断攻击、一夜派得出去的发数多，代价是读数
  变旧、军力被高估；调小＝读数新鲜、选靶准，代价是更常打断攻击。留空 = 不限。
- Database: **没有迁移，一列都没加。** 它住在 `params_json` 里，同 `bot_limit`。
- Verification: 本机 `pytest tests` 3,489 passed / 263 skipped、
  `ruff check src tests`、`ruff format --check src tests`、`mypy src`
  （135 文件，3 条 error **全在本 PR 一个字都没动过的行上**，`main` 上同样报，
  见「仍未验证」）。⚠️ 主要质量闸门是**变异测试**，逐处翻转判据、确认对应用例
  真的转红：

  | 变异 | 转红的用例 |
  |---|---|
  | 起算点从「开始」改成「结束」 | `test_the_cooldown_is_measured_from_the_start_of_the_previous_round` |
  | 比较符号 `>=` → `>` | `test_the_cooldown_lets_go_the_moment_it_is_full[正好满两小时]` |
  | 比较符号 `>=` → `<`（整个反过来） | 6 条（挡住的全放行、放行的全挡住） |
  | ★ 安全阀失效（`below_floor` 恒假） | `..._steps_aside_once_the_window_pool_dips_below_its_floor`、`test_a_starving_window_pool_makes_the_cooldown_step_aside` |
  | 安全阀恒真（`below_floor` 写成 `<=`） | `test_a_pool_exactly_at_its_floor_still_counts_as_having_stock`、`test_a_healthy_window_pool_leaves_the_cooldown_in_force` |
  | `military_window=None` 当成「窗口空了」 | `test_no_military_task_at_all_is_not_the_same_as_an_empty_window`、`test_no_military_task_means_no_valve` |
  | 没配时回落成 2 小时默认值 | `test_an_unconfigured_cooldown_never_blocks_anything`、`..._empty_cooldown_lets_the_scan_come_straight_back`、`test_an_empty_box_parses_to_no_cooldown_at_all`（3 参数） |
  | 冷却把已开始的批次卡住（`_military_batch_decision` 改问 RANKING 的 `has_work`） | `test_the_attack_batch_hands_over_even_while_the_scan_is_cooling` |
  | 挡住时顺手 `stop()` 掉正在跑的那一轮 | `test_the_cooldown_never_interrupts_a_scan_that_is_already_running` |
  | 挡掉时不写日志 | `test_being_held_back_is_written_down_with_the_numbers` |
  | 日志签名改回 `_line_signature`（限流失效） | `test_the_held_back_line_is_throttled_instead_of_written_every_tick` |
  | 安全阀放行时不写日志 / 降成 INFO | `test_a_starving_window_pool_makes_the_cooldown_step_aside` |
  | 边界那条不写 | `test_a_round_that_outlived_its_own_cooldown_leaves_a_trace` |
  | 旋钮从任务级退化成全局（`task_snapshot` 改按 kind 取第一条 RANKING 的参数） | `test_two_ranking_tasks_keep_their_own_cooldowns` |
  | 页面状态掉回兜底的「等航线」 | `test_a_held_back_scan_says_so_instead_of_claiming_to_wait_for_a_line` |
- Safety: **不新增任何点击、任何派遣、任何网络调用。** 它只会让军力榜**少**起几轮，
  永远不会多起一轮（安全阀只是把闸门抬回加这个旋钮之前的状态，不催任何人）。
  正在跑的那一轮一个字都不碰——这一层是纯判据、动不了子进程。留空时行为与
  加这个旋钮之前逐字相同。
- Rollback: revert 本次提交即可。**没有迁移要退**；库里已存的
  `scan_cooldown_hours` 键会变成一个没人读的键，不影响任何判据。

## ⚠️ 仍然没能验证的

1. **没有实机跑过一轮真实扫描。** 用例里的「一轮 40 分钟」是把假时钟往前拨出来的，
   真实一轮的时长、以及真实池子在一轮之间掉多少个，都要开着这个旋钮跑过一个
   周四 + 一个周一才知道。
2. **页面没有渲染验证。** 新增的输入框、`↻` 那个字形、「扫描间隔未到」那一档的
   上色都只有代码级断言——本轮不许起 preview / dev server（会顶掉用户正在跑的
   控制台）。
3. **生产库一个字都没读、没写。**
4. **`mypy src` 那 3 条 error 没有修**：`domain/intel_query.py:88` 与
   `application/mission_scheduler.py`（`_military_batch_task` 里那个
   `min(..., default=None)`，两条）。那几行与 `main` 上逐字相同，属于既有问题。
   本轮新写的 `_most_starved_window` 刻意避开了同一个写法（先早退再 `min`），
   没有把这个模式再抄一遍。本机 mypy 1.14.1，落在 `pyproject` 的 `>=1.11,<2` 内，
   但 **CI 才是权威**。
5. **「窗口内候选低于门限」这个安全阀口径只对得上军力优先那一支。** 区域攻击
   （`by_military` 关着）的 bot 任务不产生 `MilitaryPoolReading`，于是
   `military_window` 为 `None`、冷却照常生效——那是对的（区域攻击不读军力榜），
   但如果将来有别的东西开始依赖军力榜的新鲜度，这道安全阀不会自动覆盖它。
