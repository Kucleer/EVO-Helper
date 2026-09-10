---
issue: 303
agent: salvage-dialog-routing
type: Changed
date: 2026-09-10
---

**弹窗分流改造：purpose 贯穿，九格表替代 DialogKind。**

同一句中文「没有可执行的任务」在攻击上是保护期、在回收上是没残骸。
原 `DialogKind` 把「游戏说了什么」和「我们该做什么」混在一起，第二种含义放不进去。

## 改了什么

- `pirate_ui.py`: `DialogMessage` / `DispatchPurpose` / `DialogAction` 三 enum + 九格封闭表 + `dialog_action()`
- `pirate_loop.py`: `_handle_dialog` 及 4 个调用点全部显式传 `purpose`（必填无默认值）
- 行为不变：现有攻击/侦察仍走 SKIP_PROTECTED / STOP_ROUND
- 回收三格先摆好：SKIP_NO_DEBRIS 不写保护期排除，SKIP_NO_RECYCLERS 不停整轮且按 WARNING 落库

## 九格表

| 弹窗 × 意图 | 处置 |
|---|---|
| (NO_MISSION, ATTACK/SCOUT) | SKIP_PROTECTED |
| (NO_MISSION, RECYCLE) | SKIP_NO_DEBRIS ⚠️ 绝不写保护期排除 |
| (NO_SHIPS, ATTACK/SCOUT) | STOP_ROUND |
| (NO_SHIPS, RECYCLE) | SKIP_NO_RECYCLERS ⚠️ 不停整轮，WARNING 落库 |
| (LINES_FULL, *) | STOP_ROUND |

## 四个地雷，逐条防住

1. `purpose` 必填无默认值、4 个调用点全部显式传 —— 漏传会静默拿到攻击那一档
2. `(NO_MISSION, RECYCLE)` 不调 `_note_protection_period` —— 防止打得了的 bot 被排出 8 小时
3. `(NO_SHIPS, RECYCLE)` 是 SKIP_NO_RECYCLERS 不是 STOP_ROUND —— 两拨船独立
4. SCOUT 三格显式写死 —— 九格表封闭

## 变异验证

- `(NO_MISSION, RECYCLE)` → `SKIP_PROTECTED` ⇒ 两条用例红
- `(NO_SHIPS, RECYCLE)` → `STOP_ROUND` ⇒ 一条用例红
- 源码级断言：`_note_protection_period` 只在 SKIP_PROTECTED 分支；每个调用点都传 purpose

- Configuration: 无新增配置项。
- Database: 无迁移。
- Verification: `pytest tests/unit/tools/ -q`（766 passed）/ `ruff check` / `mypy` 全绿。
- Safety: **攻击/侦察侧行为一个字没变**；回收三格只摆着，接线在后续 PR。
- Rollback: revert 本 PR 即可。
