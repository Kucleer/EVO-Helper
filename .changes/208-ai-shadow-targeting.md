---
issue: 208
agent: root
type: Added
date: 2026-08-19
---

AI 选靶**一期（影子模式）**：把局面组成 prompt → 调 LLM → 拿到它选的目标 →
存下来 → 诊断页上能对比。**绝不影响派遣**——调度照常用
`assign_by_capacity_and_value`，AI 的选择只是记录。

用户口径（2026-08-19）：「先做第一期」「第一期完成基础框架后，直接评估他的选择」。

## 一期的架构：发出去就不管

`_military_assignments`（同步调度 tick 里）算完之后调 `AiShadowObserver.observe()`，
**立刻返回**——组 prompt、调 LLM、校验、落库全在后台线程里。开关关掉时第一行就
返回，**不建线程、不组 prompt、不发任何请求**；代价是一行
`military_attack_config` 的查询（开关就住在那张表里，而同一次派遣本来就要读它
好几次）——**「零开销」不含这一行**，写清楚免得下次有人照着这句去缓存它，
把「页面上点开开关」变成延迟生效。线程有上限（同时 1 个）、
每任务有最小间隔（60 秒）。⚠️ **这条路走不到二期**——二期要按 AI 结果派遣，
它必须回到关键路径上，那是一次真实的架构分叉，需求文档 2.2 已写明。

## 输入：五样事实，一个旋钮的值都不给

- **航线预算**：直接给可派发数 + 减法过程，「时长未知」那几条单独标
  （按兜底 90 分钟占着）。⚠️ 占用判据复用 `repository._still_holding_a_line`
  （新增 `repository.inflight_lines`），不许另算。
- **候选池**：喂的是 `MilitaryPoolReading.candidates`（**选靶第 1 步之后的全池**），
  ⚠️ **不是 `eligible`**——后者已被 `score_max_age_hours`、`top_n`、`max_score`
  三个旋钮裁过，而这三个旋钮的**值一个都没进 prompt**：旋钮的数值不给、筛选
  效果照给，是「把答案先塞给它」的另一种形态。三维交叉表（银河 × 往返档 ×
  军力档，每格个数/军力中位/龄中位；**「无读数」单占一档**，不许并进 `<10K`）
  + 分层样本。⚠️ 抽样键刻意是「最强/最新」两个，**不是** `军力 ÷ 往返`。
  抽样改成**按格子轮转填**、格子之间轮流由两个键领头：旧的「3+3 → 1+1 →
  只取最强 1」阶梯在生产量级上会落到最后一级，**把「最新」这个键整个丢掉**
  （合成 3,788 个候选、两个出发点实测：80 个非空格子，`sample_size=60` 恰好
  退到那一级）。`ai_sample_size` 默认因此从 60 调到 **120**，而 prompt 里那句
  描述改成**照实说**每格实际取了几个、覆盖了几个格子。
- **时间**：周期起点、刷新后第几天、样本读数龄，以及**全池读数龄分布**
  （中位 / p90 / 最大 / 多少个根本没读数）。⚠️ 上一版只写了一句「下面是全池
  读数龄分布」而后面什么都没有——对模型说有而实际没有，比不说更糟。
- **飞行公式**：给公式本身 + 适用域误差（同银河近档 p90 误差 32–50%，跨银河
  ≤0.03%）。
- **收益模型**：`稀有三样 ≈ 0.141 × 军力 × exp(−0.068 × 快照龄小时)`
  （n=74，R²=0.781）。⚠️ 基础三样绝不进模型（由我方货舱容量决定）。
- **游戏规则**：保护期 8h、周一刷新、我方上次攻击时刻、可用预设。
- **不给**：五个旋钮的值、单位数量、账号名/玩家名。prompt 模板进 git，输出示例
  里的坐标是 `0:0:1` / `0:0:2` 这样**不存在的 0 号银河**占位值
  （`EXAMPLE_TARGET` / `EXAMPLE_ORIGIN`）——⚠️ 上一版那里写的是用户真实的出发
  星球，而同一份说明还宣称「不含真实坐标」。占位值顺带是一道免费检验：模型
  照抄示例会被硬校验当场作废。

## 输出：严格 JSON + 两层核对

