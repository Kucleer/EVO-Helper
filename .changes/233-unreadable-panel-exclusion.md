---
issue: 233
agent: root
type: Fixed
date: 2026-08-20
---

**「面板名读不出」现在会落库，并把那个坐标排除一段时间。** 修的是一个吞吐漏洞：
几个读不出面板名的坐标把攻击链路卡成了死循环。

## 现象（生产库 + `system_log` 实测，2026-08-20）

```
近 24h「不是 bot（面板名 None）」    40 次，只涉及 3 个坐标
对照：「不是 bot」但真读出了名字       0 次
这 3 个坐标历史上成功派出              0 次
```

⇒ **这个判据 100% 是在报识别失败**，从来没真的认出过一个「非 bot」。三个坐标军力
39,030 / 20,960 / 20,630，**正因为高才排在候选池最前**。

死循环怎么闭合的：

```
军力高 → 排在候选池最前 → 站过去面板名读不出 → 判「不是 bot」跳过
   ↓
这一轮 0 发 → came_back_empty → waiting_for_a_line 把整颗球压到「下一条航线
              空出」为止（实测一次压了 117 分钟）
   ↓
失败没有留下任何记录 → 下一轮候选池一个字没变 → 又挑中同一个 → 回到开头
```

代价：近 24h 的 65 轮 BOT 里 **16 轮空手而归（25%）**；每次白花 21--44 秒鼠标时间，
外加把那颗球的空闲航线压住约 1--2 小时。

⚠️ **只修「失败不留记录」这一环。** 面板名为什么读不出（识别层，根因未知，要实机
才查得动）一个字没碰；`waiting_for_a_line` 也一个字没碰。

## 形状照抄保护期那一条（PR #196）

它解决的是**一模一样**的问题——「撞上保护期之后选靶查不到，于是每轮重新挑中同一
批」。所以这里没有另立一套：三列、两张表，事实与策略分开存。

| 住处 | 名字 | 记的是什么 |
|---|---|---|
| `bot_targets` | `unreadable_seen_at_utc` | **什么时候读不出的**（事实，可空、无默认值） |
| `bot_targets` | `unreadable_attempts` | **连续第几次**读不出（事实，非空、`server_default="0"`） |
| `military_attack_config` | `unreadable_exclusion_hours` | **之后排除多久**（策略，可空 = 用代码默认值） |

⚠️ **「排除到什么时候」不进库**，同 `protection_seen_at_utc`：那是策略，选靶时现读
旋钮算。存进去等于把当下这份策略腌进历史数据，日后调旋钮旧行不跟。

排除接在**选靶第 ① 步**（`mission_scheduler._military_candidates`），和
`attacked_bot_targets_since` / `protected_bot_targets_since` **并排、同优先级**。
挪到航线预算之后，缩成空集那个失败模式会原样复发：读不出的高分目标先把航线占满，
再被筛掉，这一轮一发不派——正是实机上那 16 轮空轮的形状。

## 默认 6 小时，以及为什么是 6

三条边把它夹在这里（整段在 `domain.target_order.DEFAULT_UNREADABLE_EXCLUSION`）：

- **下界由「重新挑中它」的节奏定。** 那个撞了 24 次的坐标约**每小时**被挑中一次；
  窗口不明显大于 1 小时等于没排。而空轮把航线压住 1--2 小时，短于 2 小时就会在那次
  压制刚解除时原样再撞一发。
- **上界由「万一只是偶发」定。** 这几个是军力最高的目标，而**根因至今不明**
  （画面没加载完？窗口失焦？还是这个坐标就是读不出？）。排一整天等于凭一次读不出
  就把一个可能很好的目标丢掉一天，而我们并没有证据支持那个结论。
- **6 小时把观测到的浪费砍掉约四分之三**（40 次/24h → 约 12 次），同时每个坐标一天
  仍有 4 次机会自己证明「我其实读得出来」。

上限 24 小时，理由同 `PROTECTION_EXCLUSION_MAX_HOURS`：越过一天就和
`bot_revisit_hours` 争同一件事，排障时分不清目标被哪一条挡住。

