---
issue: 34
agent: domain-storage
type: Fixed
date: 2026-08-10
---

航线记账的两个错：释放时刻用错了列，侦察发根本没记。用户提供实机截图后暴露。

**一、航线不是在战报出来时释放的。** `count_inflight()` 一直用
`expected_report_at_utc > now` 数在飞数，而那一列是「出发 + 飞行时长 × 1」——
战报在**抵达**时产生的时刻。航线要等舰队**飞回来**才空。它的文档字符串写着
「这边问的是舰队回来没有」，意图是对的，用的那一列却回答的是「战报出来没有」。
后果：调度器在航线其实还占着时就去派，撞上游戏弹窗「同时派遣的舰队数量已达上限。」，
白跑一整轮。

新增 `attack_dispatches.line_free_at_utc`，与 `expected_report_at_utc` 在派出时
一起算好存下。倍数按发次分岔：攻击发 ×2（打完飞回来）、探路发 ×1（**单程**，
探路舰队会在攻击中损失）、侦察发 ×2（会飞回来）。「是不是探路发」复用
`domain.bot_round.is_probe_preset`，与 `phase_of` 同一条判据。

顺带拆掉了 `count_inflight` 里的「已有战报则航线已空」——那在一个钟的年代成立，
分成两个钟之后是同一个 1× 判据的侧门：攻击发的战报在 1× 到手，那时舰队还在往回飞。

**二、侦察发占航线，却一条记录都没有。** `pirate_loop.scout()` 只调 `_launch`，
不写 `attack_intents` / `attack_dispatches`。海盗一轮最多派 4 发侦察 →
最多 4 条航线对调度器完全隐形。现在 `scout()` 也记 intent + dispatch，
时机与 `attack()` 同语义（意图在点「出发！」之前写，被闸门拦下的也进日志）。

补记录必须避开的陷阱：日配额查询只按 `target_kind` 过滤，侦察照 `PIRATE` 记进去
就是**每发侦察吃掉一次攻击配额**，当天 32 次以 4 倍速度消失且完全静默。
因此新增 `attack_dispatches.mission_kind`（`ATTACK` / `SCOUT`）：

| 用途 | 口径 |
|---|---|
| 日配额 `count_dispatches_since` | 只数 `ATTACK` |
| 在飞数 `count_inflight` | **全都数**——侦察一样占航线 |
| 待收战报 `pending_reports_for_kind` | 只数 `ATTACK`——侦察不产生战报 |
| bot 三态 `bot_dispatch_facts` | 只数 `ATTACK`——同上 |

后两条与「防卡死反转成永久卡死」是同一个形状：把不会产生战报的行喂进
`ReportWaitPlanner`，它会永远判「该去收」。

- Configuration: 无。
- Database: 迁移 `d18b3f5c07ae`，`attack_dispatches` 加两列。
  `mission_kind` 带 `server_default='ATTACK'`（不给默认值 SQLite 会拒掉这条
  ALTER，`f2b9d3c07a41` 踩过），存量行一律 `ATTACK`；`line_free_at_utc`
  存量行一律 NULL——它们的 `flight_seconds` 本来就全是 NULL，无从回算，
  而 NULL 不计入在飞数。已在临时库上验过 upgrade / downgrade 与存量行取值。
- Verification: 937 passed（起点 921）/ ruff `src tests` 全绿 / mypy 90 源文件零问题。
  四处关键判据各做过一次变异（改坏 → 测试变红 → 还原）：在飞数改回 1×、
  探路倍数改成 ×2、配额把 `SCOUT` 算进去、待收战报把 `SCOUT` 算进去；
  另加一处 `bot_dispatch_facts` 同样验过。
- Safety: `scout()` / `attack()` 里的点击、等待、认屏一步未动，只加写库；
  一条点击顺序断言把这件事钉住。权威航线闸门仍在 runner 的 `LineCapacityGate`
  （它看屏），调度器的估算依旧是乐观的。`pyautogui.FAILSAFE` 未动。
- Rollback: 迁移可 downgrade（只删两列）。回滚后 `count_inflight` 需同步改回
  旧判据，否则查一列不存在的东西。
- 已知缺口: **已在同一分支补上**，见 `34-scout-line-clock.md`。原文记的是
  「侦察发的 `line_free_at_utc` 恒为 NULL，因此仍不计入在飞数」——那等于记了账
  没记钟，症状原封不动。
