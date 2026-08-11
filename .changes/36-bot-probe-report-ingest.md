---
issue: 36
agent: root
type: Fixed
date: 2026-08-11
---

bot 探路战报现在真的会被收回来并入库。这之前整条链路是**死锁**的。

## 死结长什么样

1. `domain/bot_round.py::phase_of` 要 `DispatchFact.has_report` 为真，才放目标进
   `NEEDS_ATTACK`；
2. `has_report` 来自 `storage/repository.py::bot_dispatch_facts()`，判据是
   `battle_reports` 里有没有一行的 `dispatch_id` 指着这一发派遣；
3. **全仓没有任何代码为 bot 探路写过 `battle_reports`**（库里仅有的 4 条全是手工跑
   `tools/ingest_report.py` / `ingest_pirate_report.py` 灌的，最新一条 08-09）；
4. 而唯一读战报的 `BotLoop.read_defender_units()` 只挂在 `NEEDS_ATTACK` 分支上。

**读战报的代码只在读过战报之后才会被执行。** 实机跑一整夜：
`AWAITING_PROBE_REPORT` 出现 152 次，`NEEDS_ATTACK` 出现 0 次。连带后果就是用户
报的那个现象——网页「情报中心」永远是空的：`storage/intel.py::_row_for()` 按目标
坐标取最新一条 `battle_reports`，没有战报就 `snapshot_at=None / total=None /
has_fleet_data=False`，整夜 26 发探路一行数据都没多。

## 收报告：只读详情页那一屏

`AWAITING_PROBE_REPORT` 的出路是新的 `BotLoop.collect_probe_reports()`：**一趟信箱**
（不是一个目标一趟）翻最上面六行，认得出的那几份读成 `BattleReport` 交给
`append_report`，由它按「出发坐标 + 目标坐标 + 时间就近」自己认领那一发派遣。

**取舍：只读战斗详情页，不进回放页，`fleet_snapshots` 一行不写。** 依据是两条实测事实：

- **逐舰种明细不在详情页上。** 参战战舰那两列的行界（`ReportLayout.participating_rows
  = (405, 750)`）是对着**回放页**量的，`tools/ingest_report.py` 也是从 replay 那一屏
  取的；详情页上同一段 y 正压着 VS 块（`detail_versus = (720, 370, 1200, 460)`）。
  所以「读明细」换掉的不是几次 OCR，是整整一屏。
- **打开那一屏要点「查看战斗回放」，而那个按钮至今没有标定过的点击坐标。**
  全仓搜不到任何回放入口的坐标（`report_screens._details_banner_bottom` 的注释里
  只把它当成一条要避开的亮带）。在一条真的驱动鼠标的链路上现编一个没核过的坐标，
  违反本仓「认不出的画面绝不点击 / 改硬编码坐标前先核对」这两条。

先例在仓库里：海盗战报刻意只记胜负与战损总数（用户口径 2026-08-09，为省性能，
`.changes/28-pirate-report-outcome.md`），并且**明确禁止**顺手补明细——
「明细一旦混进去，情报中心会把我方预设的舰船当成对方的舰队，比缺数据坏得多」。
这里守同一条：`participating_*` 与 `rounds` 一律空着，绝不用「单位」总数顶替。

因此情报中心拿到的是**报告时间 + 守方「单位」总数**（`battle_reports.defender_units`），
逐舰种那几列仍然空着——要补那部分，先标定回放入口按钮，那是独立的一件事。

## 报告就是读不到时的出路

`phase_of` 的 docstring 有一条前置条件：调用方必须先剔除「已判定战报永远不会来」的
派遣，否则目标**静默卡死**在等待态。收报告这一步会失败（OCR 读不出、报告还没到、
坐标认不上号），所以这条出路必须存在。落实在 `bot_dispatch_facts()` 里，与兄弟方法
`pending_reports_for_kind` 同源、同样是「现算」而不是别处先写好的标记：

> 派出超过 `MAX_REPORT_AGE`（6 小时）还没有战报的，整条剔掉。

剔干净之后目标退回 `NEEDS_PROBE`，也就是**允许重新探路**。代价有界：每个目标每 6
小时最多重来一次。判据取 `dispatched_at_utc` 而不是 `expected_report_at_utc`——这条
链路打同系目标、飞行按分钟计，`MAX_CREDIBLE_FLIGHT` 又把简报上读到的时长封在 6 小时内。
**战报已经回来的一律不剔**，否则一个本轮已经打完的 bot 会在六小时后被重新探一遍。

另外两道闸门防重复入库：入库前复核 VS 块的目标坐标（翻行时读过一遍，这里再读一遍，
两遍必须一致）；入库前按**报告时间**去重（与探索报告采集器同源），否则一份认不上号的
战报会每趟复制一行。

## 顺手修掉的已知缺陷

`read_defender_units()` 直接 `if not self._goto_planet_surface()` 就进信箱，没有先
`_reset_to_known_screen()`——而 `_on_planet_surface()` 的正面凭据是右上角那个未读数，
浮层正好盖住它。同一个缺陷在 `pirate_loop.collect_scout_reports()` 里刚修过（实机
2026-08-11 的 02:10 / 03:35 / 03:46 三次都倒在那里）。现在两条链路共用同一段
`_scan_mail()`，并在切不过去时 `_dump_frame` 留一帧现场。

分档取数也顺带改成**先问库**（`latest_defender_units(target, since=本轮)`），库里没有
才现场读一次。省的不只是十几秒 OCR：信箱那条路**没有任何时间闸门**，翻到的可能是上一轮
甚至上一天的报告，照它分档挑出来的档次是错的，而且完全静默。

- 配置：无变更
- 数据库：无变更（复用 `battle_reports` / `fleet_snapshots`）
- 验证：`pytest tests -q` 1052 passed / 24 skipped；`ruff check src tests`、
  `ruff format --check src tests`、`mypy src` 全过。12 处变异逐条验证过
  （去掉收报告、编造舰队构成、去掉放弃阈值、放弃阈值连已闭合的一起剔、
  不关浮层、不存现场图、改回按行号认报告、不复核 VS 坐标、不去重、
  分档不问库、读不出也照存、按行号对位），每一处都有测试变红，无漏网
- 安全：不删邮件、不领奖励、不派遣——新增路径只是打开邮件、读、返回。
  出发前那三道闸门（面板认得出目标、预设标题选中、简报写着攻击）一条未动；
  `bot_dispatch_facts` 原有的四个过滤（`accepted` / `mission_kind` / `target_kind` /
  坐标）一个都没动
- 回滚：`bot_dispatch_facts` 去掉那条 `or_` 放弃规则、`_sweep` 去掉
  `collect_probe_reports` 那一支即回到原行为（连同死锁）
