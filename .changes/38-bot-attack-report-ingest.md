---
issue: 38
agent: root
type: Fixed
date: 2026-08-11
---

bot **攻击发**的战报没人读：没入库、状态不更新、攻击日志的战果列永远空着。
正因是 `BotLoop._sweep()` 只把 `AWAITING_PROBE_REPORT` 的目标交去收信箱，
而 `AWAITING_ATTACK_REPORT` **全仓没有任何代码去推进**。

## 甲、现象：同一页上两种结局

实机 2026-08-11 的攻击日志，同一页、同一个 bot 链路：

    10:30:20  bot  2:26:12  预设 探路  已派出  战果 负（战损 我 1 · 敌 0）
    10:29:18  bot  2:25:10  预设 探路  已派出  战果 负（战损 我 1 · 敌 0）
    10:28:17  bot  2:24:5   预设 探路  已派出  战果 负（战损 我 1 · 敌 0）
    10:27:16  bot  2:16:7   预设 AAA   已派出  战果 待战报  预计战报 18:58:33

探路那三发的战果与战损都读出来了，分档之后真打出去的 AAA 那一发停在「待战报」。
它的战报就躺在同一个信箱里、主题也是「攻击报告」，只是**没有人去开**。

## 乙、正因：`AWAITING_ATTACK_REPORT` 是个死结

`_sweep()` 里那句注释写着「其余三态这一趟没事可做：等攻击战报，或已走完」。
那句话预设了「攻击发的战报由调度器那条等待链路收」——**而调度器不收**：
`mission_scheduler` 到点做的是把这条链路整个重新起一遍
（`_command_for` → `bot_command`），起来之后走的还是 `_sweep()`。于是：

    收取名单 = 只有 AWAITING_PROBE_REPORT
      → 攻击发的战报永远不入库
      → bot_dispatch_facts 里那一发 has_report 永远为假
      → phase_of 永远停在 AWAITING_ATTACK_REPORT，到不了 DONE
      → mission_scheduler._bot_remaining 永远大于 0，这一轮永远没跑完
      → 攻击日志的 outcome 列永远是「待战报」

直到 6 小时后 `bot_dispatch_facts` 按 `MAX_REPORT_AGE` 把那一发整条判掉、
目标退回去**重打一遍**——一条航线加一次配额，换来同样的结局。

**这和之前 `AWAITING_PROBE_REPORT` 那个死结是同一个形状**（那次实机跑一整夜，
那一态出现 152 次、下一态 0 次）。上一轮修复只修好了探路那一半。

## 丙、修法：把攻击发放进同一趟信箱，而不是另写一条读法

收取这条路径**本来就与预设无关**：认归属靠 VS 块里的目标坐标，翻信箱靠
「攻击报告」这个主题，两种发的战报长得一模一样，路径上没有一处读得到
「这一份是哪个预设打的」。所以：

- `collect_probe_reports` → `collect_battle_reports`，`_ingest_probe_report` →
  `_ingest_battle_report`（一行读法都没改，改的是名字与调用范围）。
  故障截图前缀同步改成 `battle-report-unreadable`。
- `_sweep()` 的收取名单改成具名集合 `_AWAITING_REPORT = {AWAITING_PROBE_REPORT,
  AWAITING_ATTACK_REPORT}`。写成具名集合而不是在分支里手写 `phase is ...`：
  漏一个态不报错、不留日志，只是那一档目标再也不被收取——这次就是这么漏的。
- **两个等待态并进同一趟信箱**，不是各进一趟：两种发的报告混在同一页上按时间
  倒序排，分两趟要把「关浮层 → 切地表 → 开信箱 → 翻四屏」付两遍（实机一趟
  83 秒），还会互相抢那 8 封的开封预算。

时间下界、去重、胜负计算一律沿用探路那一半已有的口径，一个字没改：
`bot_report_due_at` 交出的是**未闭合**派遣的派出时刻（探路那一发闭合后自然
让位给攻击那一发）、去重按 `has_report_at` 的报告时间、胜负按
`domain.battle_outcome` 的「剩余 = 单位 − 损失」现算。

- Configuration: 无。
- Database: 无迁移。写入的仍是 `battle_reports`，认领派遣仍走 `append_report`。
- Verification: `pytest tests -q`（1285 passed / 51 skipped）、`ruff check src tests`、
  `ruff format --check src tests`、`mypy src`。三处变异验证均确认变红后还原：
  ① `_AWAITING_REPORT` 去掉 `AWAITING_ATTACK_REPORT` → `test_each_phase_routes_to_
  exactly_one_action[AWAITING_ATTACK_REPORT]` 与 `test_both_waiting_phases_are_
  collected_in_the_same_single_mail_trip` 变红；② `phase_of` 的
  `all(item.has_report for item in attacks)` 改成 `True` → `test_the_attack_report_
  is_what_finishes_the_target` 变红；③ `bot_report_due_at` 去掉
  `BattleReportRow.id.is_(None)` → `test_the_mail_floor_follows_the_attack_dispatch`
  变红。
- Safety: 信箱里仍然只切「报告」标签，别的筛选一个都不碰
  （白名单由 `tests/unit/tools/test_mailbox_clicks.py` 钉着，未改动）。
  读不出来就不存、不存半份、缺数留空不拿 0 顶替。「从新往旧读、读到已入库的
  就收工」的早停原样保留。未驱动游戏，未触碰 `var/evo-helper.db`。
- Rollback: 把 `_AWAITING_REPORT` 改回只含 `AWAITING_PROBE_REPORT` 即回到原行为
  （连同攻击发那一半的死结）。
