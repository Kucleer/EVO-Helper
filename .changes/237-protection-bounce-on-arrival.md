---
issue: 237
agent: root
type: Fixed
date: 2026-08-21
---

**「到达之后才发现目标在保护期，舰队原路返航」这封邮件，解析器从来没认过。**
它结构上永远不会有战报，于是那一发**永久**沉在「未读回」里。

## 现象（用户截图 + 生产库只读核对，2026-08-21）

邮件正文（全角括号、逗号、句号，照抄；坐标是编的，真的那两个不进公开仓库）：

```
[4:321:9]（bot_4_321_9's Planet）处于保护状态，我方舰队已返航。
```

拿邮件时刻去生产库对那两发派遣，**分秒吻合**——它是**到达那一刻**发出的：

| 目标 | 派出（UTC） | 单程 | `expected_report_at_utc` | 邮件时刻 | 差 |
|---|---|---|---|---|---|
| A | 13:12:07 | 1467 s | 13:36:34 | 13:36:34 | 0 s |
| B | 13:27:26 | 3724 s | 14:29:31 | 14:29:32 | +1 s |

## ⚠️ 这和已知的保护期不是同一件事，代价差两个数量级

| | 触发点 | 代价 |
|---|---|---|
| 已实现的那一档 | 派遣时弹「没有可执行的任务」（`DIALOG_NO_MISSION`） | 舰队没起飞，约 **38 秒**鼠标时间 |
| **这一档** | **舰队飞到了才发现** | **一整趟往返的航线时间** |

`line_free_at_utc − dispatched_at_utc` 实测正好是单程的两倍（2934 s / 7450 s）：
这两发白占 **48.9** 与 **124.1** 分钟。航线是这条链路唯一真正稀缺的东西
（9 线 × 24 h = 216 线小时/天）。

后果是三重的，而且**账上看不出来**：

1. 这一发永远不会有战报 → 一直算「未读回」，把回收率往下拽；
2. `protection_seen_at_utc` 不写 → 那个坐标留在候选池里，下一轮可能又被挑中；
3. 白占的那一趟航线，在账上和「战报还没回来」长得一模一样。

生产库佐证：近 3 天 `system_log` 里提到「保护状态」的日志 **0 条**。

⚠️ **本 PR 不声称近 5 天那 84 发无战报的派遣（全部 398 发的 21.1%、122.8 航线
小时）都属于这一类。** 只有用户截图对应的那 2 发经过确认；其余可能是 OCR 读失败
或还没翻到。能确定的是：这一类**结构上**永远不会有战报，所以会永久沉在那个集合里。

## 判据：三样缺一不可

`vision.parsers.PROTECTION_BOUNCE_RE`——**方括号里的坐标** + **「处于保护状态」**
+ **「已返航」**：

```
\[(\d{1,3}):(\d{1,3}):(\d{1,3})\]   [^\[\]]{0,64}?
处\s*于\s*保\s*护\s*状\s*态          [^\[\]]{0,32}?
已\s*返\s*航
```

三处刻意的松紧：

- **字与字之间允许空白**（`chi_sim` 会插空格，`INTERCEPTED_PROBES_RE` 已有先例）。
  放开的只有空白，**每个字仍要按顺序出现**。
- **括注不写死**：`（bot_x_y_z's Planet）` 随目标而变，全角/半角/整段读丢都要认得出。
  **标点也不进判据**——它是这段文字里最容易被 OCR 吃掉的东西。
- **两句之间不许含方括号**：保证跨不过下一个坐标去，一封信里两条记录不会串成一条。

⚠️ **裸匹配「保护」两个字是这条判据上最贵的错法**：它会把正常战报误判，那份真
战报的收获、战损、胜负连同它认领的那一发一起消失。CLAUDE.md 上记着同形的教训——
黑洞事件只匹配完整损失短语，因为每份报告页脚都提「黑洞」二字。

