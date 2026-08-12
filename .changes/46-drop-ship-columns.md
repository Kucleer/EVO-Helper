---
issue: 46
agent: web-api
type: Removed
date: 2026-08-12
---

情报中心移除四个舰种列（深空吞噬者 / 噬能截击者 / 钛能守卫者 / 收割者），只看舰队总数。
用户口径（2026-08-11）：「不读了吧，节约性能，在页面移除这 4 项，仅查看舰队总数」。
（补记：PR #98，提交 `276855a`。）

移除的理由是**数据源**，不是版面：

- **bot 那半边根本没有这四个数。** 逐舰种明细在战斗回放页上，而 bot 链路刻意只读详情页、
  `fleet_snapshots` 一行不写（见 `tools.bot_loop` 模块头，理由见 #36）。要补上得多点开
  一次「查看战斗回放」——那个按钮至今没有标定过的坐标，每份报告还要多花两三秒 OCR。
  用户选择不为这四个数付这笔钱。
- **海盗那半边有**（`scout_trigger_ships`），但「收割者」一列在实机 98 份报告里**一份都没
  读出来**（ROI 落空），摆在列表上也是满屏的「—」。

留成空元组而不是删掉这个常量：取数与渲染那条路仍然按它走，回放页哪天标定好了，把
`PIRATE_TRIGGER_SHIPS` 填回去就有列。**没有改成 `or 0`**——`None` 是「没读到」，`0` 是
「真的没有」，整个 ATTACK/SKIP/UNREADABLE 三值判定建立在这个区分上（#40 为此一路把
`int | None` 保到了像素）。

原来那条测试断言「四列 == 判定舰种」，意图随这个决定消失，改写成钉住「列表不再逐舰种
开列」，并顺带钉住判定舰种本身没被删。

- Configuration: 无。
- Database: 无迁移；只是少读了 `scout_trigger_ships`。
- Verification: 1537 passed、`ruff check src tests`、`ruff format --check src tests`、
  `mypy src` 全绿。
- Safety: 纯显示改动，未驱动游戏，未触碰生产库；侦察判定读的仍是那四个舰种。
- Rollback: 把 `LIST_SHIP_COLUMNS` 填回 `PIRATE_TRIGGER_SHIPS` 即恢复四列。