⚠️ **和保护期刻意做成两个旋钮。** 保护期是**读得懂的事实**（游戏弹窗明说了「没有
可执行的任务」），「读不出」是**根因还没查清的现象**。两者该排多久没有理由相同；
合成一个，用户为了治其中一个而调的数会同时改掉另一个。

## 那个设计问题：排除之后会不会永远回不来

**选的是「排除 6 小时后放回来，同时把连续失败次数记进库」。** 理由：

1. **根因未知，永久拉黑赌的是一个还没有证据的结论。** 而被赌掉的恰恰是军力最高的
   那几个目标，且这种丢失是**静默的、不可自愈的**——库里从此少了几个坐标，页面上
   一切正常，只有人工翻库才发现得了。
2. **回来撞一次的代价有界且很小。** C=6 时一个「无可救药」的坐标每天最多再花
   4 × 约 30 秒 ≈ 2 分钟鼠标时间，而现在是每小时一次、外加把航线压住。
3. **「是偶发还是无可救药」改由数据回答**，不靠人去数日志行数：`unreadable_attempts`
   是**连续**计数（读通一次就归零），一个连续 40 次、一次都没读通过的坐标和一个偶尔
   失败一次的，在这一列上一眼就分得开。
4. **永久拉黑还要配一条解封的路**（页面、迁移、又一个旋钮），成本远高于它省下的
   那 2 分钟/天。

⚠️ **归零那一步不是顺手打扫，它是判据的一部分。** `_goto_checked` 在读不出时会复位
画面重试一次；不清零的话，一个「第一次读不出、复位之后就读通了」的坐标会被排除好几
个小时——而它明明是能打的。所以善后看的是**最后那一次**读数，并且判 `CONFIRMED`
时把标记和计数一起撤掉。

## ⚠️ 「读不出」和「真的不是 bot」必须分开

同一个结论（这一发不派），成因完全相反：

| 面板读数 | 判成 | 为什么 |
|---|---|---|
| `display_name is None` | **识别失败**，记一笔、排除 6 小时、到点放回来 | 成因未知，重试可能就好了 |
| `display_name` 有值、只是不以 `bot_` 开头 | **事实变了**，不记 | 这一位现在住着别人，该由坐标扫描更新 `bot_targets.is_bot` |
| 坐标核对不过（`MISMATCH`） | 不归这里管 | 导航漂了，`_goto_checked` 自己有自愈 |

实测目前 100% 是第一种，**但代码不许假设永远如此**。混为一谈有两重害处：把一个本该
永久剔除的坐标每 6 小时放回来撞一次，还往识别层的统计里掺假数据——而用户正是要靠
那个统计判断识别层坏得多厉害。

## 日志（落库不落文件，**不限流**）

```
坐标 X 面板名读不出（连续第 N 次）；排除到 YYYY-MM-DD HH:MM UTC（读不出的时刻 + C 小时）
```

`payload_json`：`event` / `coordinate` / `attempts` / `military_score` /
`seen_at_utc` / `excluded_until_utc` / `exclusion_hours` / `recorded`，外加
**「当时看到了什么」**四项原始读数（`panel_layout` / `panel_coordinate_text` /
`panel_owner_raw` / `panel_planet_name_raw`）——`display_name is None` 有好几种长相
（名字行整个空、贴成「荒芜行星」/「未知」占位、有主/无主布局走岔了），事后只有靠
它们才分得开，而分开正是**将来查根因**的第一步。

⚠️ **`event` 是给统计用的键，不是装饰。** `say()` 会把控制台那一行**双写**进
`system_log`（`tools.scan_coordinates.say`），所以按正文 `LIKE` 去数会把控制台那份
一起数进来。有了它，「识别失败几次 / 真不是 bot 几次」就是一句 group by。

四个时刻各有一条：

