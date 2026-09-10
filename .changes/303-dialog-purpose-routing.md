---
issue: 303
agent: salvage-dialog-routing
type: Changed
date: 2026-09-10
---

**残骸回收闭环：完整实现（A+B+C+D+E）。**

## 分层

| 层 | 内容 |
|---|---|
| A | 弹窗分流：purpose 贯穿，九格表替代 DialogKind |
| B | 保护期排除计数（system_log，recorded+dialog 过滤） |
| C | 预算扣减（调度器侧：作业先占名额） |
| D | 调度接线（四层：起轮判据/轮换/预算/空目标） |
| E1 | 配置项 + 两张表（recycle_decisions / recycle_jobs） |
| E2 | 决策扫描（tick 里 acc 累加 + 生成作业） |
| E3 | bot_loop --recycle 执行链路（六步） |
| E4 | 概览页回收卡片 + 保护期排除计数 |

## 关键设计

- **整数十分位误差累加**（绝不用浮点）：R=6 × 10 发 = 正好 6 次
- **purpose 必填无默认值**，4 个调用点全部显式传
- **(NO_MISSION, RECYCLE) 不写保护期排除**
- **(NO_SHIPS, RECYCLE) 不停整轮**，WARNING 落库
- **回收按钮按标签文字定位，不写死坐标**（图标集动态）
- **残骸框三格数字不读**（用户 hold）
- **简报页任务类型只告警不拦**（R24）
- **回收必须写 attack_intents**（否则 count_inflight 少数一条 ⇒ 超派）

## 迁移

`e5a8c3d2f1b4`：`recycle_rate_tenths` + `recycle_decisions` + `recycle_jobs`。
⚠️ SQLite batch mode；推前需拿远端 `evo_helper_test` 单独验。

## 默认关

滑块默认 0（NULL=关），上线当天不会有任何回收发生。

- Configuration: `recycle_rate_tenths`（0–10 整数十分位，NULL=关）
- Database: 迁移 `e5a8c3d2f1b4`
- Verification: `pytest` 全绿 / `ruff check` 全绿 / `mypy` 无新增错误
- Safety: **攻击/侦察侧行为一个字没变**；滑块默认 0
- Rollback: 滑块置 0 即可停用；revert 本 PR 回到改造前
