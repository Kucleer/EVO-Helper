---
issue: 48
agent: vision-game
type: Changed
date: 2026-08-13
---

bot 攻击模式改成「直接用预设 BBB 打，平局就对同一坐标再打」，分档整套删除。

```bash
python -m evo_helper.tools.bot_loop --targets 2:137:14              # 只认目标，不派
python -m evo_helper.tools.bot_loop --targets 2:137:14 --attack     # 用 BBB 打，平局再打
```

用户口径（2026-08-13）：「不再进行攻击侦查，直接用预设BBB进行攻击，如果同一坐标
攻击结果为平局，则继续进行攻击」；追问后确认「bot分档相关功能可以移除」。

## 为什么改（8/12 通宵的实测数据）

在生产库副本上按类别数了一遍。UTC 2026-08-12（= 本地 8/12 晚 → 8/13 早那一夜）：

| 链路 | mission_kind | 预设 | 发数 | 有战报 |
|---|---|---|---|---|
| bot | ATTACK | 探路 | 18 | 6 |
| bot | ATTACK | AAA | 3 | 0 |
| pirate | ATTACK | AAA | 15 | 9 |
| pirate | SCOUT | 侦察 | 44 | 0（侦察发本来就不产生攻击战报） |

派遣 80 发里 44 发是侦察，真正该有战报的攻击发 36 发，认领上的只有 15 份。
bot 这一侧 21 发只回来 6 份，而那 6 份全是**头一批**——从 UTC 15:04 起，
bot 再没有一发的战报被读回来，链路从 15:51 到 23:12 一发未派、全部目标卡在等战报。

按类别拆开之后，缺的那一类是明确的：**不是「还在飞」，也不是被游戏拒绝**
（那一夜 `accepted=False` 0 条），而是**翻信箱的开封预算被别的链路的报告吃光**。
`var/logs/mission-bot.log` 那一夜 11 趟开工里 8 趟撞上 `MAIL_MAX_OPENS`（8 封），
而开出来的封数里 37 次「VS 块读不出来」、16 次「not an attack report: 海盗攻击报告」
——同样那几封海盗报告（2:137:2/3/4）**每一趟都被重开一遍**，因为「这一封不是我的」
只活在一趟之内。bot 自己的报告就躺在它们下面。

这条改动不直接动那个预算（那是两条链路共用的 `PirateLoop._scan_mail_rows`），
但把 bot **每个目标要等的战报从两份减到一份**：原先探路一发 + 分档一发，现在只有
一发 BBB。同样的预算下能闭合的目标数直接翻倍。

## 设计要点

- **`BotPhase` 从五态减到三态**：`NEEDS_ATTACK` / `AWAITING_ATTACK_REPORT` / `DONE`。
  `NEEDS_PROBE` 与 `AWAITING_PROBE_REPORT` **删掉而不是留成死态**——留着就是
  `phase_of` 里两条永远走不到的分支，下一个读它的人会照着它改判据。
- **平局重打有界**：`MAX_ATTACKS_PER_TARGET = 3`（初打一发 + 最多补两发）。
  周期是**一轮**：计数直接由 `bot_dispatch_facts(since=本轮起点)` 的行数给出，
  控制台点「新一轮」就归零，**不新增任何一列**去记「打了几发」。
  取 3 的依据是与仓里另外两条自愈配额同一档（断线重开 3 次/滚动 1 小时、
  认不出目标只自愈一次）。
- **读不到战报时算不算一次**：6 小时之内算（那一发还在 `bot_dispatch_facts` 上，
  目标停在等战报，根本走不到重打）；超过 `MAX_REPORT_AGE` 不算（整条被剔掉，
  配额退回去）。合起来的上界是「每个目标每 6 小时最多因此多打一发」，
  与既有的「战报丢了就允许重来一次」是同一条规则，没有新增第二套计时。
- **算不出战果 ≠ 平局。** 四个数缺一个时 `outcome` 为空，那种目标**不重打**：
  重打的唯一依据是确认平局，拿一次 OCR 失手去再送一支舰队出去是反的。
- **只看最后一发的战果**，不是「有没有任何一发平过」——后者会让先平后胜的目标
  一直打到撞上限。仓储按 `dispatched_at_utc` 排序交出，次序是判据的一部分。
- **等战报优先于重打**：还有一发没回来就一律等，否则同一坐标上会摞起几支舰队。
- **海盗链路一个字没改**：它的判定走 `domain.scout_verdict`，不看战果、不分档。

## 分档删除的范围（用户确认「可以移除」）

- `domain/fleet_tier.py` 整个模块（`FleetTier` / `tier_for` / `TierThresholds` /
  `DEFAULT_TIER_THRESHOLDS` / `TierThresholdError` / `classify` / `TierVerdict` /
  `BOUNDARY_MARGIN`）。
  ⚠️ `parse_fleet_count` **不能跟着删**——它的消费者全在读战报那一侧
  （`vision.live_reports` / `vision.pirate_reports` / `vision.optional.report_screens`），
  是 `domain.battle_outcome` 那四个输入的解析器，与分档无关。搬到
  `domain/fleet_counts.py`，模块名跟着用途走。
- `/tiers` 页、侧栏入口、`GET|PATCH /api/tier-thresholds`、`TierThresholdsOut/Patch`、
  `TierThresholdsView` / `TierBandView`、`_threshold_bands/summary/changes`。
- `scheduler_config` 上的 `tier_alpha_from` / `tier_beta_from` / `tier_gamma_from`
  （迁移 `c1f70b8a26d4`，SQLite 走 `batch_alter_table`）。
- `bot_command` / `bot_loop.main()` 的 `--tier-thresholds`、`--probe`，
  `BotOptions.tier_thresholds`，`repository.tier_thresholds/update_tier_thresholds`、
  `latest_defender_units`、`mark_bot_target_skipped`、`REVISIT_SCOPE_TIER_NEGLIGIBLE`、
  `MissionConfigFreeze.tier_thresholds`。
- **`test_each_tier_maps_to_a_real_in_game_preset_title` 没有被连带删掉**，它守的
  「预设标题必须是游戏里真实存在的」仍然成立，改写成
  `test_the_attack_preset_is_a_real_in_game_preset_title`（守 BBB）+
  `test_the_runner_dispatches_with_exactly_that_preset`（守判据与派遣同源）。
  这条守卫现在比原先更要紧：BBB 正是要往右拖才看得到的那一档（PR #100），
  标题一旦对不上，这条链路**一发都派不出去**。

- 配置：删掉「分档阈值」页与它的三个数；`mission_tasks` 的参数不变
- 数据库：迁移 `c1f70b8a26d4` drop 掉 `scheduler_config` 的三列（一行业务数据不动）
- 验证：`pytest`（1492 passed / 51 skipped）、`ruff check src tests`、
  `ruff format --check src tests`、`mypy src`；在**生产库副本**上验
  `upgrade → downgrade → upgrade` 往返：20 张表逐表行数不变、配置行其余值不变、
  回滚后列集合与起点逐字一致、`integrity_check` ok、`foreign_key_check` 空；
  用生产的 `var/mission-config-freezes.jsonl`（7 行，其中 5 行带 `tier_thresholds`）
  实测**全部 7 行仍读得出来**；11 条变异逐条确认变红
- 安全：默认仍不派任何东西（`--attack` 才动鼠标）；出发前三道闸门不变
  （面板认得出目标 / 预设标题选中 / 简报写着攻击）；平局重打有硬上限；
  绝不伪造 `attack_dispatches` 行
- 回滚：`alembic downgrade a3d7b1e64c92` + revert 本次提交