| 时刻 | 级别 | `event` |
|---|---|---|
| 面板名读不出、记上了 | WARNING | `unreadable_panel` |
| 面板名读不出，但 `bot_targets` 里没有这一行、**没能记下来** | WARNING | `unreadable_panel`（`recorded: false`） |
| 读出了名字、只是不是 bot | INFO | `not_a_bot`（`recorded_as_unreadable: false`） |
| 读通了、连续失败清零 | INFO | `unreadable_cleared` |

**不限流**：一轮里每个目标最多走到这里一次，而每一次都值钱。限流管的是每 tick 都
可能重复的那一档（先例 `record_unrecognised_screen`）。最后那一条**只在状态真的变了
时才写**（`clear_unreadable_panel` 返回 0 就不写）——每轮每个目标一条纯噪音。

⚠️ 第二行那一档是**说实话**的一档：没落成就明说「下一轮选靶排除不掉它」。默不作声
的话日志看起来像是记上了，而「日志说假话比不说更糟」（2026-08-17 那次缺中文语言包
整晚空转，正是栽在这上头）。

- Configuration: `military_attack_config.unreadable_exclusion_hours`（攻击配置页
  「面板名读不出排除时长」，紧挨「保护期排除时长」）。**运维旋钮**，留空 = 6 小时。
  调小＝读不出可能只是一次抖动，早点重试能救回一个高军力目标，代价是它更早回到候选池
  （而实测那几个约每小时就被挑中一次，填太小等于没排）；调大＝几乎不会再为它白跑，
  代价是万一只是偶发，一个高军力目标要闲置这么久。上下界 1--24，`0` 当场拒掉。
- Database: **有迁移** `d4b6e0f19c73`（`down_revision = 61eb261c5a09`，`main` 当前的
  head，单 head 无分叉），加三列：`bot_targets.unreadable_seen_at_utc`（可空、无默认
  值）、`bot_targets.unreadable_attempts`（非空、`server_default="0"`——0 对存量行是
  真话）、`military_attack_config.unreadable_exclusion_hours`（可空、无默认值 = 跟着
  代码默认走，这正是升级完成那一刻行为完全不变的保证）。
  ⚠️ **这条迁移一次都没有在任何真实库上跑过。** 生产自己在重启 bat 时升
  （`web.runtime._upgrade_database`），开发一侧一次都没碰。
- Verification: 本机 `pytest` 3,530 passed / 263 skipped、`ruff check src tests`、
  `ruff format --check src tests`、`mypy src`（135 文件，3 条 error **全在本 PR 一个字
  都没动过的行上**，`main` 上逐字相同，见「仍未验证」）。⚠️ 主要质量闸门是**变异
  测试**，逐处翻转判据、确认对应用例真的转红。**21 处变异，21 处全部被杀，无存活**：

  | 变异 | 转红的用例 |
  |---|---|
  | 排除窗口比较符号反了（`>=` → `<=`） | 5 条 |
  | 排除起点算反（`now - window` → `now + window`） | 2 条 |
  | ★ 识别失败不落库（回到原来的死循环） | 10 条 |
  | ★ 接线断掉：`_attack_once` 认不出之后不做善后 | `test_attack_once_records_the_failure` |
  | 连续计数退化成「永远是 1」 | `test_repeated_failures_count_up` |
  | ★ 排除没接进候选池（判据被删） | `..._no_longer_picks_an_unreadable_target`、`test_the_exclusion_runs_before_the_budget_is_spent` |
  | 旋钮空值：没配时回落成硬编码 1 小时 | `test_an_empty_knob_excludes_for_six_hours`、`test_a_missing_config_row_also_falls_back_to_the_code_default` |
  | 旋钮空值：默认值写成 8（抄了保护期那个数） | `test_an_empty_knob_excludes_for_six_hours` |
  | 旋钮读成保护期那一列（两个旋钮串了） | `test_the_two_exclusion_knobs_are_independent`、`test_a_configured_knob_reopens_a_target_sooner` |
  | 空串被当成 0（「没配」和「配了 0」混为一谈） | `test_blank_means_follow_the_default_not_zero`（3 参数） |
  | 下界放开（0 被收下） | `test_impossible_exclusion_windows_are_refused[0]` |
  | 识别失败那一刻不写日志 | 2 条 |
  | payload 少了坐标 | `test_an_unreadable_panel_is_recorded_and_logged` |
  | payload 少了排除截止时刻 | 2 条 |
  | 日志里那句「排除到什么时候」写死默认值、不跟旋钮 | `test_the_log_line_follows_the_configured_knob` |
  | 没能落库时假装记上了（日志说假话） | `test_a_target_with_no_row_says_so_instead_of_pretending` |
  | ★ **误伤：真的不是 bot 也记成识别失败** | `test_a_readable_name_that_is_not_a_bot_is_not_counted_as_a_failure` |
  | 误伤：坐标核对不过也记成识别失败 | `test_a_coordinate_mismatch_is_not_counted_as_a_failure` |
  | 读通之后不清零（连续退化成累计，自愈过的目标照样被排除） | `test_attack_once_clears_the_streak_when_the_panel_reads` |
  | 清零之后照写日志（本来就好着的也写） | `test_recovering_after_a_streak_is_logged_once` |
  | 六文件管线断一边：保存时不写这个旋钮 | `test_every_knob_survives_a_single_save` |
  | 页面上那个框没接进保存的那张表 | `test_the_settings_page_renders_every_knob` |

