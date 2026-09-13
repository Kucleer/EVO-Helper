---
issue: 328
agent: domain-storage
type: Fixed
date: 2026-09-12
---

**回收作业终于挂得上「是哪一发派的」。** `recycle_jobs.dispatch_id` 这根指向
`attack_dispatches.id` 的外键，从建表到 2026-09-12 **一行都没写过** —— 生产库 193 行
（dispatched 164 / no_debris 20 / screen_error 9）全是 NULL。

⚠️ **断点不在仓储。** `finish_recycle_job_for_target` 与 `mark_recycle_job` 早就收
`dispatch_id` 并会写进去，`BotLoop._finish_recycle_job` 也早就往下传 —— 断的是唯一的
调用方：`_record_dispatch` 返回 `None`，派遣 id 根本没出来。

现在 `_record_dispatch` 返回它写下的那一发的 `dispatch_id`，`_recycle_once` 接住再传下去。
另外两个调用方（侦察、攻击）照旧忽略返回值，签名没变、行为不变。

## 为什么是现在

读回收邮件那条链路（`docs/回收闭环/邮件读实收-评估-2026-09-12.md`，数据模型随 `#325`
上线）要把一封回收报告的实收挂回「是哪一发回收派的」。没有这根外键就只能拿
**坐标 + 时刻**去凑，而**同一坐标一天可能回收好几趟**。这根 FK 就是为此存在的。

## ⚠️ 用例守的是接线，不是方法

新增的那条跑完 `_recycle_once` 顺利那一路：碰屏幕的全换成假的，但 `_record_dispatch`
与 `_finish_recycle_job` **都是真的**，断言「结作业时写下的 `dispatch_id`」就是
「`save_dispatch` 写下的那一发」。换成桩就等于没测这条接线 —— 而这次出事的正是接线
（同 `mark_recycle_job` 躺了一整版那次）。改之前它确实红：`[None] != [UUID(...)]`。

- Database: **无迁移** —— 列早就在了，这次只是终于往里写
- Verification: `pytest`（4498 passed / 266 skipped）/ ruff / mypy 全绿；**不需要实机**（改的是记账，不碰屏幕）
- Safety: 只多写一个此前恒为 NULL 的列；没有任何读取点，不改变现行运行
- Rollback: revert 本 PR（已写下的 dispatch_id 留着无害）