判据**只有一份**：邮件列表行用它决定「这一行值不值得打开」，正文用它决定「这封是
不是」。它排在 `classify_report_subject` 所有关键词**之前**——列表行上是正文预览，
面板半透明会让背后那一页的字透进同一块 ROI（`MailRow.identity` 记着实测 192 行里
主题一字不差的是 0 行），而「战报」「攻击报告」那几条是子串判定、天然更宽。

## 那个设计问题（4.3）：选了「建一条 `battle_reports` 记录」

新增第四档 `outcome = "PROTECTED"`（`domain.battle_outcome.OUTCOME_PROTECTED`）。

**为什么不是给 `attack_dispatches` 加一列**：

1. **「未读回」全仓只有一个判据**——`battle_reports.dispatch_id` 指没指着这一发。
   `pending_reports_for_kind`、`storage.overview.unread_reports`、
   `storage.origin_efficiency._counts`、`bot_dispatch_facts` 全是同一个 LEFT JOIN。
   写下这一行，四处同时结清，**一句查询都不用改**。加一列则要逐个改到，
   而漏一处的症状是「只在那一个页面上还挂着」——最难发现的一类不一致。
2. `pending_reports_for_kind` 的 docstring（连同 `storage.intel:796` 两处兄弟注释）
   **明写着本仓不落「放弃标记」**，理由是迁移先于写入方落地时，信它的查询会一个都
   筛不掉。加列要顶着这条既有口径走，还要迁移、还要回来问用户。
3. 加列**换不来诚实**：origin-efficiency 的分母能自然排掉它，但总览那张卡的
   `period_counts` 分子**根本不 join 派遣**（`storage/overview.py:322`），只能靠重写
   分子来修——那已经不是这个缺陷的范围了。

**我把 `outcome` 的所有读取方查了一遍**（src / tests / Jinja / 内联 JS），结论：

| 档 | 位置 | 加一个新值会怎样 |
|---|---|---|
| **唯一会抛** | `storage/intel.py` `IntelSearchQuery.__post_init__` | 按 `BATTLE_RESULTS` 白名单校验，不在里面就 `InvalidQueryError` → HTTP 422。**已加进白名单。** |
| **绊线（就是要它响）** | `web/display.py:missing_intel_labels` + `tests/unit/web/test_display.py` | 白名单里有、三张标签表里没有 → 用例转红。**已补 label/tone/glyph。** |
| 会误显示 | `templates/logs.html:382` `show_losses` | `outcome is not none` 就摆一行「战损 我 — · 敌 —」。**已排掉这一档**：没打起来，战损这个概念不存在，两个破折号会读成「读失败了」。 |
| 会排错位 | `web/app.py:_ordered_outcomes` | 顺序取自 `BATTLE_RESULT_LABELS` 的键序。**已插在胜/负/平之后、`AWAITING` 之前**：前四档都是「已经有结论」，后三档是「还没有结论」。 |
| 自动跟上 | `intel.html:192` `RESULT_LABELS \| tojson`、`persistent_service.attack_log_options`（`SELECT DISTINCT outcome`）、`list_attack_log` 的过滤 | 不用改。 |
| **不受影响** | `vision/pirate_reports.parse_outcome` / `decide_outcome` / `domain.battle_outcome.outcome_from_survivors` | 它们只处理**画面横幅**。这一档一次 OCR 都不经过。 |

⚠️ **`OUTCOME_PROTECTED` 刻意不进 `OUTCOME_LABELS`。** 那个元组是横幅的 OCR 吸附
词表；放一个屏幕上根本不存在的词进去，只是给噪声多一个能吸附过去的靶子——而吸附
成功的后果是一份真战报的胜负被写成 `PROTECTED`。（有用例钉住。）

**⚠️ 这一行不是「成功但收获为 0」。** `attacker_units` / `defender_units` /
两个 `losses` / 收获格**一律留空**——那些数**不存在**，不是没读到。这没有与 PR #217
的「0 行 = 没读到」冲突：收益统计是从 `battle_report_resources` **往外**联表的
（`storage.origin_efficiency._rare`），一行都没有就是压根不参与，不会被当成一个 0
拉低每线小时的稀有产出。（`test_no_resource_rows_are_written` 守这一条。）

