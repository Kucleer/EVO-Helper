---
issue: 355
agent: vision-game
type: Fixed
date: 2026-09-15
---

**补上 `#353` 颜色判据的两个缺口**（Codex 复核 P1-1 / P1-2,均已在源码上核实）。

## P1-2:最后一次点成功仍返回失败

`_select_mail_sub_tab` 的成功判断只在循环开头,最后一轮点完的读数没人判 ——
点对了、状态也对了,却去存现场图、报「切不到」,整趟作废。
改成多转一圈,最后那一圈**只判不点**。

## P1-1:战报入口绕过新逻辑

原先 `if not fleet_sub_tab and self._fleet_filter_looks_on():` 只问「舰队亮不亮」,
于是 `{战斗,侦察}`、`{侦察}`、**空集合**三档都不会去修 ——
空集合那一屏是「没有符合当前筛选条件的邮件」,读战报什么都读不到。

换成 `_mail_filters_need_fixing(fleet=False)`:问「现在是不是**不等于**这一趟要的样子」。

⚠️ 读不出按钮时**退回旧问法**(只在正面认出舰队开着才动手)——
否则退化成「认不出就当它不对」,那正是 `#339` 每轮自杀的形状。

- Configuration: 无
- Database: 无
- Verification: `ruff` / `mypy` 干净;全量 **4672 passed / 264 skipped**。
  2 条新用例;两处改动分别退回旧行为验过**各自会红**。
  守卫 `test_the_unfiltered_trips_guarantee_their_own_precondition` 跟着指向新调用,
  位置与处置两条未削弱。
- Safety: 不新增任何点击目标;点击预算不变(仍是 `MAIL_SUB_TAB_TRIES` 次)。
- Rollback: 回退即可;回退后回到「最后一轮成功也报失败」与「只在舰队亮着时才修筛选」。
