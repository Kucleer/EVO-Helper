---
issue: 30
agent: domain-storage
type: Fixed
date: 2026-08-09
---

调度器仓储查询的收尾修正：清掉波次 1 并行拆分欠下的债，并修掉评审揪出的几处查询缺陷。

## 修的是什么

- **`pending_reports_for_kind` 会让调度器永久空转。** 原查询返回该 kind 有史以来
  每一条真实派遣，没有时间下界。`ReportWaitPlanner` 见到任何一条
  `expected_report_at_utc` 为 NULL 的派遣就无条件返回 `COLLECT`，而库里现存的派遣
  **全是 NULL**（飞行时间从来没人读过，历史也不回填）。于是「有到期未收的战报」
  永久为真 → 海盗「有活干」的右半边被钉死 → 每个 tick 都去起 runner 收一封永远不会
  到的战报 → 扫描永远抢不到空隙。**防卡死机制原样反转成卡死机制。**

  放弃做成**查询时现算的规则**，不依赖任何人先去写标记（写标记的调度器还不存在，
  先落地标记再依赖它，中间这段时间一条都排不掉）。两条规则：有预计时间的过
  `grace` 判缺失；NULL 的按 `dispatched_at_utc` 过 `MAX_REPORT_AGE`（6 小时）判缺失。
  后者只管 NULL 那一档——拿派出时刻一起卡会把飞十小时还没到的远征当成缺失。

- **补 `count_inflight()`**（波次 1 漏做）。跨 kind 的全局量，航线不属于任何单条链路。
  与 `pending_reports_for_kind` 不是同一个查询：这边问「舰队回来没有」（带 `> now`），
  那边问「战报收了没有」（不带）。

- **`bot_dispatch_facts` 补 `accepted` / `dry_run` 过滤。** 兄弟方法都过滤了，这个漏了：
  一条被游戏拒掉的派遣会被当成「已派出且永远收不到战报」，该目标永远停在
  `AWAITING_ATTACK_REPORT`，bot 的完成态永远达不到。

- **「分档不值得打」从 `guard_status` 挪到 `target_revisits`**（scope `BOT_TIER_NEGLIGIBLE`）。
  那一列已被 `application/workflow.py` 用 `ALLOWED` / `REFUSED` 占着，`logs.html` 渲染成
  「未派出 · {guard_status}」；塞第三套词汇进去，一发确实飞出去了的攻击会显示成「未派出」。
  复查行写 `status=DONE` 而不是默认的 `PENDING`：`persistent_service` 数的是 PENDING 的
  条数、missions 页显示成「待复查」，用 PENDING 会让每跳过一个 bot 就凭空多一条谁也
  不会去执行的复查请求。

- **`mark_bot_target_skipped` 的 `since` 改必填**，且只在本轮真的有过意图时才记，一轮一条。
  原先 `since=None` 是「不限时间范围」，会把该坐标历史上每一轮的每一条 intent 全刷成
  跳过——手工跑一次 `--probe --attack` 就能触发，而且是静默的。

- **收敛 `ORIGIN`** 到 `domain.missions`，`tools/pirate_loop.py` 与
  `tools/scan_coordinates.py` 改成转手。原先三份，改一次主星要改三处。

- **拆掉 `tools/bot_loop.py` 里两处临时的 `repository: Any`**，返回类型从
  `list[Any]` / `tuple[Any, ...]` 收成 `DispatchFact`。`phase_of(...)` 那个唯一的生产
  调用点原先完全不被 mypy 检查。

- 配置：无变更
- 数据库：`target_revisits.scope` 由 `String(16)` 放宽到 `String(32)`
  （迁移 `b4d81f60c9a2`）。规格点名的两个 scope 名都超过 16 字；SQLite 不校验
  VARCHAR 长度所以现在不报错，正因为不报错才必须现在改。表结构无其他变更。
- 验证：`pytest`（782 passed，较基线 +25）、`ruff check`、`mypy`（88 源文件零问题）。
  第一、三、五条各做过一次变异验证：把实现改坏，确认对应测试真的变红，再还原。
- 安全：全部是只读查询与记录位的变更，不新增任何会点鼠标或派舰队的路径；
  `LineCapacityGate` 仍是权威闸门，调度器的航线估算只用来决定值不值得起一个进程。
- 回滚：`git revert` 这一串提交；迁移 `b4d81f60c9a2` 有 downgrade。
