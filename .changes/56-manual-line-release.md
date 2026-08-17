---
issue: 56
agent: web-api
type: Added
date: 2026-08-17
---

调度台上加一个「清理航线占用并立即派舰队」，把库里陈旧的航线占用一次放开。

航线占用是**推算**出来的：`attack_dispatches.line_free_at_utc` = 派出时刻 +
派出时读到的飞行时长 × 倍数，读不出飞行时长的那一档更是按
`UNKNOWN_LINE_HOLD`（90 分钟）硬占。舰队真的回港了，这两个钟都不会自己改口，
于是任务会一直显示「等航线」并被 `domain.scheduler.waiting_for_a_line` 压着，
而真实航线其实是空的。用户口径 2026-08-16：「时间到了，自然就释放了航线，
我会手动 check 后清理。」

新增 `attack_dispatches.line_released_at_utc`（人工放手的时刻），
`_still_holding_a_line` 与 `next_line_free_at` 同时看它。

**为什么另起一列而不是改写 `line_free_at_utc`。** 那一列是观测——这一发飞了
多久。把它改写成「现在」同样能让占用消失，但顺手抹掉了飞行时长这个事实，而
`domain.report_wait.vet_flight_time` 那道下限正是靠这批样本校准的（209 发攻击
里 66 发落在 0–59 秒，那是解析截断的残骸）。两列分开之后，「舰队几点回来」与
「人几点说它回来了」各说各的话。

- Configuration: 无。
- Database: 迁移 `a9d5f31c0e77`，在 `attack_dispatches` 上加一列可空的
  `line_released_at_utc`。存量行一律 NULL，含义与这一列加进来之前完全一致。
  时区语义按方言分岔（Postgres `TIMESTAMPTZ`、SQLite `DATETIME`），理由与
  `b6e0a4f21c98` 相同。生产库已升级并放开 1 条陈旧占用（改前 1 条、改后 0 条，
  整行 CSV 备份在 `%TMP%\evo-line-release-backup-20260817-011439.csv`）。
- Verification: 2131 passed（起点 2126）/ ruff `src tests` 全绿 / mypy 117 源文件
  零问题。两处变异各验过一次（改坏 → 测试变红 → 还原）：
  ① `release_held_lines` 写 NULL 而不是当前时刻 → 7 条红；
  ② `web/security.py` 里那道写请求校验短路放行 → 5 条红，其中包括新加的
  「未授权就不许放开航线」。
- Safety: 这是**会烧燃料**的写操作——放开之后调度器下一个 tick 就按新的空闲数
  去派真实舰队。页面上的按钮因此带二次确认，文案写明「派舰队」；鉴权走所有写
  接口共用的那一道（`web/security.py`），没有额外分支。后端不加自己的闸门：
  真实航线数只有用户看得见，服务端拦不出任何有意义的东西。权威闸门仍是
  runner 里看屏的 `LineCapacityGate`。
- Rollback: `alembic downgrade a7f2c9d40b16` 去掉那一列，行为退回本次改动之前；
  `line_free_at_utc` 与 `flight_seconds` 全程未被改写，历史无损。