**回收率的含义**：这一发从「战报丢了」挪到了「打了但没打成」。它进分子也进分母，
因为回收率是一个**数据完整性**信号（`is_untrustworthy(recovery) = recovery < 0.6`，
意思是「这个出发点的收益数缺数据，别信」）——而这一发**不缺任何数据**，已经查清了。
同时它白占的那 124 分钟仍然老老实实留在「线小时」那个分母里，所以每线小时的产出
照样被它拉低——那是真的，也该是真的。

## 认不出是哪一发时**宁可不写**

`battle_reports.attacker_origin_*` 三列非空，而这封通知里根本没有出发点。所以出发点
只能从认出来的那一发派遣上取，认不出（零个候选，或多个分不开）就**只记保护期、
不写战报行**——凭猜写下去等于把这一发的账挂到别人头上，那一发从此再也不会被认领。

认领复用**与战报认领同一把尺子**（`_unmatched_dispatch_candidates` +
`_within_expected_window`），不另立判据：邮件时刻**就是**抵达时刻，而那把尺子量的
正是「战报该在什么时候到」。落库仍然走 `append_report`，由它自己再认一次并写
`match_status`/`match_confidence`——这里不预先断言一个没核过的置信度。

保护期那一半走**现有的唯一写入口** `note_protection_period`，写的是**邮件时刻**
（= 撞上的那一刻），不是我们翻到它的时刻——两者可能差几个小时，写错会把保护期的
起点往后推。

## 日志（落库不落文件，**不限流**）

```
到达时撞保护期：X 在我们到达时处于保护状态，舰队原路返航；本次白占航线 N 分钟
（单程 M 分）；已记保护期到 <时刻>；那一发已结掉，不再算未读回
```

`source = tools.protection_bounce`（单独一个 source：这件事到底发没发生，要能一句
SQL 答上来——上线之前近 3 天是 0 条）。`payload_json`：`coordinate` / `mail_at_utc` /
`dispatch_id` / `wasted_line_minutes` / `military_score` / `one_way_seconds` /
`origin` / `protection_noted` / `dispatch_closed` / `already_recorded` /
`candidates` / `arrivals_in_window`。

**不限流**：一封信只走一次（第二趟是 `already_recorded` 那一句），而每一次都值钱。

四档说实话，**级别跟着结果走**：

| 情形 | 级别 | 正文里说什么 |
|---|---|---|
| 记上了、也结掉了 | INFO | 白占多少分钟、保护期记到几点 |
| **认不出是哪一发** | WARNING | 「⚠️ 认不出这封信结的是哪一发（够得着 N 发，落在抵达窗口里 M 发）；不猜，那一发仍算未读回」 |
| **`bot_targets` 里没有这一行** | WARNING | 「⚠️ 保护期没记上……下一轮还会被挑中」 |
| 这一封读不齐 | WARNING | 读不齐的理由 + 当时那一行的主题 |
| 算不出白占多少 | （随上） | 「白占航线不明」——**不报 0**，0 会读成「一分钟都没浪费」 |

⚠️ 第二、三档是**说实话**那一档：含糊一句「已处理」的话，「结掉了」与「那一发仍然
永久挂在未读回上」在库里就分不开。有用例钉住那句话里的告警记号。

- Configuration: **没有新旋钮。** 三个阈值都是**标定常量**不是运维旋钮
  （判据见 CLAUDE.md：「改这个值会让结果变更适合我，还是变错？」）：判据里那三个
  短语由游戏文案决定；抵达窗口 `MATCH_EXPECTED_WINDOW_*` 是既有的、注释里已写明
  「不是运维旋钮」；往返 = 单程 × 2 是物理。**排除多久**这个真正的旋钮已经存在
  （`military_attack_config.protection_exclusion_hours`），本 PR 原样复用。
