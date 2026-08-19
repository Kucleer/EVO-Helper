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
返回，**零开销**（不建线程、不组 prompt、不查库）；线程有上限（同时 1 个）、
每任务有最小间隔（60 秒）。⚠️ **这条路走不到二期**——二期要按 AI 结果派遣，
它必须回到关键路径上，那是一次真实的架构分叉，需求文档 2.2 已写明。

## 输入：五样事实，一个旋钮的值都不给

- **航线预算**：直接给可派发数 + 减法过程，「时长未知」那几条单独标
  （按兜底 90 分钟占着）。⚠️ 占用判据复用 `repository._still_holding_a_line`
  （新增 `repository.inflight_lines`），不许另算。
- **候选池**：三维交叉表（银河 × 往返档 × 军力档，每格个数/军力中位/龄中位）
  + 分层样本（每格军力最高 3 + 读数最新 3，去重，总量旋钮默认 60）。
  ⚠️ 抽样键刻意是「最强/最新」两个，**不是** `军力 ÷ 往返`——那等于把要验证的
  公式的答案泄露给它。
- **时间**：周期起点、刷新后第几天、样本读数龄。
- **飞行公式**：给公式本身 + 适用域误差（同银河近档 p90 误差 32–50%，跨银河
  ≤0.03%）。
- **收益模型**：`稀有三样 ≈ 0.141 × 军力 × exp(−0.068 × 快照龄小时)`
  （n=74，R²=0.781）。⚠️ 基础三样绝不进模型（由我方货舱容量决定）。
- **游戏规则**：保护期 8h、周一刷新、我方上次攻击时刻、可用预设。
- **不给**：五个旋钮的值、单位数量、账号名/玩家名。prompt 模板进 git，不含任何
  真实坐标例子。

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
  `ai_model`、`ai_timeout_seconds` 默认 30、`ai_sample_size` 默认 60、
  `ai_retention_days` 默认 90），一律可空不给 `server_default`（NULL = 跟着代码
  默认走，同 `blind_scrolls` 的先例）
- Database: 新增 `ai_target_decisions` 表 + `military_attack_config` 五个可空列
  （迁移 `61eb261c5a09`，单一 head）
- Verification: 本机 `pytest` 全绿（unit 2054 + integration/application 505 +
  storage/api 671 + e2e 238 + safety）、`ruff check src tests`、`mypy src`
  （135 文件无 issue）、`compileall`。验收四件事各有用例钉住：派遣逐字不变
  （`test_ai_shadow_safety.py`）、LLM 挂掉调度继续（超时/异常/坏 JSON 三档）、
  开关关零开销、四种 status 各落一行（`test_ai_shadow_ingest.py`）
- Safety: 一期**绝不影响派遣**——调度返回值逐字不变；失败静默降级、不占关键
  路径；凭据只走环境变量、prompt 不含 key；模板不含真实坐标；没有真实 LLM 调用
  （测试全用假 httpx）
- Rollback: revert 本次提交即可。迁移要退就 `alembic downgrade 61eb261c5a09`
  （开发一侧不碰生产库；生产退回由用户重启 bat 前的代码完成）
