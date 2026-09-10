---
issue: 308
agent: salvage-remaining-2
type: Fixed
date: 2026-09-10
---

**⚠️ 两个生产 bug 修复 + 盲区修复 + 周期表残骸列。**

## Bug 1：since 棘轮效应（回收几乎不动的原因）

原来用 `last_decided` 当下界，但比较的是 `dispatched_at_utc` —— 两个不同的钟。
一发攻击约 1 小时后才释放，而那时 `since` 已被后来的决策推到它的派遣时刻之后
⇒ 它永远出局。**生产实测：22 发攻击只产生 1 条决策。**

修法：落 `recycle_enabled_at_utc`（滑块从 0 变非 0 时写一次），用它当固定下界。

## Bug 2：时刻列 WITHOUT time zone

`e5a8c3d2f1b4` 用 `sa.DateTime()` 建的，应为 `UTCDateTime`。
写进去的 aware UTC 被按会话时区转成 +08 墙钟再丢掉时区。

迁移 `b8d4f6e0a3c2`：改成 `DateTime(timezone=True)`，已有行按 +08 → UTC 修正。

## 盲区修复

三档生命周期原来塞在 `pending_recycle_jobs()` 里，带 `limit=10`。
第 11 条往后的旧作业永远不会被读到，R23「作业不跨周」对它们不生效。

修法：`cleanup_stale_recycle_jobs()` 扫全表、不带 limit，挂在 tick 里。

## §2 周期统计表残骸列

- 「残骸趟数」+「残骸占线」两列
- 实收恒为「未知」（回收邮件 hold）

## 迁移

| 迁移 | 内容 |
|---|---|
| `a7c3e5d9f2b1` | `recycle_enabled_at_utc` 列 + 已有数据补启用时刻 |
| `b8d4f6e0a3c2` | 修正 recycle_* 时刻列的时区 |

- Database: 两个迁移
- Verification: `pytest -q`（4413 passed / 266 skipped）/ `ruff check` / `ruff format --check` 全绿
- Safety: Bug 1 修复后回收才会真正跑起来；Bug 2 是预防性修复
- Rollback: revert 本 PR