- Database: **没有迁移。** 一列没加、一张表没建。`battle_reports.outcome` 是
  `String(16)`、可空、无 CHECK、无 enum（`a91c6d4e8b07`），`"PROTECTED"` 9 个字符。
  ⚠️ 生产库只读查过（`SET TRANSACTION READ ONLY`，并**实测一次写被拒**：
  `psycopg.errors.ReadOnlySqlTransaction`）；`alembic_version` = `a3c81f5d2b64`，
  与 `main` 一致，没有跑过任何 `alembic upgrade`。
- Verification: `pytest` 3,701 passed / 263 skipped、`ruff check src tests`、
  `ruff format --check src tests` 全绿。⚠️ 主要闸门是**变异测试**：**32 处变异，
  32 处全部被杀，无存活**（判据放宽 3 处、判据收紧 3 处、分类顺序 2 处、OCR 词表
  1 处、保护期时刻 2 处、结账 4 处、「收获 0」3 处、白占算法 3 处、日志 7 处、
  信箱接线 3 处、正文多目标 1 处）。
- Safety: **不新增任何点击、任何派遣、任何网络调用。** 只在**已经会打开**的邮件上
  多认一种正文。`expedition_reports.py` 的只读性、`AUTO_ENABLED` 默认值、拟人化点击
  路径一个字没动。对调度的唯一影响是「撞过保护期的坐标暂时不进候选池」——那条规则
  本来就在，只是现在多了一个能触发它的证据来源。
- Rollback: revert 本次提交即可。没有迁移要退。库里已经写下的 `PROTECTED` 行
  revert 之后会变成「一条战果读不出来的战报」（页面显示原始英文），不会让任何查询
  报错，也不会重新变回「未读回」。

## ⚠️ 仍然没能验证的

1. **没有实机跑过一趟。** 这封邮件的**主题行**长什么样，我从来没看见过——用户给的
   是正文截图。判据因此建在正文上，并让 `classify_report_subject` 也拿同一个模式去
   认列表行的预览文字。如果游戏的列表行**只显示主题、不显示正文预览**，而那个主题
   恰好含「战报」二字（会被判成 `ReportKind.SYSTEM`），这封信就不会被打开——功能
   静默失效。这是本 PR 最大的一处未知。
   排查办法：上线后查 `system_log` 里 `source = tools.protection_bounce` 有没有行；
   一条都没有而库里又出现新的「永远不会有战报」的派遣，就是撞上了这一种。
2. **正文那块 ROI（`report_layout.security_message`，720,205–1205,420）没有拿这封信
   的实拍量过。** 它是「你的行星被侦察」那封安全提示的 ROI，两者都是「页眉 + 一句
   正文」的版面，所以复用；但这封信的正文在不在那个矩形里，只有实机才知道。
3. **生产库一个字都没写。** 那 2 发已确认的派遣**没有**被补录成 `PROTECTED`——本轮
   只读。要补的话得另开一条离线入口，或者等信箱里那两封被下一趟对账翻到（它们
   2026-08-20 的，早已掉出 6 小时的 `_routine_scan_floor`，多半要走手动补录）。
4. **页面没有渲染验证。** 新那一档的 chip（🛡 撞保护期，warn 色）、以及攻击日志上
   那行不再摆战损，都只有代码级断言——本轮不许起 preview / dev server。
5. **`web/app.py:_ordered_outcomes` 的排序没有用例钉。** 它读的是
   `BATTLE_RESULT_LABELS` 的键序，而字典键序在这里是**有语义的**；谁按字母序重排
   那个字典，下拉框的分组就散了，而没有任何东西会转红。
6. **本机 `mypy` 是 2.3.0，落在 `pyproject` 的 `>=1.11,<2` **之外**，所以它报的
   `Success: no issues found in 142 source files` 不作数——CI 才是权威。**
7. **AI 影子选靶（`domain.ai_targeting`）没有跟着改。** 它的 `protected_until` 读的是
   `protection_seen_at_utc`，所以排除本身对它生效；但提示词里看不出这次保护期是
   「白飞了一趟才撞上的」还是「派遣时就被拦下的」——而这两者的代价差两个数量级。