```json
{"picks": [{"target": "3:98:12", "origin": "4:277:15", "preset": "BBB", "rank": 1,
            "military": 31820, "reading_age_hours": 0.3, "round_trip_minutes": 125,
            "reason": "……"}],
 "pool_warnings": [...], "confidence": "high|medium|low", "notes": "……"}
```

- **硬校验**（不过整份作废 → `schema_violation`）：picks 数必须恰好等于预算、
  target/origin/preset 必须来自给过的集合、同一坐标不许两次、origin 预算不超。
- **软核对**（只记录）：数字自洽（军力精确相等、龄 ±0.1h、往返 ±1 分钟——
  **编数字从「靠人看」变成「靠断言」**）、规则遵守（保护期 8h、距我方上次攻击
  不足 8h，判据用游戏规则不是 24h 旋钮）。

## 落库：`ai_target_decisions`

`prompt_text` / `response_text` **原样存**（模型换版本答案就变，这是唯一能对账的
东西）；`decided_at_utc` 是产生时刻不是入库时刻；`status` 五档
（`ok` / `timeout` / `http_error` / `invalid_json` / `schema_violation`），失败也
落一行。保留期旋钮 `ai_retention_days`（默认 90 天，控制台启动清理）。

⚠️ **迁移合进 `main` 就完事，开发一侧一次都不许跑 `alembic upgrade`**——生产
重启 bat 自己升（`web.runtime._upgrade_database`）。

## 凭据

全仓第一个对外网络调用。`Settings` 加三样，alias 直接读用户 `.env` 里已有的键：
`EVO_HELPER_API` / `EVO_HELPER_Model` / `EVO_HELPER_API_key`。**不进库、不进代码、
不进日志**。`httpx` 进主依赖（缺它时整条观测降级为不可用，调度不受影响）。

- Configuration: 五个旋钮全在 `military_attack_config`（`ai_shadow_enabled` 默认关、
  `ai_model`、`ai_timeout_seconds` 默认 30、`ai_sample_size` 默认 **120**
  （需求文档写的 60 是按 `eligible` 量级估的，换全池后连每格一个代表都不够）、
  `ai_retention_days` 默认 90），一律可空不给 `server_default`（NULL = 跟着代码
  默认走，同 `blind_scrolls` 的先例）
- Database: 新增 `ai_target_decisions` 表 + `military_attack_config` 五个可空列
  （迁移 `61eb261c5a09`，单一 head）
- Verification: 本机 `pytest tests` 3,420 passed / 263 skipped、
  `ruff check src tests`、`ruff format --check src tests`、`mypy src`
  （135 文件，3 条 error **全部在本 PR 一个字都没动过的行上**，`main` 上同样报，
  见「仍未验证」一节）。
  ⚠️ **主要质量闸门是变异测试，不是「用例全绿」。** 上一版栽的就是这里：一条名叫
  「派遣逐字不变」的用例**跑的时候开关是关的**，于是 baseline 和被污染的那一路
  比的是同一条没有 AI 的路径——审查把 `_military_assignments` 的返回值反序，
  **AI 相关的 40 条用例全绿**。这一轮逐处注入判据翻转、确认对应用例真的转红：

  | 变异 | 转红的用例 |
  |---|---|
  | ★ 开关开着就把 assignments 反序 | 4 条（含「逐字不变」两条、observer 抛异常、observer 声称成功） |
  | 候选池退回 `eligible` | `test_the_observer_gets_the_whole_pool_not_the_filtered_one` |
  | 删掉全池龄分布 | 2 条 |
  | 抽样两个键只剩「最强」 | `test_even_one_per_cell_keeps_both_keys` |
  | 抽样描述写死不照实说 | `test_it_reports_what_was_actually_taken` |
  | 「无读数」并回 `<10K` 档 | `test_the_cross_table_gives_no_reading_its_own_bucket` |
  | 示例坐标换回真实出发星球 | 2 条 |
  | `_ai_health` 硬校验率合并回全体分母 | 5 条 |
  | 删掉跳过路径日志 | 5 条 |
  | 删掉「可用↔不可用」跃迁日志 | 4 条 |
  | 删掉调度器一侧 `except` 的留痕 | `test_the_scheduler_records_why_it_skipped_the_shadow` |
  | 上次攻击改读那个从没被写过的列 | `test_a_pick_we_attacked_three_hours_ago_is_flagged` |
  | observer 不再自己确认开关 | 2 条 |
  | 保护期判据废掉 | 2 条 |

  ⚠️ 其中两处是**这一轮补出来的漏网**：把 8 小时判据换成 24 小时旋钮、把「无读数」
  并进 `<10K`，原先都是**全绿**——各补了一条用例才钉住。
