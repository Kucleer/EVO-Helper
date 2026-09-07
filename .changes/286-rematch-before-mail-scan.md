---
issue: 286
agent: pirate-loop
type: Fixed
date: 2026-09-07
---

**每趟进信箱之前复用一次库内补认领。** 原先它只在调度器启动时跑一次。

`repository.rematch_unlinked_reports()` 遍历库里 `dispatch_id IS NULL` 的战报，
拿报告自己带的 target + 时刻走 `_link_dispatch` 的唯一候选规则补认领 ——
只查库、不读页面。库里现在有 54 份未认领（近 7 天 8 份）。

## 为什么刷新周期不能是「重启」

下一件要在开封邮件之前按「这个时刻库里有没有战报」跳过已入库的邮件。跳过之后，
一份「在库里但没认领」的报告就**再也不会被重新开封**，也就再也没有机会在开封
路径上补认领 —— 那正是 2026-08-11 踩过的坑（`rematch_report_at` 的注释里记着
那四发的全过程：报告在库里、派遣永远停在「待战报」）。

**所以这一件是那一件的前置。**

## 接在哪两处

`reconcile_today()` 开头（日常那条链）和 `backfill_reports()` 开头（补录那一档，
控制台「开始」走它）。**都在「真要进信箱」那一层，不在 `run()` /
`_reconcile_if_due()` 上** —— `reconcile_today` 生产侧唯一的调用点就在
`_reconcile_if_due` 的冷却放行分支里，所以接在它里面天然排除了被冷却跳过的那些趟
（09-07 有 66 趟）。

侦察那两条（`collect_scout_reports` / `backfill_scout_reports`）**没碰**：
它们读侦察报告、写 `_store_scout_reading`，与 `battle_reports.dispatch_id` 上的
认领没有一行交集。另有一条用例钉住「没做这件事」。

⚠️ **两处都刻意排在「问单子」之前**，这是接线里唯一有先后要求的地方：
补认领会把几发派遣从「到点还没战报」里销掉，先问单子那个 N 就永远比真相大；
而 `BackfillTally.claimed` 是拿单子的**落差**算的，销掉的那几发落在落差里，
这一趟信箱就白拿一份不属于它的功劳。

## 耗时

新增 `rematch_unlinked_before_mail` 与 `UnlinkedRematch`，两处共用。
人看的一行照 `[耗时]` 的既有形状；结构化落 `record_system_log`
（`stage` / `matched` / `elapsed_ms`）；并进两侧的收尾汇总。

⚠️ 用 `.1f` 而不是 `StepTimer` 惯用的 `.0f`：正常一趟不到一秒，取整会一律显示成
`0s`，那就**看不出它什么时候变贵了** —— 而它逐份查候选、不是一次 SQL，
「变贵」正是这次要能看见的东西。

## ⚠️ 异常这一档吞掉，和启动那处口径不同

`mission_scheduler:915` 不吞；这里吞，走 `warn` + WARNING payload。

那一句在用户点「开始」的路上，抛出来当场有人看见、当场能重试，而且那一刻整轮
还什么都没做。**这一句排在整趟对账最前面** —— 抛出来会把「读战报」整趟带走，
那就是把 `reconcile_cooldown` 模块头记的那次断流故障换个成因再造一遍。

`failed` 与 `matched == 0` 在措辞上**刻意分得开**：安静地退化成
「一直补上 0 份」是这条路最坏的失效形态。

## 用例

新文件 `test_rematch_before_mail_scan.py`（10 条）：真进信箱那一趟调了它**且排在
问单子之前**（钉顺序不是次数）；冷却跳过那一趟一个字都没查库；耗时与份数进了
三处（**秒表打了桩**，钉的是「真在计时」而不是恒为 0）；亚秒不许被取整成 `0s`；
反面两条（抛异常时整趟对账照常走完、「没跑成」能按 WARNING 筛出来且不与
「补上 0 份」混）；补录摘要单独报这一行不吃进「认领上 N 发」；侦察补录**不**调它。

⚠️ 顺带给 `test_mailbox_reconciliation.py` 与 `test_backfill_reports.py` 的假仓储
补上 `rematch_unlinked_reports`。**这条不是可选的**：不补的话那两个文件会整体
走进「吞异常」的降级分支，**而且照样全绿** —— 它们会在一条根本没跑起来的路径上
继续绿。

- Configuration: 无新增配置项。
- Database: 无迁移。
- Verification: `ruff check` / `ruff format --check` / `pytest -q`
  （4300 passed / 256 skipped）全绿。
- Safety: 补认领本身的规则一个字没动。
- Rollback: `git revert`。

## 两件发现、没动的

1. **`limit=500` 现在成了一个每趟都吃的约束。** `_unlinked_report_rows` 是
   「新的在前」，所以未认领的行一旦积压超过 500，最旧的那些**每趟都够不到**，
   而这个方法从「启动一次」变成「每趟一次」并不会让它们轮到 ——
   原先靠 `tools/rematch_reports.py --limit` 人工救。要不要做成运维旋钮，待定。
2. **新增日志量**：0 份时原先库里没有任何痕迹，而耗时那条是无条件写的 ——
   两条链路每 15 分钟各一条 INFO，约 190 行/天。按「出事时能只靠库里日志定位」
   这条判据值得，但它是新增量。
