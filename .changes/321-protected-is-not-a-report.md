---
issue: 321
agent: web-api
type: Fixed
date: 2026-09-11
---

**撞保护期那一行不算「攻击战报」。** 用户口径 2026-09-11。

它是 `to_protection_bounce_report` **合成**出来的，用途只有一个：给那一发结账，
让它从「到点未读」里消失。里面**一格资源都没有**，不是真读回了一份战报。

## ⚠️ 只砍一侧

| | 算不算 | 为什么 |
|---|---|---|
| `unread_reports` / `pending_reports_for_kind` | **算**（不动） | 那一发确实有结论了；动了它会退回去变成假欠账（同 `#320` 的回收 bug） |
| 周期表 / 星球效率的「攻击战报」 | **不算** | 没有资源，会把战报回收率撑高 |

用例把这条分界**两边都断言**了。

两处查询都改：`period_counts`（按 `reported_at_utc` 切）与
`origin_efficiency._counts`（按派出日归属）。只改一处，同一天的率在两个页面上
会是两个数。

## ⚠️⚠️ SQL NULL 陷阱（用例当场抓到）

第一版写的是裸的 `outcome != 'PROTECTED'`。SQL 里 `NULL != 'PROTECTED'` 求值是
**NULL 不是 TRUE** ⇒ 所有没写结局的战报被一起滤掉，生产上这一列整列归零、
且不报错。已带上 `IS NULL` 那一支；变异验证裸 `!=` 会红 2 条。

实测全库 `outcome='PROTECTED'` 只有 1 行，本次改动对现有数字影响几乎为零。

- Database: 无迁移
- Verification: `pytest -q` / ruff / mypy 全绿；三处变异都验过
- Safety: 纯统计口径，不碰结账那一侧
- Rollback: revert 本 PR
