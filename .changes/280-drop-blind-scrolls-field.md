---
issue: 280
agent: console-settings
type: Removed
date: 2026-09-07
---

**撤掉攻击配置页上那个「军力榜盲拖屏数」框 —— 它存得进、读不出。**

## 它从来没生效过

- `domain.missions.ranking_command(*, bot_limit, blind_rows)` —— 参数表里没有 `blind_scrolls`
- `MissionScheduler._blind_scrolls()` —— 零调用点

而保存那条路是通的。所以填进去会存、会回显、页面上看着像生效了，实机行为一个字不变。

## ⚠️ 为什么这比「多一个没用的框」严重

它本想当「不改代码就能回滚到慢拖」的杠杆（理由整段写在迁移 `b8e1c4a72f05` 上），
但杠杆的另一半（库列 → 命令行）**始终没接上**。
**而人会去拨它的时刻，恰好是滚轮那条路已经出事的时刻。**

2026-09-07 真发生了一次。盲滚行数的自动标定是 `min(最近 5 次实测) − 83`，样本从
共库的 `system_log` 取、**不按 host 也不区分手工跑**。备份机上两趟调试各写下一条
「翻了 685 / 688 行到达 bot 区」——那两趟 `检测 0 屏`、`写入 0 条`，根本没到 bot 区，
是假实测。于是：

    09-06 全天 调度  821–832 行  ->  盲滚 738 行  正常
    09-06 22:13 手工 685 行(假)  ->  min 685 - 83 = 602
    09-07 08:07 调度 679 行(假)  ->  min 679 - 83 = 596   自我强化
    09-07        7 趟里 6 趟写入 0 条

而 bot 区在 821 行。页面上摆着这个框，填 80、保存成功、回显 80 —— 什么都没变。

**一行「（回滚用，当前不生效）」的说明挡不住这件事。** 那句话在页面上摆了半个月。

## 慢拖那条路一个字都没删

    python -m evo_helper.tools.ranking_scan --blind-scrolls N

`--blind-scrolls` / `scan(blind_scrolls=...)` / `drag_blind_rows` 全部原样保留，
优先级也照旧（只给 `--blind-scrolls` 就退回慢拖，两个都给行数赢）。撤掉的只是
「在页面上配它」这条通路：要回滚的人已经在读日志了，敲一条命令行不是负担；
留着那个框，代价是每个看到它的人都可能被绕进去一次。

⚠️ **保留**：`ranking_ui.BLIND_SCROLLS`（运行器自己的默认值）、`BLIND_SCROLL_SAMPLES`、
`BLIND_SCROLL_MARGIN`（行版标定靠它算 `BLIND_SCROLL_MARGIN_ROWS`）、
`bot_area_reached_message` / `bot_area_scrolls`（库里存着一整年屏版样本，读得出来
才谈得上过渡）。

## 用例：三条断言反过来了

原先有三条在钉「这个框刻意保留」，现在钉「它不许再长回来」：

1. `test_the_screen_era_validator_still_accepts_values`
   → `test_the_screen_era_knob_is_gone_for_good`（调度器上那三个方法都不存在）
2. `test_the_screens_column_survives_as_the_rollback_lever`
   → `test_the_screens_column_is_gone_by_head`
3. 页面渲染那条：`assert "完全不上命令行" in body`
   → `assert 'id="blind-scrolls"' not in body`

`test_ranking_blind_scrolls.py` 整个文件删掉 —— 它整份都在验一套已经不存在的机器。

新增 `test_drop_blind_scrolls_migration.py`，其中一条值得单说：

⚠️ **`test_the_model_no_longer_declares_it`** —— 删列这一类**唯一会静默分叉**的地方。
本地用 `create_all` 建表，所以「模型删了属性、迁移忘了删列」和反过来都是全绿：
`create_all` 建出来的表按模型来，本来就没那一列。只有生产那张真表会一直带着它，
然后在下一次「按模型对齐」时变成一个没人解释得清的差异。

另一条 `test_the_other_knobs_survive` 把同表其余 12 个旋钮点名：这条迁移刻意**不走**
`batch_alter_table`（那条路会重建整张表），真要是哪天改成批量重建而漏抄一列，症状是
「某个旋钮突然回到默认值」—— 页面上看着正常，只是它填的数不见了。

## 顺手修掉一个会连坐的用例写法

`test_ranking_capture_requests_migration.py` 有两条用例写的是
`command.downgrade(config, "-1")`，从 head 往回走一步。**head 一换人，`-1` 就落在
本条之后那一条上**，于是它们会为一个和自己无关的原因红 —— 这次就红了。
改成显式的 `DOWN_REVISION`，我新加那份也照这个写。

- Configuration: **撤掉一个配置项**。它此前对行为无影响，所以撤掉不改变任何行为。
- Database: 迁移 `c7d92f4a1b60`，`DROP COLUMN military_attack_config.blind_scrolls`。
  SQLite 3.35+ 与 PostgreSQL 都原生支持，所以不走 `batch_alter_table`；这一点由
  `test_upgrade_is_replayable_after_a_downgrade` 兜着（跑不动的方言上它会红，
  而不是等到生产启动时才炸）。
- Verification: `ruff check` / `ruff format --check` / `pytest -q`（4283 passed /
  256 skipped）全绿。
- Rollback: 回退迁移会把列加回来，但**加回来的是空列** —— 那一列的值本来对行为没有
  任何影响，所以「回退之后旧值没了」不构成损失。真要恢复慢拖，走上面那条命令行。

## 还没修的两件事（本 PR 范围之外）

1. **标定样本不隔离**：`recent_system_log_messages` 只做前缀匹配，不按 host、
   也不区分手工跑与调度跑。备份机上的手工跑会直接改生产的盲滚行数。
2. **`写入 0 条` 的趟仍然会记一条实测**：bot 区检测在一个 bot 都没见到的情况下
   判「到了」，然后把这个假值反馈回标定 —— 这是那个自我强化环里的关键一环。
