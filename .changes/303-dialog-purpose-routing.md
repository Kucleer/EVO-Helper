---
issue: 303
agent: salvage-dialog-routing
type: Changed
date: 2026-09-10
---

**残骸回收闭环：弹窗分流 + 模型/迁移 + acc + 调度接线 + 执行骨架。**

## 分层

| 层 | 内容 | 状态 |
|---|---|---|
| PR-A | 弹窗分流：purpose 贯穿，九格表替代 DialogKind | ✅ |
| PR-B | 保护期排除计数（system_log，recorded+dialog 过滤） | ✅ |
| PR-C | 预算扣减（调度器侧：作业先占名额） | ✅ |
| PR-D | 调度接线（四层：起轮判据/轮换/预算/空目标） | ✅ |
| E1 | 配置项 + 两张表（recycle_decisions / recycle_jobs） | ✅ |
| E2 | 决策扫描（tick 里 acc 累加 + 生成作业） | ✅ |
| E3 | bot_loop --recycle 执行链路 | ⚠️ 骨架 |
| E4 | 概览页三个数 + 保护期排除计数 | 待做 |

## 关键设计

- **整数十分位误差累加**（绝不用浮点）：R=6 × 10 发 = 正好 6 次
- **purpose 必填无默认值**，4 个调用点全部显式传
- **(NO_MISSION, RECYCLE) 不写保护期排除**
- **(NO_SHIPS, RECYCLE) 不停整轮**，WARNING 落库
- **回收先于攻击执行**，预算按实际派遣扣
- **空 targets 允许**（纯回收轮）

## 迁移

`e5a8c3d2f1b4`：`recycle_rate_tenths` 配置列 + `recycle_decisions` + `recycle_jobs`。
⚠️ SQLite batch mode；推前需拿远端 `evo_helper_test` 单独验。

## 默认关

滑块默认 0（NULL=关），上线当天不会有任何回收发生。

- Configuration: `recycle_rate_tenths`（0–10 整数十分位，NULL=关）
- Database: 迁移 `e5a8c3d2f1b4`
- Verification: `pytest tests/unit/tools/ -q`（822 passed）/ `ruff check` / `mypy` 全绿
- Safety: **攻击/侦察侧行为一个字没变**；滑块默认 0
- Rollback: 滑块置 0 即可停用；revert 本 PR 回到改造前