- Safety: 一期**绝不影响派遣**——调度返回值逐字不变；失败静默降级、不占关键
  路径；凭据只走环境变量、prompt 不含 key；模板里的示例坐标是不存在的 0 号银河
  （用例逐个 token 断言）；⚠️ **没有真实 LLM 调用**——所有用例都用假 httpx，
  真实模型的返回质量一期结束前无从验证
- Rollback: revert 本次提交即可。迁移要退就 `alembic downgrade 61eb261c5a09`
  （开发一侧不碰生产库；生产退回由用户重启 bat 前的代码完成）


## ⚠️ 仍然没能验证的

一期是观测框架，**下面这几件事这个 PR 结构上证明不了**，写下来免得被当成已验：

1. **没有一次真实的 LLM 调用。** 所有用例都注入假 httpx。真实模型答出来的东西
   长什么样、JSON 通过率有多少、`pool_warnings` 说不说得中，**要等开关真的打开
   跑过两个完整周期才知道**（验收标准最后四项本来就写着「跑满两周才算数」）。
2. **诊断页那一块没有渲染验证。** `_ai_health` 的算法有用例，模板本身没有——
   本轮不许起 preview / dev server（会顶掉用户正在跑的控制台）。
3. **迁移只读过、一次都没执行。** `61eb261c5a09` 不曾在任何库上跑过
   （开发一侧绝不跑 `alembic upgrade`，生产重启 bat 自己升）。
4. **生产库的候选池规模是文档值，不是这一轮现查的。** 本机没装 psycopg，
   为一次估算去装一个数据库驱动不划算。方案 2.2 记的是 **3,784**、需求 11.3 记的是
   **3,788**；「80 个非空格子」那个数是拿 3,788 个合成候选 + 两个出发点量出来的，
   银河分布与真实的不完全一样，**当量级看，别当精确值**。
5. **`mypy src` 那 3 条 error 没有修**：`domain/intel_query.py:88` 与
   `application/mission_scheduler.py:2698`（两条）。那两处的代码与 `main` 上**逐字
   相同**（`git diff origin/main` 对这两个文件的相关行为空），属于既有问题，
   不在这次的范围里；本机 mypy 是 1.14.1，落在 `pyproject` 的 `>=1.11,<2` 内，
   但**CI 才是权威**。

## ⚠️ 软核对那两条规则：一个必须说清的事实

审查提出「换成全池之后 `rule_in_protection` / `rule_attacked_too_recently` 才真正
开始工作」。**查下来不是这样**，理由写在 `tests/.../test_ai_shadow_soft_rules.py`
的模块头上，摘要：

- 这两条够不够得着，由**选靶第 1 步**的两个排除窗口决定，而 `candidates` 与
  `eligible` **都在第 1 步之后**——换池子不改变它们的可达性。第 2--4 步筛的是
  读数窗口、窗口门限、军力上限，与保护期和攻击间隔无关。
- 默认配置（`bot_revisit_hours`=24、`protection_exclusion_hours`=8）下，第 1 步的
  排除与游戏的 8 小时保护期严丝合缝甚至更保守，**两条规则恒不触发**。
- 把任一窗口调到 8 小时以下（页面允许填到 1 小时）它们就会触发。这不是假想：
  想多榨几轮的人会调小复访，而**游戏的 8 小时不跟着变**。

⇒ **「规则遵守率」在默认配置下量到的 100% 来自第 1 步的排除，不是来自 AI 守规矩。**
看这个指标的人必须知道这件事。现在有四条用例把它钉住：两条让规则真的触发、
一条反面、一条直接断言「默认旋钮下够不着」。

换池子本身仍然是必须做的，理由是另一个：`eligible` 已被三个**旋钮**裁过，
而那三个旋钮的值没进 prompt——这才是「把答案先塞给它」的那一处。
