---
issue: 150
agent: domain-storage
type: Removed
date: 2026-08-17
---

bot 攻击移除「平局就对同一坐标再打一次」的重试规则。**只删规则，战果照旧观测。**

用户口径（2026-08-17）：「bot 攻击移除平局再打一次机制」。

## 界线：删的是规则，不是「平局」这个战果

这两件事在仓里挨得很近，很容易一起删掉，所以先把界线写死：

- **删掉的**：`DRAW` → `NEEDS_ATTACK` 这条判据，以及它的专属机件——
  `MAX_ATTACKS_PER_TARGET`（那个上限只为兜住这条规则而存在）、
  `bot_round._last_outcome`、`DispatchFact.outcome`，还有
  `bot_dispatch_facts` / `bot_dispatch_facts_many` 里取 `battle_reports.outcome`
  的那两处 select。
- **一个字没动的**：`domain.battle_outcome`（`OUTCOME_DRAW` 与那三条算术规则）、
  `battle_reports.outcome` 这一列的写入、`storage.intel.RESULT_DRAW` 与战果筛选、
  `web.display` 里「平」那个中文标签与字形。日志页、情报中心照旧看得到平局。

一句话：**平局还是平局，只是它不再触发第二支舰队。**

为什么连 `MAX_ATTACKS_PER_TARGET` 和 `DispatchFact.outcome` 一起删——留着就是
一个不约束任何东西的上限、和一个没人读的字段，而下一个读它们的人会照着它们改
判据。同一条理由 2026-08-13 已经用在探路那两个态上（「删掉而不是留成死态」）。

`phase_of` 因此收敛成两句话：没派过 → 该打；有一发还没回战报 → 等；
其余 → 走完。战果根本进不来这一层。

## 唯一还会「同一坐标再打一发」的路径没有变

`bot_dispatch_facts` 仍然按 `MAX_REPORT_AGE`（6 小时）把「派出了却永远等不到
战报」的那一发整条剔掉，目标随之退回 `NEEDS_ATTACK`。那条管的是**结果拿不到**，
不是**结果不满意**，与平局重打无关，也不受这次改动牵连。

## 两种选靶模式都验了

平局过的坐标现在一律表现成「已经打过了」：

- **范围模式**走 `_bot_remaining`（`phase_of(...) is not DONE` 才算还剩）；
- **军力优先**走 `_military_candidates`（`phase_of(...) is NEEDS_ATTACK` 才入池）。

两段是各写各的代码、共用同一条判据，所以两边各有一条集成用例。军力那条刻意
把那一发放到 24 小时以外——24 小时内的目标本来就被 `attacked_bot_targets_since`
挡掉，用那种目标验，旧规则下也是绿的，什么都守不住。

## 海盗链路一个字没改

`domain.pirate_round` 判态走 `domain.scout_verdict`，从来不看战果，本来就没有
这条规则可删；只更新了几处提到「bot 那边平局要重打」的对比说明。

- 配置：无
- 数据库：**无迁移，无 schema 变更**，一行业务数据不动；`battle_reports.outcome`
  照旧读写
- 验证：`pytest tests`（2159 passed / 80 skipped）、`ruff check src tests`、
  `ruff format --check src tests`、`mypy src`；两条变异逐条确认转红（把重试规则
  加回去 → 判态与两种模式的用例红；把 `DRAW` 这个战果本身删掉 → 观测侧的用例红）
- 安全：默认仍不派任何东西（`--attack` 才动鼠标）；出发前三道闸门不变；
  这次改动**只会让 bot 少派**，不会多派
- 回滚：revert 本次提交即可（没有迁移要退）
