---
issue: 311
agent: vision-game
type: Fixed
date: 2026-09-11
---

**残骸框标题用精确子串，框明明开着却判成「点错了按钮」。**

实测读到 `'回收残仍'`（「骸」→「仍」）。改成模糊匹配（`max_distance=2`），
并把「**不能**用精确子串」写成用例 —— 实拍那一次就会漏。

- Database: 无迁移
- Verification: `pytest -q` 全绿
- Safety: 放宽的是肯定判据，误判方向是「多点一次绿✓」，可接受
- Rollback: revert 本 PR
