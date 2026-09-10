---
issue: 306
agent: salvage-remaining
type: Changed
date: 2026-09-10
---

**残骸回收剩余待办：前台配置 + 作业生命周期 + 兜底修复。**

## §1 前台配置三件套

- `MilitaryAttackConfigOut` 加 `recycle_rate_tenths`（0–10 整数十分位）
- `GET/PUT /api/attack-config` 带上这个字段
- `settings.html` 加滑块：range 0–10，显示 0.0–1.0，落库整数十分位
- `validate_recycle_rate_tenths` 校验

## §2 作业生命周期三档

- **被取代**：`save_recycle_job` 已有（每个坐标最多一条待执行）
- **时间上限**：`recycle_job_max_age_hours` 配置列（默认 24h），过期标 `expired`
- **跨周作废**：R23，`same_cycle` 判据，不同周标 `cross_cycle`
- 三档都记 `system_log`，不上页面（R17）
- `acc` 本身不跨周清零

## §5 兜底修复

残骸框标题读不到时：WARNING 落库 + `_reset_to_known_screen` + `navigator.invalidate`。

## §4.2 简报页告警补 intent_id

事后才查得出是哪一发（第八轮评审 §2）。

## 迁移

`f6b9d4e2a1c3`：加 `recycle_job_max_age_hours` 列。

- Configuration: `recycle_job_max_age_hours`（运维旋钮，默认 24h）
- Database: 迁移 `f6b9d4e2a1c3`
- Verification: `pytest -q`（4413 passed / 266 skipped）/ `ruff check` / `ruff format --check` 全绿
- Safety: 不改变现有攻击/侦察行为
- Rollback: revert 本 PR