- Safety: **不新增任何点击、任何派遣、任何网络调用。** 这条改动只会让 runner **少**
  站到几个坐标上，永远不会多站一个。`expedition_reports.py` 的只读性、`AUTO_ENABLED`
  默认值、拟人化点击路径一个字没动。旋钮留空时的行为与加它之前唯一的差别，就是
  「读不出过的坐标 6 小时内不进候选池」这一条本身。
- Rollback: revert 本次提交即可。**迁移不需要退**：三列都留着不会被任何代码读，
  `unreadable_attempts` 有 `server_default`，插新行也不会报错。真要退，
  `alembic downgrade 61eb261c5a09` 只删这三列（有用例钉住它不带走别人的列）。

## ⚠️ 仍然没能验证的

1. **没有实机跑过一轮。** 「读不出的目标不再被挑中」在真机上到底把空轮率从 25% 降到
   多少、那 3 个坐标里有几个是偶发几个是无可救药，都要开着这个功能跑过一夜才知道。
   看 `unreadable_attempts` 那一列就能回答第二个问题——那正是它存在的理由。
2. **根因一个字都没查。** 面板名为什么读不出仍然未知（任务明确要求不碰识别层）。
   这条改动只是止血：把浪费从「每小时一次」降到「6 小时一次」，并把查根因要用的证据
   （四项原始读数 + 连续次数）攒进库里。
3. **页面没有渲染验证。** 新增那个框只有代码级断言（`GET /settings` 拿到 200 且
   `id` / 默认值 / 上界都在正文里）——本轮不许起 preview / dev server（会顶掉用户
   正在跑的控制台）。真实排版、和上面那格挤不挤，没有看过。
4. **生产库一个字都没读、没写，也没有对任何库跑过 `alembic upgrade`。** 第一节那些
   数字全部来自任务交待的实测结果，不是我这一轮查出来的。
5. **AI 影子选靶（`domain.ai_targeting`）没有跟着改。** 它的 `protected_until` 有对应
   字段，「读不出」没有。任务范围只写了 `_military_candidates`，而候选池是同一个来源，
   所以排除本身对它生效；但 AI 那侧的**提示词里看不到「这个坐标读不出」这条事实**。
6. **`mypy src` 那 3 条 error 没有修**：`domain/intel_query.py:88` 与
   `application/mission_scheduler.py:2875`（两条，`_military_batch_task` 里那个
   `min(..., default=None)`）。两处与 `main` 逐字相同（已比对），属于既有问题。
   本机 mypy 1.14.1 落在 `pyproject` 的 `>=1.11,<2` 内，但 **CI 才是权威**。
